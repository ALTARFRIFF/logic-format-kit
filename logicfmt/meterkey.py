# -*- coding: utf-8 -*-
"""Logic Pro 12.4 `.logicx` / ProjectData —— 拍号与调号（含任意小节变化）

实测依据：7 个工程、13 条调号记录、9 条拍号记录；位置公式经用户用 Logic 打开验证。

存储位置
--------
两者都在同一个 `qSvE` 块（记作 qSvE#0）的载荷里。
    块头 magic "qSvE"，长度 u32 LE @ +28，载荷起点 = 块起点 + 36
载荷内先是「拍号块」序列，再是「调号记录」序列，末尾 16 字节是 `f1 00 00 00 …` 尾巴。

拍号块 = 48 字节，以 B 为块起点
    B+0   u32   恒 48（块长）
    B+5   u32   **位置 = (tick + 38400) / 256**
    B+11  u8    分母的 2 的对数（2 -> 4，3 -> 8）
    B+12  u8    分子
    B+23  u8    恒 0x88
    B+24  u8    小节号 − 1；首块（默认拍号）为 0xF6 哨兵
    B+29  u32   位置（与 B+5 重复，两处都要写）
    B+39  u8    恒 0x88
    定位：B 处 4 字节 == 30 00 00 00 且 B+23 == B+39 == 0x88

    !!! 只写 B+24 而不写 B+5/B+29 是错的：Logic 按位置放事件，
        小节号只是缓存。写错会表现为「事件整体前移」并且被吸附到最近的小节线。

调号记录 = 32 字节
    +0  u32   恒 0x32
    +4  u32   **位置 = tick + 38400**（不除以 256）；首条默认记录为 0
    +8  u32   恒 0
    +12 u32   调号编码
    +23 u8    恒 0x88
    定位：载荷 +i 处 4 字节 == 32 00 00 00 且 +i+23 == 0x88

调号编码
    bit4  (0x10)       小调
    低 4 位            升降号数 + 7（7 = 无升降，9 = 2 升，5 = 2 降）
    bit31 (0x80000000) Disable ♭♯
    例：C major 0x07、D major 0x09、F# minor 0x1a、A minor 0x17、Bb major+关♭♯ 0x80000005

单位
    1 拍 = 960 tick；一小节 = 分子 × 4 / 分母 拍
    位置原点 38400 tick = 40 拍（Logic 内部起点，实测恒定）

顺序规则
    调号会询问「是否把和弦轨一起变调」。**先写调号，再写和弦。**
"""

import struct
from fractions import Fraction

QTVE_MAGIC = b"qSvE"
HB = 36
METER_BLOCK = 48
KEY_SIZE = 32
POS_ORIGIN = 38400
UNITS = 960

METER_POS_SCALE = 256          # 拍号位置字段的除数
DEFAULT_BAR = 0xF6
KEY_MAGIC = b"\x32\x00\x00\x00"
MB_MAGIC = b"\x30\x00\x00\x00"


# ---------------------------------------------------------------- 定位
def find_qsve0(data):
    """返回含拍号块序列的那个 qSvE 的 (块起点, 载荷起点, 载荷长度)。"""
    q, i = -1, 0
    while True:
        i = data.find(QTVE_MAGIC, i)
        if i < 0:
            break
        n = struct.unpack_from("<I", data, i + 28)[0]
        pay = data[i + HB:i + HB + n]
        if meter_blocks(pay):
            return i, i + HB, n
        i += 1
    raise ValueError("找不到含拍号块的 qSvE")


def meter_blocks(payload):
    """-> [dict(B, pos, den_exp, num, bar)]；第一项是默认拍号块。"""
    out = []
    for B in range(len(payload) - METER_BLOCK):
        if (payload[B:B + 4] == MB_MAGIC
                and payload[B + 23] == 0x88
                and payload[B + 39] == 0x88):
            out.append(dict(
                B=B,
                pos=struct.unpack_from("<I", payload, B + 5)[0],
                den_exp=payload[B + 11],
                num=payload[B + 12],
                bar=payload[B + 24],
            ))
    return out


def key_records(payload):
    """-> [dict(off, pos, code)]；第一项是默认调号。"""
    out = []
    for i in range(len(payload) - KEY_SIZE):
        if payload[i:i + 4] == KEY_MAGIC and payload[i + 23] == 0x88:
            out.append(dict(off=i,
                            pos=struct.unpack_from("<I", payload, i + 4)[0],
                            code=struct.unpack_from("<I", payload, i + 12)[0]))
    return out


# ---------------------------------------------------------------- 写入
def _meter_pos(tick):
    """位置字段的粒度是 256 tick，用**向上取整**。

    取整后写入的位置落在 [tick, tick+255]，也就是**始终在目标小节内部**；
    Logic 打开时会把它吸附到所在小节的小节线。若向下取整，可能落到
    tick-128（上一小节末），从而被吸附到**前一**小节。
    """
    return -(-(tick + POS_ORIGIN) // METER_POS_SCALE)


def _key_pos(tick):
    return tick + POS_ORIGIN


def set_meter(buf, pay_start, B, num, den, bar, tick):
    """就地改写一条拍号块（长度不变）。五个字段全写。"""
    if den & (den - 1):
        raise ValueError("分母必须是 2 的幂：%r" % (den,))
    if not 1 <= num <= 255:
        raise ValueError("分子越界：%r" % (num,))
    if not 1 <= bar <= 0xFE:
        raise ValueError("小节越界：%r" % (bar,))
    pos = _meter_pos(tick)
    o = pay_start + B
    struct.pack_into("<I", buf, o + 5, pos)
    buf[o + 11] = den.bit_length() - 1
    buf[o + 12] = num
    buf[o + 24] = bar - 1
    struct.pack_into("<I", buf, o + 29, pos)
    return o


def set_key(buf, pay_start, off, tick, code):
    """就地改写一条调号记录（长度不变）。"""
    o = pay_start + off
    struct.pack_into("<I", buf, o + 4, _key_pos(tick))
    struct.pack_into("<I", buf, o + 12, code & 0xFFFFFFFF)
    return o


def key_code(accidentals, minor=False, disable_bp=False):
    """accidentals：升为正、降为负（-7..7）。"""
    if not -7 <= accidentals <= 7:
        raise ValueError("升降号数必须在 -7..7：%r" % (accidentals,))
    v = (accidentals + 7) & 0x0F
    if minor:
        v |= 0x10
    if disable_bp:
        v |= 0x80000000
    return v


def decode_key(code):
    lo = code & 0x0F
    return dict(accidentals=(lo - 7) if 0 <= lo <= 14 else None,
                minor=bool(code & 0x10),
                disable_bp=bool(code & 0x80000000),
                raw=code)


# ---------------------------------------------------------------- 拍位换算
def beats_per_bar(num, den):
    """一小节 = 多少个四分音符（Logic 的 BPM 计数单位）。"""
    return Fraction(num * 4, den)


def build_rows(meters):
    """meters = [(起始小节, 分子, 分母)] -> 含第 1 小节 4/4 的升序表。"""
    rows = [(1, 4, 4)] + sorted(meters)
    return rows


def beats_at_bar(rows, bar):
    """零点到 `bar` 小节线之间的总拍数。"""
    total = Fraction(0)
    for k, (start, num, den) in enumerate(rows):
        end = rows[k + 1][0] if k + 1 < len(rows) else None
        if end is None or end > bar:
            return total + beats_per_bar(num, den) * (bar - start)
        total += beats_per_bar(num, den) * (end - start)
    return total


def tick_at_bar(rows, bar):
    t = beats_at_bar(rows, bar) * UNITS
    if t.denominator != 1:
        raise ValueError("小节线不在整 tick 上：%s 拍" % (beats_at_bar(rows, bar),))
    return int(t)


def bar_of_tick(rows, tick):
    """把 tick 反查成 (小节, 小节内拍数)——用于回读校验。"""
    acc = Fraction(0)
    for k, (start, num, den) in enumerate(rows):
        bpb = beats_per_bar(num, den)
        end = rows[k + 1][0] if k + 1 < len(rows) else None
        span = None if end is None else bpb * (end - start)
        if span is None or acc + span > Fraction(tick, UNITS):
            within = Fraction(tick, UNITS) - acc
            return start + int(within // bpb), float(within % bpb)
        acc += span
    raise ValueError("tick 超出行表")


# ---------------------------------------------------------------- 自检
def _selftest():
    # 调号编码：低 4 位 = 升降号数 + 7，bit4 = 小调
    assert key_code(0, False) == 0x07          # C major
    assert key_code(2, False) == 0x09          # D major
    assert key_code(-3, False) == 0x04         # Eb major
    assert key_code(0, True) == 0x17           # A minor
    assert key_code(3, True) == 0x1a           # F# minor
    assert key_code(-2, False, True) == 0x80000005   # Bb major + Disable b/#
    assert decode_key(0x07)["accidentals"] == 0
    assert decode_key(0x1a)["accidentals"] == 3 and decode_key(0x1a)["minor"]
    assert decode_key(0x80000005)["accidentals"] == -2
    assert decode_key(0x80000005)["disable_bp"]

    # 拍号位置：ceil((tick+38400)/256)，_T-MK / _P2 实测
    for tick, want in ((3840, 165), (7680, 180), (21120, 233),
                       (40320, 308), (55680, 368)):
        assert _meter_pos(tick) == want, (tick, _meter_pos(tick), want)

    # 调号位置：tick+38400，不除以 256
    for tick, want in ((11040, 49440), (25920, 64320), (44160, 82560),
                       (3840, 42240)):
        assert _key_pos(tick) == want, (tick, _key_pos(tick), want)

    # 小节 -> tick，含拍号分段累加（_T-MK 的布局）
    rows = build_rows([(3, 7, 8), (7, 5, 4), (11, 2, 2), (15, 6, 4)])
    assert tick_at_bar(rows, 1) == 0, tick_at_bar(rows, 1)
    assert tick_at_bar(rows, 3) == 7680, tick_at_bar(rows, 3)
    assert tick_at_bar(rows, 7) == 21120, tick_at_bar(rows, 7)
    assert tick_at_bar(rows, 11) == 40320, tick_at_bar(rows, 11)
    assert tick_at_bar(rows, 15) == 55680, tick_at_bar(rows, 15)
    assert bar_of_tick(rows, 11040)[0] == 4, bar_of_tick(rows, 11040)
    print("meterkey selftest OK")


if __name__ == "__main__":
    _selftest()
