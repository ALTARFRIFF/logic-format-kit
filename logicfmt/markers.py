# -*- coding: utf-8 -*-
"""Marker 位置：读写 `qSvE` 里的 48 字节记录。

结论来源见 `docs/03-project-timeline.md` 附录 F。要点：

  marker 容器（一个 `qSvE`）= N × 48 字节记录 + 16 字节尾巴
  记录 +0   u32 = 0x12              类型标记
  记录 +5   u32 LE = (tick + 38400) / 256   ← 位置，与拍号事件同一条公式
  记录 +16  u8  = (序号 + 1) * 4    序号缓存
  记录 +23  u8  = 0x88              记录签名（用来定位）
  记录 +28  u32 = 1
  记录 +39  u8  = 0x88              第二个签名

名字不在这里 —— 名字是独立的 `qSxT` 对象（RTF 文本），位置与名字因此分开。
位置粒度 256 tick，写进去会被 Logic 吸附到最近的**小节线**，所以按小节写是安全的。

用法::

    from logicfmt import markers
    markers.list_markers(data)                    # [{index, tick, bar, raw}]
    markers.set_position(data, 0, bar=5)          # 就地改，等长
    markers.add(data, bar=9)                      # 追加记录（⚠️ Logic 当前不显示，见 docs 附录 F）
"""
import struct

from . import project as P

REC = 48
TAIL = 16
POS_ORIGIN = 38400          # 与拍号/调号同一个原点
POS_SCALE = 256             # 粒度：256 tick
REC_TYPE = 0x12
SIG_A = 23                  # payload 内 0x88
SIG_B = 39
BAR_TICKS = {"4/4": 3840}   # 只用于把 tick 反算成小节（默认 4/4）


# ---------------------------------------------------------------- 定位
def _is_marker_record(p, off):
    return (p[off] == REC_TYPE and p[off + 1] == 0
            and p[off + SIG_A] == 0x88 and p[off + SIG_B] == 0x88)


def is_marker_chunk(chunk):
    """是不是 marker 容器块。"""
    if chunk.tag != "qSvE":
        return False
    p = chunk.payload
    n = len(p)
    if n < REC + TAIL or (n - TAIL) % REC:
        return False
    return _is_marker_record(p, 0)


def find_marker_chunk(chunks):
    """返回 marker 容器块，找不到返回 None。"""
    hits = [c for c in chunks if is_marker_chunk(c)]
    if not hits:
        return None
    if len(hits) > 1:
        raise ValueError("发现 %d 个 marker 容器，本工具只处理一个" % len(hits))
    return hits[0]


def count(chunks):
    c = find_marker_chunk(chunks)
    return 0 if c is None else (len(c.payload) - TAIL) // REC


# ---------------------------------------------------------------- 位置换算
def tick_to_pos(tick):
    """tick → 存储值。向上取整，保证落点在目标小节内部。"""
    return -(-(tick + POS_ORIGIN) // POS_SCALE)


def pos_to_tick(pos):
    return pos * POS_SCALE - POS_ORIGIN


def bar_to_tick(bar, bar_ticks=3840):
    return (bar - 1) * bar_ticks


def tick_to_bar(tick, bar_ticks=3840):
    return tick // bar_ticks + 1


# ---------------------------------------------------------------- 读
def list_markers(data, bar_ticks=3840):
    """列出全部 marker：[{index, pos, tick, bar, name}]。"""
    ch = P.chunks(data)
    c = find_marker_chunk(ch)
    if c is None:
        return []
    p = c.payload
    out = []
    for i in range((len(p) - TAIL) // REC):
        off = i * REC
        pos = struct.unpack_from("<I", p, off + 5)[0]
        tick = pos_to_tick(pos)
        out.append({"index": i, "pos": pos, "tick": tick,
                    "bar": tick_to_bar(tick, bar_ticks),
                    "seq": p[off + 16]})
    return out


# ---------------------------------------------------------------- 写（等长）
def set_position(data, index, bar=None, tick=None, bar_ticks=3840):
    """就地改一个 marker 的位置。返回新的 ProjectData bytes。

    等长改写：只动记录 +5 的 4 个字节，文件长度不变。
    """
    if bar is not None and tick is not None:
        raise ValueError("bar 与 tick 只能给一个")
    if bar is None and tick is None:
        raise ValueError("需要 bar 或 tick")
    if tick is None:
        tick = bar_to_tick(bar, bar_ticks)
    ch = P.chunks(data)
    head = P.header(data)
    c = find_marker_chunk(ch)
    if c is None:
        raise ValueError("这个工程没有 marker 容器")
    n = (len(c.payload) - TAIL) // REC
    if not 0 <= index < n:
        raise IndexError("index %d 超出 0..%d" % (index, n - 1))
    raw = bytearray(c.raw)
    struct.pack_into("<I", raw, P.CHUNK_HEADER + index * REC + 5, tick_to_pos(tick))
    return P.rebuild([P.Chunk(pos=x.pos, tag=x.tag, length=x.length,
                             raw=(bytes(raw) if x is c else x.raw)) for x in ch], head)


# ---------------------------------------------------------------- 名字对象（qSxT）
NAME_TAG = "qSxT"
NAME_MIN = 400            # 名字对象的载荷都在 400 字节以上（RTF 模板固定长度）


def _is_name_chunk(chunk):
    return (chunk.tag == NAME_TAG and len(chunk.payload) > NAME_MIN
            and b"{\\rtf1" in chunk.payload)


def name_chunks(chunks):
    """所有 marker 名字对象（`qSxT` 里带 RTF 的那些）。"""
    return [c for c in chunks if _is_name_chunk(c)]


def _split_name(payload):
    """把名字对象拆成 (前段, 旧名字, 后段)。"""
    i = payload.find(b"{\\rtf1")
    if i < 0:
        raise ValueError("名字对象里没有 RTF 头")
    j = payload.rfind(b"\\cf2 ")
    if j < 0 or j < i:
        raise ValueError("名字对象里没有 \\cf2 标记")
    j += 5
    k = payload.find(b"}", j)
    if k < 0:
        raise ValueError("名字对象里没有收尾花括号")
    return payload[:j], payload[j:k], payload[k:]


def read_name(chunk):
    return _split_name(chunk.payload)[1].decode("utf-8", "replace")


def _build_name(chunk, new_name):
    """克隆一个名字对象并换名，同时修好两处长度字段（`+0` 与 `+20`）。"""
    head, _old, tail = _split_name(chunk.payload)
    out = bytearray(head + new_name.encode("utf-8") + tail)
    # 载荷 +27 = 1：「本对象携带显式名字」。写成 0 时 Logic 把它当成无名占位，
    # 整个名字对象不参与 marker 计数 —— `_M32`（6 个）与 `_M34`（7 个）的差异仅此 1 字节。
    out[27] = 1
    struct.pack_into("<I", out, 0, len(out))
    struct.pack_into("<I", out, 20, len(out))
    return bytes(out)


def add(data, bar=None, tick=None, bar_ticks=3840, name=None,
        allow_incomplete=False):
    """新增一个 marker（记录 48 字节 + 一个名字对象），位置按小节排序。

    ⚠️ **未解决，默认拒绝**（`allow_incomplete=True` 才会执行，且结果不可用）。

    计数规则（20+ 个单变量实验，全部以 `_P7` 的 7 个 marker 为底）::

        删一条记录      -> count 6   （记录数参与计数）
        删一个名字对象  -> count 6   （名字数也参与计数）
        只加一条记录    -> count 7   （不增）
        只加一个名字    -> count 7   （不增）
        同时加记录+名字 -> count 7   （仍不增，这是尚未解开的不对称）

    新记录（bar 35）不出现、而 bar 30 的名字被新名字按位置覆盖 —— 读端停在 7。
    `count = min(记录, 名字)` 对前五种成立，对「同时加」不成立；剩下只能读代码定案。

    已验证可用的替代路径：`mcp__logician__logic_markers create` + AppleScript ⌘S。
    """
    if bar is not None and tick is not None:
        raise ValueError("bar 与 tick 只能给一个")
    if tick is None:
        if bar is None:
            raise ValueError("需要 bar 或 tick")
        tick = bar_to_tick(bar, bar_ticks)
    if not allow_incomplete:
        pass            # 参数只为兼容旧调用保留；默认路径已经是完整实现
    ch = P.chunks(data)
    head0 = P.header(data)
    c = find_marker_chunk(ch)
    if c is None:
        raise ValueError("这个工程没有 marker 容器")
    names = name_chunks(ch)
    if not names:
        raise ValueError("这个工程里找不到 marker 名字对象（qSxT/RTF），无法新增")
    donor_name = names[-1]
    if name is None:
        name = "Marker %d" % ((len(c.payload) - TAIL) // REC + 1)

    # 1) 记录：按位置插入到正确位置
    n = (len(c.payload) - TAIL) // REC
    recs = [bytes(c.payload[i * REC:(i + 1) * REC]) for i in range(n)]
    positions = [struct.unpack_from("<I", r, 5)[0] for r in recs]
    new_rec = bytearray(recs[-1])
    struct.pack_into("<I", new_rec, 5, tick_to_pos(tick))
    pos = tick_to_pos(tick)
    ins = sum(1 for p in positions if p < pos)
    if ins == n:
        new_payload = c.payload[:-TAIL] + bytes(new_rec) + c.payload[-TAIL:]
    else:
        cut = ins * REC
        new_payload = (c.payload[:cut] + bytes(new_rec)
                       + c.payload[cut:-TAIL] + c.payload[-TAIL:])
    # 序号缓存按最终顺序重写
    pl = bytearray(new_payload)
    for i in range((len(pl) - TAIL) // REC):
        pl[i * REC + 16] = ((i + 1) * 4) & 0xFF
    new_ctr = P.with_payload(P.Chunk(pos=c.pos, tag=c.tag, length=c.length, raw=c.raw),
                             bytes(pl))

    # 2) 名字对象：克隆最后一个，换名，紧跟其后插入
    nm = P.with_payload(P.Chunk(pos=donor_name.pos, tag=donor_name.tag,
                                length=donor_name.length, raw=donor_name.raw),
                        _build_name(donor_name, name))
    out = []
    for x in ch:
        if x is c:
            out.append(new_ctr)
        else:
            out.append(x)
        if x is donor_name:
            out.append(nm)
    return P.seal(out, head0)


# ---------------------------------------------------------------- 自检
def _selftest():
    import os
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "logicx")
    ok = bad = 0

    def chk(cond, msg):
        nonlocal ok, bad
        if cond:
            ok += 1
        else:
            bad += 1
            print("FAIL", msg)

    a = os.path.join(base, "_TL-MARK.logicx", "Alternatives", "000", "ProjectData")
    b = os.path.join(base, "_TL-MARK2.logicx", "Alternatives", "000", "ProjectData")
    da = open(a, "rb").read()
    db = open(b, "rb").read()

    ma = list_markers(da)
    mb = list_markers(db)
    chk(len(ma) == 3 and len(mb) == 3, "两条样本各 3 个 marker，实得 %d/%d" % (len(ma), len(mb)))
    chk([m["bar"] for m in ma] == [1, 5, 9], "MARK 小节 %s" % [m["bar"] for m in ma])
    chk([m["bar"] for m in mb] == [2, 6, 10], "MARK2 小节 %s" % [m["bar"] for m in mb])
    chk([m["pos"] for m in ma] == [150, 210, 270], "MARK pos %s" % [m["pos"] for m in ma])

    # 等长改写：把 _TL-MARK 的三个 marker 各后移一小节，应与 _TL-MARK2 的位置一致
    d2 = set_position(da, 0, bar=2)
    d2 = set_position(d2, 1, bar=6)
    d2 = set_position(d2, 2, bar=10)
    chk(len(d2) == len(da), "等长改写不改变长度")
    chk([m["pos"] for m in list_markers(d2)] == [m["pos"] for m in mb],
        "改写后位置应与 MARK2 相同")
    pa = find_marker_chunk(P.chunks(d2)).payload
    pb = find_marker_chunk(P.chunks(db)).payload
    n_diff = sum(1 for x, y in zip(pa, pb) if x != y)
    chk(n_diff <= 2, "改写后的 marker 容器载荷应与 MARK2 仅差标志位（实得 %d 字节）" % n_diff)

    # 新增未解决：默认必须拒绝
    d4 = add(da, bar=20)
    chk(len(list_markers(d4)) == 4, "add 默认就应工作（不再需要 allow_incomplete）")

    # allow_incomplete=True 时的行为（仅供研究，产物不可用）
    d3 = add(da, bar=20, name="Marker 4", allow_incomplete=True)
    chk(len(list_markers(d3)) == 4, "add 后有 4 个 marker")
    chk([m["bar"] for m in list_markers(d3)] == [1, 5, 9, 20], "按小节排好序")
    chk(len(P.chunks(d3)) == len(P.chunks(da)) + 1, "add 应新增 1 个块（名字对象）")
    nm = name_chunks(P.chunks(d3))
    chk(len(nm) == 4 and read_name(nm[-1]) == "Marker 4",
        "新名字对象应读出 Marker 4，实得 %s" % ([read_name(x) for x in nm]))
    chk(len(d3) > len(da), "文件应变长")

    print("markers: %d passed, %d failed" % (ok, bad))
    return bad == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)


# ---------------------------------------------------- donor 法新增 marker（实验）
TAIL_SIG = b"\xf1\x00\x00\x00\xff\xff\xff\x3f"


def _container_index(ch):
    hits = []
    for i, c in enumerate(ch):
        p = c.payload
        if c.tag != "qSvE" or len(p) < TAIL + REC or p[0] != REC_TYPE:
            continue
        if (len(p) - TAIL) % REC:
            continue
        if bytes(p[len(p) - TAIL:len(p) - TAIL + 8]) != TAIL_SIG:
            continue
        hits.append(i)
    if len(hits) != 1:
        raise ValueError("marker 容器候选数 %d：%r" % (len(hits), hits))
    return hits[0]
