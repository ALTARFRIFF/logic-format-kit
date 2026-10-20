# -*- coding: utf-8 -*-
"""logicfmt.mixer — 通道条与发送：推子、声像、静音、独奏、输出路由、Send。

所有偏移都是 **chunk 载荷相对偏移**（载荷起点 = chunk 起点 + 36）。

定位法则（在本目录 `_templates/` 的 6 个工程上实测）：
* 排列区的一条轨 = 一个 `OCuA` 头（长度 201/209/237/249，载荷 +97 起是名字）
  紧跟它自己的 `UCuA(76)`（每条 Send 一个）与 `UCuA(303)/UCuA(348)`（通道条覆盖）。
* 环境区的 Input/Aux/Output/Bus 对象形状相同但**不是**排列轨，按名字前缀排除。
* Aux 与 Stack 主轨是 `ivnE(467)`，不是 `OCuA`。
"""
from __future__ import annotations
import math, re, struct
try:
    from . import project as P
except ImportError:            # 允许 `python3 mixer.py selftest` 直接跑
    import project as P

HEADER_LENS = (201, 209, 237, 249)
ENV_PREFIX = ("Input", "Aux", "Output", "Bus", "Master", "Monitor", "Preview")
NAME_REL = 97

OFF_VOL, OFF_SOLO, OFF_PAN, OFF_MUTE, OFF_OUT = 85, 88, 89, 90, 92
OFF_SEND_POST, OFF_SEND_LEVEL, OFF_SEND_DEST, OFF_SEND_LEVEL2 = 16, 17, 20, 27

NEG_INF = float("-inf")


def db_to_byte(db: float) -> int:
    """推子 dB → 0..127。实测律：int(90 * 10**(dB/40))，向下取整。0 = -∞。"""
    if db is None or db == NEG_INF:
        return 0
    return max(0, min(127, int(90.0 * (10.0 ** (db / 40.0)))))


def byte_to_db(byte: int) -> float:
    """0..127 → dB。0 返回 -inf。低端受格距限制（-40 以下一格约 3 dB）。"""
    if byte <= 0:
        return NEG_INF
    return 40.0 * math.log10(byte / 90.0)


def pan_to_byte(pan: float) -> int:
    """-64(左) .. 0 .. +63(右) → 0..127，64 = 居中。"""
    return max(0, min(127, int(round(pan + 64))))


def byte_to_pan(byte: int) -> int:
    return int(byte) - 64


class Track:
    __slots__ = ("name", "hdr", "chunks", "index")

    def __init__(self, name, hdr, chunks, index):
        self.name, self.hdr, self.chunks, self.index = name, hdr, chunks, index

    @property
    def payload(self) -> bytes:
        return self.hdr.payload

    @property
    def sends(self):
        return [c for c in self.chunks if c.tag == "UCuA" and c.length == 76]

    @property
    def strip(self):
        return [c for c in self.chunks if c.tag == "UCuA" and c.length in (303, 348)]

    def __repr__(self):
        return f"<Track {self.name} vol={byte_to_db(self.payload[OFF_VOL]):.1f}dB sends={len(self.sends)}>"


def _name_of(chunk):
    for n in P.names_in(chunk):
        n = n.strip()
        if n:
            return n
    return None


def tracks(data: bytes) -> list:
    """按文件顺序返回排列区的轨道。"""
    ch = P.chunks(data)
    hdr = []
    for i, c in enumerate(ch):
        if c.tag != "OCuA" or c.length not in HEADER_LENS:
            continue
        if len(c.payload) <= NAME_REL:
            continue
        nm = _name_of(c)
        if not nm or nm.startswith(ENV_PREFIX):
            continue
        hdr.append((i, nm))
    out = []
    for k, (i, nm) in enumerate(hdr):
        j = hdr[k + 1][0] if k + 1 < len(hdr) else len(ch)
        out.append(Track(nm, ch[i], ch[i:j], len(out)))
    return out


def find(tracks_, name: str) -> Track:
    hits = [t for t in tracks_ if t.name == name]
    if not hits:
        raise KeyError(f"没有轨道 {name!r}（有：{[t.name for t in tracks_]}）")
    if len(hits) > 1:
        raise KeyError(f"{name!r} 有 {len(hits)} 条")
    return hits[0]


# ---- 单字段读写 -----------------------------------------------------------
def _set_u8(track: Track, off: int, val: int) -> None:
    track.hdr.raw[P.CHUNK_HEADER + off] = val & 0xFF


def set_volume(track: Track, db: float) -> int:
    b = db_to_byte(db)
    _set_u8(track, OFF_VOL, b)
    return b


def set_pan(track: Track, pan: int) -> int:
    b = pan_to_byte(pan)
    _set_u8(track, OFF_PAN, b)
    return b


def set_mute(track: Track, on: bool) -> int:
    cur = track.payload[OFF_MUTE]
    new = (cur | 1) if on else (cur & ~1)
    _set_u8(track, OFF_MUTE, new)
    return new


def set_solo(track: Track, on: bool) -> int:
    cur = track.payload[OFF_SOLO]
    new = (cur | 1) if on else (cur & ~1)
    _set_u8(track, OFF_SOLO, new)
    return new


def set_output(track: Track, dest) -> int:
    """dest: 'Stereo Out' / 'No Output' / 'Bus 3' / 3。编码：0=Stereo Out，0xFF=No Output，bus+1。"""
    if isinstance(dest, str):
        d = dest.strip().lower()
        if d in ("stereo out", "stereo output", "master"):
            v = 0
        elif d in ("no output", "none"):
            v = 0xFF
        else:
            m = re.fullmatch(r"bus\s*(\d+)", d)
            if not m:
                raise ValueError(f"看不懂的输出目的地 {dest!r}")
            v = int(m.group(1)) + 1
    else:
        v = int(dest) + 1
    _set_u8(track, OFF_OUT, v)
    return v


def get_output(track: Track):
    v = track.payload[OFF_OUT]
    if v == 0:
        return "Stereo Out"
    if v == 0xFF:
        return "No Output"
    return f"Bus {v - 1}"


# ---- Send ----------------------------------------------------------------
def bus_to_send_dest(bus: int) -> int:
    return int(bus) + 3


def send_dest_to_bus(v: int) -> int:
    return int(v) - 3


def set_send(track: Track, slot: int, db: float = None, bus: int = None) -> dict:
    """改第 slot 条 Send（1 基）。只写已有记录，不创建新记录。"""
    s = track.sends
    if not (1 <= slot <= len(s)):
        raise IndexError(f"{track.name} 只有 {len(s)} 条 Send，没有第 {slot} 条")
    c = s[slot - 1]
    p = c.raw
    res = {"slot": slot}
    if db is not None:
        b = db_to_byte(db)
        p[P.CHUNK_HEADER + OFF_SEND_LEVEL] = b
        p[P.CHUNK_HEADER + OFF_SEND_LEVEL2] = b
        res["level_byte"] = b
    if bus is not None:
        p[P.CHUNK_HEADER + OFF_SEND_DEST] = bus_to_send_dest(bus)
        res["dest_byte"] = bus_to_send_dest(bus)
    return res


def read_sends(track: Track) -> list:
    out = []
    for k, c in enumerate(track.sends, 1):
        p = c.payload
        out.append({
            "slot": k,
            "post": bool(p[OFF_SEND_POST] & 1),
            "level_byte": p[OFF_SEND_LEVEL],
            "level_db": byte_to_db(p[OFF_SEND_LEVEL]),
            "dest_byte": p[OFF_SEND_DEST],
            "bus": send_dest_to_bus(p[OFF_SEND_DEST]),
        })
    return out


def snapshot(data: bytes) -> dict:
    return {t.name: {
        "volume_db": byte_to_db(t.payload[OFF_VOL]),
        "pan": byte_to_pan(t.payload[OFF_PAN]),
        "mute": bool(t.payload[OFF_MUTE] & 1),
        "solo": bool(t.payload[OFF_SOLO] & 1),
        "output": get_output(t),
        "sends": read_sends(t),
    } for t in tracks(data)}


# ---- 自检 ----------------------------------------------------------------
T = "_templates/"


def _read(name):
    return open(f"{T}{name}.logicx/Alternatives/000/ProjectData", "rb").read()


def _selftest():
    ok = fail = 0

    def chk(cond, what):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("FAIL", what)

    # 推子律
    for db, b in [(0, 90), (6, 127), (-3, 75), (-6, 63), (-12, 45), (-20, 28),
                  (-30, 16), (-40, 9), (-60, 2), (NEG_INF, 0)]:
        chk(db_to_byte(db) == b, f"db_to_byte({db}) == {b}")
    # 反向
    chk(abs(byte_to_db(90)) < 0.01, "byte_to_db(90) == 0")
    chk(abs(byte_to_db(63) + 6.19) < 0.05, "byte_to_db(63) ~ -6")
    # 声像
    chk(pan_to_byte(0) == 64 and pan_to_byte(-64) == 0 and pan_to_byte(63) == 127, "pan_to_byte")

    try:
        d = _read("_FADER")
        tr = tracks(d)
        got = {t.name: t.payload[OFF_VOL] for t in tr}
        exp = {"Audio 1": 90, "Audio 2": 127, "Audio 3": 75, "Audio 4": 63, "Audio 5": 45,
               "Audio 6": 28, "Audio 7": 16, "Audio 8": 9, "Audio 9": 2, "Audio 10": 0}
        for k, v in exp.items():
            chk(got.get(k) == v, f"_FADER {k} byte == {v} (got {got.get(k)})")
    except FileNotFoundError:
        print("skip _FADER")

    try:
        d = _read("_ROUTE")
        snap = snapshot(d)
        exp = {"Audio 1": "Stereo Out", "Audio 2": "Bus 3", "Audio 3": "Bus 5",
               "Audio 4": "Bus 7", "Audio 5": "No Output", "Audio 6": "Bus 9"}
        for k, v in exp.items():
            chk(snap[k]["output"] == v, f"_ROUTE {k} output == {v} (got {snap[k]['output']})")
    except FileNotFoundError:
        print("skip _ROUTE")

    try:
        d = _read("_SEND")
        tr = tracks(d)
        all_sends = [s for t in tr for s in read_sends(t)]
        chk(len(all_sends) >= 11, f"_SEND 至少 11 条 Send (got {len(all_sends)})")
        lv = sorted({s["level_byte"] for s in all_sends})
        chk({90, 63, 45, 28, 16, 9, 2, 0} <= set(lv), f"_SEND 电平值覆盖 (got {lv})")
        buses = sorted({s["bus"] for s in all_sends})
        chk(3 in buses and 7 in buses, f"_SEND 目的地含 Bus 3/7 (got {buses})")
    except FileNotFoundError:
        print("skip _SEND")

    try:
        d = _read("_SOLO-A")
        snap = snapshot(d)
        chk(snap["Audio 3"]["solo"] and snap["Audio 7"]["solo"], "_SOLO-A Audio3/7 soloed")
        chk(not snap["Audio 6"]["solo"], "_SOLO-A Audio6 not soloed")
        chk(not any(snap[k]["mute"] for k in snap), "_SOLO-A 无静音")
    except FileNotFoundError:
        print("skip _SOLO-A")

    try:
        d = _read("_MIX-A")
        snap = snapshot(d)
        chk(abs(snap["Audio 2"]["volume_db"] + 20) < 0.5, "_MIX-A Audio2 ~ -20 dB")
        chk(snap["Audio 3"]["pan"] == -64 and snap["Audio 4"]["pan"] == 63, "_MIX-A pan 极值")
        chk(snap["Audio 5"]["mute"] is True, "_MIX-A Audio5 静音")
        chk(snap["Audio 8"]["output"] == "Bus 5", "_MIX-A Audio8 -> Bus 5")
    except FileNotFoundError:
        print("skip _MIX-A")

    print(f"{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
