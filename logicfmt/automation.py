# -*- coding: utf-8 -*-
"""自动化曲线（音量）：读写大 `qSvE` 里的 16 字节采样记录。

结论来源见 `docs/03-project-timeline.md` 附录 G。要点：

    envelope 容器 = 一个 `qSvE`，载荷 = N × 16 字节记录
    记录 +0    u32 = 0x50
    记录 +4    u32 LE = tick + 38400        ← 位置（采样步长实测 15 tick）
    记录 +8    3 字节，含义未定（写曲线时保持为原值或 0）
    记录 +11   u8 = 音量字节                ← 与推子同一张表：b = 90 * 10 ** (dB/40)

所以 `+11` 就是 dB 值（`mixer.byte_to_db` 可直接换算），位置在 `+4`。
目前只支持**等长改写**（改位置或改值），不支持插入/删除采样点。
"""
import struct

from . import project as P
from . import mixer as X

REC = 16
REC_TAG = 0x50
POS_ORIGIN = 38400
VAL_OFF = 11
POS_OFF = 4
MIN_PAYLOAD = 1024          # 太小的 qSvE 是拍号/调号/marker，不是曲线


# ---------------------------------------------------------------- 定位
def is_envelope(chunk):
    p = chunk.payload
    if chunk.tag != "qSvE" or len(p) < MIN_PAYLOAD or len(p) % REC:
        return False
    return struct.unpack_from("<I", p, 0)[0] == REC_TAG


def find_envelope(chunks):
    hits = [c for c in chunks if is_envelope(c)]
    if not hits:
        return None
    return max(hits, key=lambda c: c.length)


# ---------------------------------------------------------------- 读
def records(data):
    """返回 [{index, tick, byte, db}]。"""
    ch = P.chunks(data)
    c = find_envelope(ch)
    if c is None:
        return []
    p = c.payload
    out = []
    for i in range(len(p) // REC):
        o = i * REC
        tick = struct.unpack_from("<I", p, o + POS_OFF)[0] - POS_ORIGIN
        b = p[o + VAL_OFF]
        out.append({"index": i, "tick": tick, "byte": b, "db": X.byte_to_db(b)})
    return out


# ---------------------------------------------------------------- 写（等长）
def _rewrite(data, fn):
    ch = P.chunks(data)
    head = P.header(data)
    c = find_envelope(ch)
    if c is None:
        raise ValueError("这个工程没有自动化曲线容器")
    raw = bytearray(c.raw)
    n = c.length // REC
    for i in range(n):
        o = P.CHUNK_HEADER + i * REC
        fn(raw, o, i)
    new = P.Chunk(pos=c.pos, tag=c.tag, length=c.length, raw=bytes(raw))
    return P.rebuild([new if x is c else x for x in ch], head)


def set_constant(data, db):
    """把整条曲线拉平到某个 dB（等长改写，只动 +11）。"""
    b = X.db_to_byte(db)

    def fn(raw, o, i):
        raw[o + VAL_OFF] = b
    return _rewrite(data, fn)


def shift_db(data, delta_db):
    """整条曲线上下平移 delta_db（等长改写，只动 +11）。"""

    def fn(raw, o, i):
        cur = X.byte_to_db(raw[o + VAL_OFF])
        raw[o + VAL_OFF] = X.db_to_byte(cur + delta_db)
    return _rewrite(data, fn)


def scale_db(data, factor):
    """以 0 dB 为轴缩放曲线（factor < 1 更平，> 1 更夸张）。"""

    def fn(raw, o, i):
        cur = X.byte_to_db(raw[o + VAL_OFF])
        raw[o + VAL_OFF] = X.db_to_byte(cur * factor)
    return _rewrite(data, fn)


def move(data, shift_ticks):
    """整条曲线在时间上平移（等长改写，只动 +4）。"""

    def fn(raw, o, i):
        v = struct.unpack_from("<I", raw, o + POS_OFF)[0]
        struct.pack_into("<I", raw, o + POS_OFF, v + shift_ticks)
    return _rewrite(data, fn)


def grid_range(data, bar_ticks=3840):
    """返回采样网格覆盖的 (起小节, 止小节) —— 曲线只能写在这个范围内。"""
    rs = [r for r in records(data) if r["tick"] >= 0]
    if not rs:
        return (0, 0)
    return (rs[0]["tick"] // bar_ticks + 1, rs[-1]["tick"] // bar_ticks + 1)


def set_curve(data, points, bar_ticks=3840, default_db=0.0):
    """按 (小节, dB) 断点写一条折线，覆盖采样网格的整个范围。

    points 形如 [(1, 0.0), (5, -12.0), (9, -12.0), (16, 0.0)]，按小节线性插值；
    网格的首尾之外按首/末断点的值保持不变。**只改值字节，等长**。
    """
    if not points:
        raise ValueError("points 不能为空")
    pts = sorted(points)
    lo = pts[0][0]
    hi = pts[-1][0]

    def target(bar):
        if bar <= lo:
            return pts[0][1]
        if bar >= hi:
            return pts[-1][1]
        for (b0, v0), (b1, v1) in zip(pts, pts[1:]):
            if b0 <= bar <= b1:
                if b1 == b0:
                    return v1
                return v0 + (v1 - v0) * (bar - b0) / (b1 - b0)
        return default_db

    def fn(raw, o, i):
        tick = struct.unpack_from("<I", raw, o + POS_OFF)[0] - POS_ORIGIN
        if tick < 0:
            return
        raw[o + VAL_OFF] = X.db_to_byte(target(tick / bar_ticks + 1))
    return _rewrite(data, fn)


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

    a = os.path.join(base, "_TL-AUTO.logicx", "Alternatives", "000", "ProjectData")
    b = os.path.join(base, "_TL-AUTO2.logicx", "Alternatives", "000", "ProjectData")
    if not os.path.exists(a):
        print("markers/automation fixtures 缺失，跳过")
        return True
    da = open(a, "rb").read()
    db_ = open(b, "rb").read()

    ra, rb = records(da), records(db_)
    chk(len(ra) == 1538, "_TL-AUTO 采样点数 1538，实得 %d" % len(ra))
    chk(len(rb) == 1483, "_TL-AUTO2 采样点数 1483，实得 %d" % len(rb))
    chk(ra[0]["db"] == 0.0, "首点应是 0 dB，实得 %.2f" % ra[0]["db"])
    trough_a = min(ra[:-8], key=lambda r: r["db"])["db"]
    trough_b = min(rb[:-8], key=lambda r: r["db"])["db"]
    chk(abs(trough_a + 12.0) < 1.0, "_TL-AUTO 谷值应约 −12 dB，实得 %.2f" % trough_a)
    chk(abs(trough_b + 9.0) < 1.0, "_TL-AUTO2 谷值应约 −9 dB，实得 %.2f" % trough_b)

    d2 = set_constant(da, -6.0)
    chk(len(d2) == len(da), "set_constant 等长")
    vals = {r["byte"] for r in records(d2)}
    chk(vals == {X.db_to_byte(-6.0)}, "拉平后所有采样点同值，实得 %s" % sorted(vals))

    d3 = shift_db(da, -3.0)
    chk(abs(min(r["db"] for r in records(d3)[:-8]) + 15.0) < 1.0,
        "平移 −3 dB 后谷值约 −15 dB")

    lo, hi = grid_range(da)
    chk(lo == 1 and hi >= 5, "网格范围应至少覆盖 bar 1..5，实得 %d..%d" % (lo, hi))

    d5 = set_curve(da, [(1, 0.0), (5, -12.0)])
    got = {}
    for r in records(d5):
        if r["tick"] < 0:
            continue
        got.setdefault(r["tick"] // 3840 + 1, round(r["db"], 1))   # 每小节取第一个采样点
    chk(abs(got.get(2, 99) + 3.0) < 0.5, "折线在 bar 2 应约 −3 dB，实得 %s" % got.get(2))
    chk(abs(got.get(5, 99) + 12.0) < 0.5, "折线在 bar 5 应约 −12 dB，实得 %s" % got.get(5))

    d4 = move(da, 3840)
    chk(records(d4)[0]["tick"] == records(da)[0]["tick"] + 3840, "整体后移一小节")

    print("automation: %d passed, %d failed" % (ok, bad))
    return bad == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
