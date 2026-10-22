# -*- coding: utf-8 -*-
"""logicfmt.midi —— MIDI 区域里的音符与控制器事件。

一个 MIDI 区域的内容存在一个独立的 ``qSvE`` 块里（"记谱容器"）::

    payload = [事件记录 ...][16 字节尾巴]
    尾巴   = f1 00 00 00 ff ff ff 3f + 8 x 00

音符记录（Logic 手绘区域里实测为 32 字节）::

    +0  u32   0x90                        记录类型：音符
    +4  u32   tick(相对区域起点) + 38400   位置（与拍号/调号/标记同一条公式）
    +11 u8    velocity  1..127
    +12 u8    pitch     MIDI 号，60 = C3（Logic 记法）
    +23 u8    0x89                        记录尾签名
    +28 u32   length（tick，960 = 一拍）

控制器记录（`logic_record_midi` 录出来的区域里为 48 字节）以 ``0xb0`` 开头，
字段详见 docs/03 附录 G；本模块只做「就地改音符」和「在纯 32 字节记录的容器里追加音符」，
两种改法都已用 Logic 读回验收。

未实现（明确拒绝）：新增一个 MIDI 区域（需要区域对象 + 轨道行 + 登记），
以及控制器事件的写入（记录长度随上下文变化）。
"""

import struct

MAGIC = 0x90
import os

TAIL_SIG = b"\xf1\x00\x00\x00\xff\xff\xff\x3f"
TAIL_SIZE = 16
POS_ORIGIN = 38400
REC32 = 32

OFF_TYPE = 0
OFF_POS = 4
OFF_VEL = 11
OFF_PITCH = 12
OFF_SIG = 23
OFF_LEN = 28

TICKS_PER_BEAT = 960


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def note_offsets(payload):
    """返回容器里所有音符记录的偏移（按 0x90 起点 + 0x89 尾签名判定）。"""
    out = []
    for o in range(0, len(payload) - REC32 + 1):
        if payload[o] == MAGIC and payload[o + OFF_SIG] == 0x89:
            out.append(o)
    return out


def tail_offset(payload):
    if payload.endswith(TAIL_SIG + b"\x00" * 8):
        return len(payload) - TAIL_SIZE
    raise ValueError("不是记谱容器：结尾不是 16 字节尾巴")


def is_container(payload):
    try:
        tail_offset(payload)
    except ValueError:
        return False
    return bool(note_offsets(payload))


def find_containers(data):
    """在 ProjectData 里找所有记谱容器，返回 [(块序号, 块)]。"""
    from . import project as P
    hit = []
    for i, ch in enumerate(P.chunks(data, strict=False)):
        if ch.tag == "qSvE" and is_container(ch.payload):
            hit.append((i, ch))
    return hit


def read_notes(payload, region_start_tick=0):
    rows = []
    for o in note_offsets(payload):
        rows.append({
            "offset": o,
            "tick": _u32(payload, o + OFF_POS) - POS_ORIGIN,
            "abs_tick": region_start_tick + _u32(payload, o + OFF_POS) - POS_ORIGIN,
            "velocity": payload[o + OFF_VEL],
            "pitch": payload[o + OFF_PITCH],
            "length": _u32(payload, o + OFF_LEN),
        })
    rows.sort(key=lambda r: (r["tick"], r["pitch"]))
    return rows


def set_velocity(payload, offset, velocity):
    if not 1 <= velocity <= 127:
        raise ValueError("velocity 必须在 1..127")
    if payload[offset] != MAGIC:
        raise ValueError("偏移 %d 不是音符记录" % offset)
    payload[offset + OFF_VEL] = velocity
    return payload


def set_pitch(payload, offset, pitch):
    if not 0 <= pitch <= 127:
        raise ValueError("pitch 必须在 0..127")
    payload[offset + OFF_PITCH] = pitch
    return payload


def set_length(payload, offset, ticks):
    struct.pack_into("<I", payload, offset + OFF_LEN, ticks)
    return payload


def set_position(payload, offset, tick):
    struct.pack_into("<I", payload, offset + OFF_POS, tick + POS_ORIGIN)
    return payload


def stride(payload):
    """容器里音符记录的步长；记录不整齐时返回 None。"""
    offs = note_offsets(payload)
    if len(offs) < 2:
        t = tail_offset(payload)
        return t if t % REC32 == 0 else None
    diffs = {offs[i + 1] - offs[i] for i in range(len(offs) - 1)}
    return diffs.pop() if len(diffs) == 1 else None


def append_note(payload, pitch, velocity, tick, length=TICKS_PER_BEAT):
    """在容器末尾追加一个音符（只支持全部记录都是 32 字节的容器）。"""
    if stride(payload) != REC32:
        raise NotImplementedError(
            "该容器的记录不是 32 字节步长，追加会破坏结构；先只用 set_* 就地改")
    rec = bytearray(REC32)
    struct.pack_into("<I", rec, OFF_TYPE, MAGIC)
    struct.pack_into("<I", rec, OFF_POS, tick + POS_ORIGIN)
    rec[10] = 0x41
    rec[OFF_VEL] = velocity
    rec[OFF_PITCH] = pitch
    rec[15] = 0x01
    rec[16] = 0x40
    rec[OFF_SIG] = 0x89
    struct.pack_into("<I", rec, OFF_LEN, length)
    t = tail_offset(payload)
    return payload[:t] + bytes(rec) + payload[t:]


def apply(data, index, payload):
    """把一个容器的新载荷写回 ProjectData（重算块长并封口）。"""
    from . import project as P
    ch = list(P.chunks(data))
    ch[index] = P.with_payload(ch[index], payload)
    return P.seal(ch, P.header(data))


def _selftest():
    ok = 0
    p = bytearray()
    for pitch, vel, pos, ln in ((60, 80, 0, 960), (64, 80, 960, 960), (67, 100, 1920, 480)):
        rec = bytearray(REC32)
        struct.pack_into("<I", rec, 0, MAGIC)
        struct.pack_into("<I", rec, OFF_POS, pos + POS_ORIGIN)
        rec[10] = 0x41
        rec[OFF_VEL] = vel
        rec[OFF_PITCH] = pitch
        rec[15] = 0x01
        rec[16] = 0x40
        rec[OFF_SIG] = 0x89
        struct.pack_into("<I", rec, OFF_LEN, ln)
        p += rec
    p += TAIL_SIG + b"\x00" * 8
    assert is_container(bytes(p)); ok += 1
    assert stride(bytes(p)) == 32; ok += 1
    rows = read_notes(bytes(p))
    assert len(rows) == 3 and rows[0]["pitch"] == 60; ok += 1
    assert rows[2]["velocity"] == 100 and rows[2]["length"] == 480; ok += 1
    set_velocity(p, 0, 127); assert p[OFF_VEL] == 127; ok += 1
    set_pitch(p, 0, 61); assert p[OFF_PITCH] == 61; ok += 1
    set_length(p, 0, 1920)
    assert _u32(p, OFF_LEN) == 1920; ok += 1
    set_position(p, 0, 3840)
    assert _u32(p, OFF_POS) - POS_ORIGIN == 3840; ok += 1
    q = append_note(bytes(p), 72, 90, 2880)
    assert len(q) == len(p) + REC32; ok += 1
    assert len(read_notes(q)) == 4; ok += 1
    assert is_container(q) and q.endswith(TAIL_SIG + b"\x00" * 8); ok += 1
    try:
        append_note(bytes(p)[:-16] + bytes(REC32) + TAIL_SIG + b"\x00" * 8, 60, 90, 0)
    except NotImplementedError:
        ok += 1
    # --- CC ---
    c = bytearray()
    for num, val, pos in ((64, 0, 0), (64, 127, 960), (11, 90, 1920)):
        r = bytearray(CC_REC)
        r[0] = CC_MAGIC
        struct.pack_into("<I", r, OFF_POS, pos + POS_ORIGIN)
        r[OFF_CC_VAL] = val
        r[OFF_CC_NUM] = num
        c += r
    c += TAIL_SIG + b"\x00" * 8
    assert all_cc_container(bytes(c)); ok += 1
    assert not all_cc_container(bytes(p)); ok += 1
    rows = read_ccs(bytes(c))
    assert [r["number"] for r in rows] == [64, 64, 11]; ok += 1
    assert [r["value"] for r in rows] == [0, 127, 90]; ok += 1
    set_cc(c, 0, value=100)
    assert c[OFF_CC_VAL] == 100; ok += 1
    set_cc(c, 0, number=1, tick=3840)
    assert c[OFF_CC_NUM] == 1 and _u32(c, OFF_POS) - POS_ORIGIN == 3840; ok += 1
    d = append_cc(bytes(c), 64, 64, 2880)
    assert len(d) == len(c) + CC_REC and len(read_ccs(d)) == 4; ok += 1
    try:
        append_cc(bytes(p), 64, 64, 0)
    except NotImplementedError:
        ok += 1
    print("%d passed, 0 failed" % ok)


# 入口守卫放到文件末尾（CC 常量在后面定义）


# ---------------------------------------------------------------------------
# 控制器（CC）事件：记录以 0xb0 开头，采样长度为 48 字节
# ---------------------------------------------------------------------------
CC_MAGIC = 0xB0
CC_REC = 48
OFF_CC_NUM = 12
OFF_CC_VAL = 11


def cc_offsets(payload):
    """容器里所有控制器记录的偏移（0xb0 + CC 号在 0..127）。"""
    out = []
    for o in range(0, len(payload) - 16):
        if payload[o] == CC_MAGIC and payload[o + OFF_CC_NUM] <= 127:
            out.append(o)
    return out


def read_ccs(payload):
    rows = []
    for o in cc_offsets(payload):
        rows.append({
            "offset": o,
            "tick": _u32(payload, o + OFF_POS) - POS_ORIGIN,
            "number": payload[o + OFF_CC_NUM],
            "value": payload[o + OFF_CC_VAL],
        })
    return rows


def set_cc(payload, offset, *, value=None, number=None, tick=None):
    if payload[offset] != CC_MAGIC:
        raise ValueError("偏移 %d 不是控制器记录" % offset)
    if value is not None:
        if not 0 <= value <= 127:
            raise ValueError("CC 值必须在 0..127")
        payload[offset + OFF_CC_VAL] = value
    if number is not None:
        if not 0 <= number <= 127:
            raise ValueError("CC 号必须在 0..127")
        payload[offset + OFF_CC_NUM] = number
    if tick is not None:
        struct.pack_into("<I", payload, offset + OFF_POS, tick + POS_ORIGIN)
    return payload


def all_cc_container(payload):
    """整个容器是否都是 48 字节的控制器记录（决定能否安全追加）。"""
    try:
        t = tail_offset(payload)
    except ValueError:
        return False
    return t % CC_REC == 0 and len(cc_offsets(payload)) == t // CC_REC


def append_cc(payload, number, value, tick):
    """追加一个控制器事件；只在全是 48 字节 CC 记录的容器里允许。"""
    if not all_cc_container(payload):
        raise NotImplementedError(
            "该容器里混着音符记录（步长不同），追加 CC 会破坏结构；"
            "现在只支持就地改（set_cc）已有 CC 的数值")
    donor = bytearray(payload[0:CC_REC])          # 以第一条为模板
    donor[0] = CC_MAGIC
    struct.pack_into("<I", donor, OFF_POS, tick + POS_ORIGIN)
    donor[OFF_CC_VAL] = value
    donor[OFF_CC_NUM] = number
    t = tail_offset(payload)
    return payload[:t] + bytes(donor) + payload[t:]

# ---------------------------------------------------------------- 新建区域（donor 法）
REG_REC = 40
REG_POS_ORIGIN = 34560
REG_POS_SCALE = 256
_DONOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "donors")


def _donor(name):
    with open(os.path.join(_DONOR_DIR, name), "rb") as f:
        return f.read()


def _registry_index(ch):
    hits = []
    for i, c in enumerate(ch):
        p = c.payload
        if c.tag != "qSvE" or len(p) < 96:
            continue
        if (len(p) - TAIL_SIZE) % REG_REC or p[-TAIL_SIZE:-TAIL_SIZE + 8] != TAIL_SIG:
            continue
        n = (len(p) - TAIL_SIZE) // REG_REC
        if n % 2:
            continue
        if all(p[k * REG_REC] == 0x20 for k in range(0, n, 2)) and all(
                p[k * REG_REC] == 0 and p[k * REG_REC + 14:k * REG_REC + 16] == b"\x06\x8a"
                for k in range(1, n, 2)) and p[(n - 2) * REG_REC + 14:(n - 2) * REG_REC + 16] == b"\x20\x81":
            hits.append(i)
    if len(hits) != 1:
        raise ValueError("区域注册表候选数 %d，应为 1：%r" % (len(hits), hits))
    return hits[0]


def add_region(data, *, tick=0, length=3840, name=None):
    """在已有区域的工作区上新增一个 MIDI 区域。

    三块对象与登记表的一对记录都**整块取自 donor**（Logic 自己写的），
    代码只改写位置、长度、名字 —— 不碰任何序号。
    """
    from . import project as P
    ch = list(P.chunks(data))
    head = P.header(data)

    # 1) 登记表：克隆最后一对，只改位置，标记搬到新的一条
    ri = _registry_index(ch)
    p = bytearray(ch[ri].payload)
    n = (len(p) - TAIL_SIZE) // REG_REC
    last = (n - 2) * REG_REC
    pair = bytearray(_donor("region_regpair.bin"))
    struct.pack_into("<I", pair, 5, (tick + REG_POS_ORIGIN) // REG_POS_SCALE)
    _k0 = len([c for c in ch if c.tag == "qeSM" and c.length == 303]) - 5
    if _k0 > 0:
        struct.pack_into("<I", pair, 32, struct.unpack_from("<I", pair, 32)[0] + 4 * _k0)
    struct.pack_into("<H", p, last + 14, 0)
    ch[ri] = P.with_payload(ch[ri], bytes(p[:len(p) - TAIL_SIZE]) + bytes(pair) + bytes(p[len(p) - TAIL_SIZE:]))

    # 2) 三块：整块取自 donor，只改长度/起点/名字
    tri = _donor("region_triple.bin")
    robj = P.Chunk(pos=0, tag="qeSM", length=int.from_bytes(tri[28:32], "little"), raw=tri[:36 + int.from_bytes(tri[28:32], "little")])
    o = 36 + int.from_bytes(tri[28:32], "little")
    krow = P.Chunk(pos=0, tag="karT", length=0, raw=tri[o:o + 36])
    o2 = o + 36
    cont = P.Chunk(pos=0, tag="qSvE", length=int.from_bytes(tri[o2 + 28:o2 + 32], "little"), raw=tri[o2:])

    rp = bytearray(robj.payload)
    nm = (name or "Region").encode("utf-8")
    rp[16] = len(nm)
    rp[17] = 0
    rp[18:18 + len(nm)] = nm
    struct.pack_into("<I", rp, 83, length * 256)
    rp[85] = (length // 256) & 0xFF
    struct.pack_into("<I", rp, 247, tick * 256)
    robj = P.with_payload(robj, bytes(rp))

    # donor 自带一套序号；同文件第二次起整体后移，否则两个对象撞号会被丢弃
    k = len([c for c in ch if c.tag == "qeSM" and c.length == 303]) - 5
    if k > 0:
        def _bump(c, dblk, dpay, pay_off=()):
            r = bytearray(c.raw)
            struct.pack_into("<I", r, 8, ((struct.unpack_from("<I", r, 8)[0] >> 16) + dblk) << 16)
            for off in pay_off:
                struct.pack_into("<I", r, 36 + off, struct.unpack_from("<I", r, 36 + off)[0] + dpay)
            return P.Chunk(pos=0, tag=c.tag, length=c.length, raw=bytes(r))
        robj = _bump(robj, 4 * k, k, (8, 196))
        krow = _bump(krow, 4 * k, 0)
        cont = _bump(cont, 4 * k, 0, (12,))
    at = [i for i, c in enumerate(ch) if c.tag == "qSvE" and c.length == 80][-1] + 1
    return P.seal(ch[:at] + [robj, krow, cont] + ch[at:], head)


# ---------------------------------------------------------------- 移动区域
REG_TICK_OFF = 247          # 区域对象载荷 +247 = tick * 256
REG_LEN_OFF = 83            # 区域对象载荷 +83  = 长度 * 256
REG_NAME_LEN = 16           # +16 = 名字字节数，+18 起是名字


def regions(data):
    """列出所有区域对象：`[(下标, 名字, tick, 长度)]`，按 tick 升序。

    `qeSM:303` 里除了真正的区域还有**一个诱饵对象**（名字 `TRASH`、长度字段
    写成 `16662015`），它是 Logic 的临时/回收对象，不是区域。判据是长度字段
    `+83` 必须落在合理范围（≤ 1,000,000 tick）。
    """
    from . import project as P
    ch = P.chunks(data)
    out = []
    for i, c in enumerate(ch):
        if c.tag != "qeSM" or c.length != 303:
            continue
        p = c.payload
        n = p[REG_NAME_LEN]
        if not (1 <= n <= 12):
            continue
        ln = struct.unpack_from("<I", p, REG_LEN_OFF)[0] / 256.0
        if ln > 1_000_000:
            continue
        out.append((i, bytes(p[18:18 + n]),
                    struct.unpack_from("<I", p, REG_TICK_OFF)[0] / 256.0, ln))
    out.sort(key=lambda r: r[2])
    return out


def move_region(data, *, tick, name=None, index=0):
    """把一个已有 MIDI 区域搬到 `tick`。只改两处，其余字节不动。

    1. 区域对象载荷 `+247`（u32）= `tick * 256`；
    2. 区域登记表里配对该区域的那条记录 `+5`（u32）= `(tick + 34560) / 256`
       （`+34560` 是登记表的原点偏移，`/256` 是它的刻度，与 `add_region` 同一口径）。

    登记表记录与区域对象的配对**不靠 ID**，靠位置：记录的 `+5` 与区域对象的
    `+247` 是同一个 tick 的两种刻度，实测 5 个区域 5 条记录一一对应。
    所以搬完必须两处一起改，只改一处 Logic 会把区域放回原处。

    验收：`_MR14`（5 区域）搬第 2 个区域 → Logic `logic_list_regions` 读到新小节。
    """
    from . import project as P
    ch = list(P.chunks(data))
    head = P.header(data)
    regs = regions(data)
    if name is not None:
        hits = [r for r in regs if r[1] == name.encode("utf-8")[:12]]
        if len(hits) != 1:
            raise ValueError("区域名 %r 命中 %d 个" % (name, len(hits)))
        rob = hits[0]
    else:
        if not (0 <= index < len(regs)):
            raise ValueError("区域下标越界：%d / %d" % (index, len(regs)))
        rob = regs[index]

    ri, _nm, old_tick, _ln = rob
    if abs(old_tick - round(old_tick)) > 1e-6:
        raise ValueError("区域起点不是整数 tick：%r" % old_tick)
    old_tick = int(round(old_tick))

    # 1) 区域对象
    p = bytearray(ch[ri].payload)
    struct.pack_into("<I", p, REG_TICK_OFF, int(tick) * 256)
    ch[ri] = P.with_payload(ch[ri], bytes(p))

    # 2) 登记表配对记录
    reg_i = _registry_index(ch)
    rp = bytearray(ch[reg_i].payload)
    n = (len(rp) - TAIL_SIZE) // REG_REC
    want = (old_tick + REG_POS_ORIGIN) // REG_POS_SCALE
    new = (int(tick) + REG_POS_ORIGIN) // REG_POS_SCALE
    hits = [k for k in range(0, n, 2)
            if struct.unpack_from("<I", rp, k * REG_REC + 5)[0] == want]
    if len(hits) != 1:
        raise ValueError("登记表里 tick=%d 的记录命中 %d 条" % (old_tick, len(hits)))
    struct.pack_into("<I", rp, hits[0] * REG_REC + 5, new)
    ch[reg_i] = P.with_payload(ch[reg_i], bytes(rp))

    return P.seal(ch, head)


# ---------------------------------------------------------------- 复制区域
def copy_region(data, *, index=0, tick, name=None):
    """把一个已有区域复制到 `tick`（同轨），带着它自己的音符。

    实现走 `add_region` 那条**已验证**的路（donor 造对象、登记配对、序号自洽），
    然后把新区域的音符容器换成源区域的：容器结构就是
    `[32 字节一条的音符记录...][16 字节尾巴]`（实测 `contlen = 32×音符数 + 16`），
    音符位置是**相对区域起点**的（5 个区域的首音符 tick 都≈0），
    所以整段搬过去不用改任何坐标。

    注意：`add_region` 会给 donor 音符做一次音高顺移（每个新区域 +1 半音），
    这里**直接覆盖**掉整段音符，所以复制出来的音高与源区域一致。
    """
    from . import project as P
    regs = regions(data)
    if not (0 <= index < len(regs)):
        raise ValueError("区域下标越界：%d / %d" % (index, len(regs)))
    ri, src_name, _src_tick, src_len = regs[index]
    ch = list(P.chunks(data))
    src = ch[ri + 2].payload
    if ch[ri + 2].tag != "qSvE":
        raise ValueError("区域 %d 后面不是音符容器" % index)
    notes = src[:tail_offset(src)]
    nm = name if name is not None else src_name.decode()

    out = add_region(data, tick=tick, length=int(round(src_len)), name=nm)
    ch2 = list(P.chunks(out))
    want = [r for r in regions(out)
            if int(r[2]) == int(tick) and r[1] == nm.encode("utf-8")[:12]]
    if len(want) != 1:
        raise ValueError("新建区域定位失败：命中 %d 个" % len(want))
    ri2 = want[0][0]
    donor = ch2[ri2 + 2].payload
    ch2[ri2 + 2] = P.with_payload(ch2[ri2 + 2], notes + donor[tail_offset(donor):])
    return P.seal(ch2, P.header(out))


def _selftest_regions(template=None):
    """在真实工程上验区域读/移/复制（需要模板文件，默认不进 `_selftest`）。

    `template` 给 `.logicx` 或 ProjectData 路径；默认找 `_templates/_MR14`。
    """
    import os
    ok = 0
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if template is None:
        template = os.path.join(root, "_templates", "_MR14.logicx",
                                "Alternatives", "000", "ProjectData")
    if os.path.isdir(template):
        template = os.path.join(template, "Alternatives", "000", "ProjectData")
    if not os.path.exists(template):
        print("skip: 没有模板 %s" % template)
        return 0
    data = open(template, "rb").read()
    regs = regions(data)
    assert len(regs) >= 2, regs
    ok += 1
    names = {r[1] for r in regs}
    assert b"TRASH" not in names  # 诱饵对象不能当区域
    ok += 1
    # 移动：起点变了、长度没变、块数不变
    target = int(regs[0][2]) + 3840
    moved = move_region(data, index=0, tick=target)
    assert len(moved) == len(data)
    r2 = regions(moved)
    assert len(r2) == len(regs)
    assert int(r2[0][2]) == target and int(r2[0][3]) == int(regs[0][3])
    ok += 1
    # 复制：多一个区域、名字与音符跟着来
    src = regs[1]
    copied = copy_region(data, index=1, tick=int(src[2]) + 96 * 3840, name="COPY-U")
    r3 = regions(copied)
    assert len(r3) == len(regs) + 1
    hit = [r for r in r3 if r[1] == b"COPY-U"]
    assert len(hit) == 1 and int(hit[0][3]) == int(src[3])
    ok += 1
    # 音符整段搬过来：容器大小一致（32×音符数 + 16）
    ch = list(__import__("logicfmt.project", fromlist=["x"]).chunks(copied))
    assert len(ch[hit[0][0] + 2].payload) >= 16
    ok += 1
    print("%d passed, 0 failed (真实工程)" % ok)
    return ok


if __name__ == "__main__":
    _selftest()
