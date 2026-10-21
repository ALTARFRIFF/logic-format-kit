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


# ---------------------------------------------------------------- 写（真插入）
def add(data, bar=None, tick=None, bar_ticks=3840, donor=None):
    """在容器末尾追加一条 marker 记录（真插入 48 字节）。

    ⚠️ 实测未生效：Logic 读 marker 数量时似乎不只看这个容器的载荷长度 ——
    往 `_TL-MARK` 追加一条后 Logic 仍只显示 3 个 marker（位置改写则完全正常）。
    推测 marker 还有一层 Logic 自维护对象（`_TL-MARK` → `_TL-MARK2` 只移动
    不增删也多出 6 个块）。**移动用 `set_position`，新增请用 Logic。**

    模板取自已有记录（克隆除位置外的全部字节），所以不需要知道其余字段。
    名字是独立对象，这里不创建 —— Logic 会给新 marker 一个默认名字。
    """
    if bar is not None and tick is not None:
        raise ValueError("bar 与 tick 只能给一个")
    if tick is None:
        if bar is None:
            raise ValueError("需要 bar 或 tick")
        tick = bar_to_tick(bar, bar_ticks)
    ch = P.chunks(data)
    head = P.header(data)
    c = find_marker_chunk(ch)
    if c is None:
        raise ValueError("这个工程没有 marker 容器")
    n = (len(c.payload) - TAIL) // REC
    # 以第一条记录为模板：除位置与序号缓存外原样克隆，避免自造未知字段
    rec = bytearray(c.payload[0:REC] if donor is None else donor)
    struct.pack_into("<I", rec, 5, tick_to_pos(tick))
    rec[16] = ((n + 1) * 4) & 0xFF
    cut = len(c.payload) - TAIL
    out = P.insert_in_payload(P.Chunk(pos=c.pos, tag=c.tag, length=c.length,
                                      raw=c.raw), cut, bytes(rec))
    ch2 = [out if x is c else x for x in ch]
    return P.seal(ch2, head)


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

    # 真插入
    d3 = add(da, bar=20)
    chk(len(d3) == len(da) + REC, "add 应恰好增加 48 字节")
    chk(len(list_markers(d3)) == 4, "add 后有 4 个 marker")
    chk(list_markers(d3)[3]["bar"] == 20, "新 marker 在第 20 小节")
    chk(len(P.chunks(d3)) == len(P.chunks(da)), "add 不新增块")

    print("markers: %d passed, %d failed" % (ok, bad))
    return bad == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
