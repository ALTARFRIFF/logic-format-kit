# -*- coding: utf-8 -*-
"""Logic Pro 和弦轨（Global Harmonies）区域编解码。

和弦轨上的每个和弦 = 一个 497 字节、名字字面为 ``MIDI Region`` 的对象。
本模块只做**就地改写**（不改文件长度），因此可以安全地在模板里预埋槽位。

记录布局（偏移相对记录起点 R）
--------------------------------------------------------------------------
  R+ 54   名字 "MIDI Region"（12 字节 ASCII）
  R+126   u16 LE   长度（tick）
  R+290   u32 LE   起始位置（tick）
  R+465   u8       半音位图 低 8 位
  R+466   u8       半音位图 高 8 位
  R+468   u8       低音（Bass）音级 0-11，0x0f = 无
  R+470   u8       根音音级 0-11，0x0f = 无和弦
  R+477   u8       bit7 = 音阶 Auto（0x80 = Auto，0x00 = 显式音阶）
  R+478   u8       显示缓存：0x20 | 根音音级；No Chord 为 0x7f

半音位图 ``mask = 465 | (466 << 8)``，bit N 表示「根音上方第 N 个半音」存在。
例：C 大三和弦 C E G = bit 0,4,7 = 0x0091；C 小七 = C E♭ G B♭ = 0x0490。
``mask == 0`` 即 **No Chord**（显式清空，不是删除记录）。

已验证样本：_CHORD-A 25 条、_CHORD-D 15 条、_CHORD-E 15 条，共 55 条零例外；
``_CHORD-TEST`` 把 0x91 改成 0x89 后 Logic 显示 C 小三和弦，确认位图权威。

尚未解出：音阶的具体编码（``R+479``/``R+480`` 是随音阶变化的显示缓存，
但 14 个样本无法给出稳定映射）。写和弦时保持 ``R+477`` bit7 = Auto 即可。
"""

import re

TICKS_PER_BEAT = 960
REC_SIZE = 497
NAME = b"MIDI Region"
NAME_OFF = 54

OFF_LEN = 126
OFF_START = 290
OFF_MASK_LO = 465
OFF_MASK_HI = 466
OFF_BASS = 468
OFF_ROOT = 470
OFF_SCALE = 477
OFF_CACHE = 478

NO_PITCH = 0x0F

PITCH_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

#: 常见和弦性质 -> 相对根音的半音集合
QUALITIES = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "5": (0, 7),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "m7b5": (0, 3, 6, 10),
    "dim7": (0, 3, 6, 9),
    "9": (0, 2, 4, 7, 10),
    "11": (0, 2, 4, 5, 7, 10),
    "13": (0, 2, 4, 7, 9, 10),
}


def note_index(name):
    """``"C"``/``"C#"``/``"Db"``/``"Bb"`` -> 0-11。"""
    n = name.strip().replace("♯", "#").replace("♭", "b")
    table = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    base = table[n[0].upper()]
    for ch in n[1:]:
        if ch == "#":
            base += 1
        elif ch == "b":
            base -= 1
        else:
            raise ValueError("bad note name: %r" % name)
    return base % 12


def intervals(spec, root=0):
    """把 ``"maj"`` 或 ``(0,4,7,14)`` 变成相对半音集合（支持 >11 的延伸音）。"""
    if isinstance(spec, str):
        if spec not in QUALITIES:
            raise ValueError("unknown quality %r (have: %s)"
                             % (spec, ", ".join(sorted(QUALITIES))))
        spec = QUALITIES[spec]
    return sorted({(i % 12) for i in spec})


def mask_from(ivs):
    """位图是**相对根音**的：C maj 与 D maj 都是 0x0091（_CHORD-A 实测）。"""
    m = 0
    for i in ivs:
        m |= 1 << (i % 12)
    return m & 0xFFFF


def find_records(data):
    """返回所有和弦记录的起始偏移。"""
    return [m.start() - NAME_OFF for m in re.finditer(re.escape(NAME), data)]


def read_record(data, off):
    mask = data[off + OFF_MASK_LO] | (data[off + OFF_MASK_HI] << 8)
    return {
        "off": off,
        "start": int.from_bytes(data[off + OFF_START:off + OFF_START + 4], "little"),
        "length": int.from_bytes(data[off + OFF_LEN:off + OFF_LEN + 2], "little"),
        "mask": mask,
        "root": data[off + OFF_ROOT],
        "bass": data[off + OFF_BASS],
        "auto_scale": bool(data[off + OFF_SCALE] & 0x80),
        "cache478": data[off + OFF_CACHE],
    }


def read_records(data):
    return [read_record(data, o) for o in find_records(data)]


def describe(rec):
    """给人类看的一行摘要。"""
    if rec["mask"] == 0:
        return "No Chord"
    return symbol(rec["root"], [k for k in range(12) if rec["mask"] >> k & 1],
                  rec["bass"])


def _cache_for(root):
    return 0x7F if root == NO_PITCH else (0x20 | (root & 0x0F))


def write_record(data, off, *, start=None, length=None, root=None, ivs=None,
                 mask=None, bass=NO_PITCH, auto_scale=True):
    """就地改写一条和弦记录（不改文件长度）。

    ``root`` 与 ``ivs``/``mask`` 二选一；``root=None`` 且 ``mask=0`` 即 No Chord。
    传 ``bass=None`` 表示保持原值。
    """
    if mask is None:
        if root is None:
            raise ValueError("need root+ivs or mask")
        mask = mask_from(ivs if ivs is not None else ())
    if root is None:
        root = data[off + OFF_ROOT] if mask else NO_PITCH
    if mask == 0:
        root = NO_PITCH
        bass = NO_PITCH

    if start is not None:
        data[off + OFF_START:off + OFF_START + 4] = int(start).to_bytes(4, "little")
    if length is not None:
        data[off + OFF_LEN:off + OFF_LEN + 2] = int(length).to_bytes(2, "little")

    data[off + OFF_MASK_LO] = mask & 0xFF
    data[off + OFF_MASK_HI] = (mask >> 8) & 0xFF
    data[off + OFF_ROOT] = root
    data[off + OFF_BASS] = bass if bass is not None else data[off + OFF_BASS]
    data[off + OFF_SCALE] = 0x80 if auto_scale else 0x00
    data[off + OFF_CACHE] = _cache_for(root)
    return read_record(data, off)


def write_no_chord(data, off, *, start=None, length=None):
    return write_record(data, off, start=start, length=length, mask=0)


def chord_at(data, tick):
    """找到覆盖某 tick 的和弦（用于自检「这一拍会显示什么」）。"""
    hits = [r for r in read_records(data)
            if r["start"] <= tick < r["start"] + max(r["length"], 1)]
    return hits[0] if hits else None


_ALTER = {"b5": 6, "#5": 8, "b9": 1, "9": 2, "#9": 3, "11": 5, "#11": 6,
          "b13": 8, "13": 9, "6": 9, "b7": 10, "7": 10, "maj7": 11}

_QUAL_TOKENS = {
    "": "maj", "maj": "maj", "M": "maj", "m": "min", "min": "min", "mi": "min",
    "dim": "dim", "aug": "aug", "+": "aug", "sus2": "sus2", "sus4": "sus4",
    "5": "5", "6": "6", "m6": "m6", "7": "7", "maj7": "maj7", "M7": "maj7",
    "m7": "m7", "min7": "m7", "m7b5": "m7b5", "dim7": "dim7",
    "9": "9", "11": "11", "13": "13",
}


def parse_symbol(text):
    """``"Cmaj7"`` / ``"Dm7b5"`` / ``"F#7(9,#11)"`` / ``"Eb/G"`` -> (root, ivs, bass).

    支持的记法是文档里给出的那个子集：根音 + 性质 + 可选 ``(变音, …)`` + 可选 ``/低音``。
    ``"No Chord"`` -> ``(None, [], None)``。
    """
    s = text.strip()
    if s.lower().replace(" ", "") in ("nochord", "no", "n.c.", "nc"):
        return None, [], None
    bass = None
    if "/" in s:
        s, b = s.rsplit("/", 1)
        bass = note_index(b)
    alts = []
    if "(" in s:
        s, rest = s.split("(", 1)
        alts = [a.strip() for a in rest.rstrip(")").split(",")]
    m = re.match(r"^([A-Ga-g](?:#|b)?)(.*)$", s)
    if not m:
        raise ValueError("cannot parse chord symbol %r" % text)
    root = note_index(m.group(1))
    qtok = m.group(2).replace(" ", "")
    if qtok not in _QUAL_TOKENS:
        raise ValueError("unknown chord quality %r in %r (have: %s)"
                         % (qtok, text, ", ".join(sorted(_QUAL_TOKENS))))
    ivs = set(intervals(_QUAL_TOKENS[qtok]))
    for a in alts:
        if not a:
            continue
        if a not in _ALTER:
            raise ValueError("unknown alteration %r in %r" % (a, text))
        ivs.add(_ALTER[a])
    return root, sorted(ivs), bass


def symbol(root, ivs, bass=NO_PITCH):
    """反向渲染成常见记法（尽力而为；权威表示始终是位图）。"""
    if root is None or root == NO_PITCH:
        return "No Chord"
    S = set(i % 12 for i in ivs)
    name = PITCH_NAMES[root % 12]
    third = 4 if 4 in S else (3 if 3 in S else None)
    fifth = 7 if 7 in S else (6 if 6 in S else (8 if 8 in S else None))
    sev = 11 if 11 in S else (10 if 10 in S else (9 if 9 in S else None))
    used = {0}

    if S == {0, 4, 8}:
        base = "aug"
        used |= {4, 8}
    elif S == {0, 3, 6}:
        base = "dim"
        used |= {3, 6}
    else:
        if third == 4:
            base = ""
            used.add(4)
        elif third == 3:
            base = "m"
            used.add(3)
        elif 2 in S:
            base = "sus2"
            used |= {2, 7}
        elif 5 in S:
            base = "sus4"
            used |= {5, 7}
        elif fifth == 7:
            base = "5"
            used.add(7)
        else:
            base = ""

    out = name + base
    if sev == 11:
        out += "maj7"
        used.add(11)
    elif sev == 10:
        out += "7"
        used.add(10)
    elif sev == 9:
        out += "6"
        used.add(9)
    if base not in ("aug", "dim", "sus2", "sus4", "5"):
        if fifth == 6:
            out += "b5"
            used.add(6)
        elif fifth == 8:
            out += "#5"
            used.add(8)

    tail = [lab for iv, lab in ((1, "b9"), (2, "9"), (3, "#9"), (5, "11"),
                                (6, "#11"), (8, "b13"))
            if iv in S and iv not in used]
    if tail:
        out += "(%s)" % ",".join(tail)
    if bass not in (None, NO_PITCH) and bass % 12 != root % 12:
        out += "/" + PITCH_NAMES[bass % 12]
    return out


def _selftest():
    data = bytearray(1024)
    off = 100
    data[off + NAME_OFF:off + NAME_OFF + 12] = NAME
    r = write_record(data, off, start=3840, length=3840,
                     root=note_index("C"), ivs=intervals("7"))
    assert r["mask"] == 0x0491, hex(r["mask"])
    assert r["root"] == 0 and r["cache478"] == 0x20
    assert describe(r) == "C7", describe(r)

    r = write_record(data, off, root=note_index("D"), ivs=intervals("m7"))
    assert r["mask"] == 0x0489, hex(r["mask"])
    assert r["cache478"] == 0x22
    assert describe(r) == "Dm7", describe(r)

    r = write_record(data, off, root=note_index("C"), ivs=intervals("maj"),
                     bass=note_index("Bb"))
    assert describe(r) == "C/Bb", describe(r)

    r = write_no_chord(data, off)
    assert r["mask"] == 0 and r["root"] == NO_PITCH and r["cache478"] == 0x7F
    assert describe(r) == "No Chord"
    print("chord selftest OK")


if __name__ == "__main__":
    _selftest()
