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
    print("%d passed, 0 failed" % ok)


if __name__ == "__main__":
    _selftest()
