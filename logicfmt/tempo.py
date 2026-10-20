# -*- coding: utf-8 -*-
"""Logic Pro 12.4 —— 速度（BPM）与任意小节的速度变化

实测依据：`_TL-BASE`（1 档）与 `_TL-TEMPO`（bar 3 = 90、bar 7 = 140）配对差分。

存储位置
--------
属于自己的一个 `qSvE` 块（与拍号/调号那个 qSvE 是**不同的块**）。
载荷布局：

    [默认速度 32 字节][速度事件 64 字节 × N][16 字节尾巴]
    32 + 64*N + 16 = 载荷长度

    `_TL-BASE`  : 48 = 32 + 16            (N = 0)
    `_TL-TEMPO` : 176 = 32 + 2*64 + 16    (N = 2)

默认块与事件共用的头 32 字节
    +0   u32   0x60（96）
    +4   u32   **位置 = tick + 38400**
    +12  u32   0x7f（127）
    +16  u32   **BPM × 10000**（不是 float32）

    BPM 不是浮点：`90.0`/`140.0`/`120.0` 的 float32 在文件里 0 命中。

事件的第 33..63 字节是 Logic 自己的附加字段，本模块不解，直接沿用 donor。

位置基准与拍号/调号相同（`tick + 38400`），所以 `bar 1 = tick 0`。
"""

import os
import struct

QTVE_MAGIC = b"qSvE"
HB = 36
TEMPO_MAGIC = b"\x60\x00\x00\x00"
POS_TAG = b"\x7f\x00\x00\x00"                 # 载荷 +12
TAIL = b"\xf1\x00\x00\x00\xff\xff\xff\x3f" + b"\x00" * 8
TAIL_SIZE = 16
DEFAULT_SIZE = 32
EVENT_SIZE = 64
POS_ORIGIN = 38400
BPM_SCALE = 10000


# ---------------------------------------------------------------- 定位
def _payload_ok(p):
    return (len(p) >= DEFAULT_SIZE + TAIL_SIZE
            and p[:4] == TEMPO_MAGIC
            and p[12:16] == POS_TAG
            and p[-TAIL_SIZE:] == TAIL)


def find_tempo_chunk(data):
    """-> (块起点, 载荷起点, 载荷长度)"""
    i = 0
    while True:
        i = data.find(QTVE_MAGIC, i)
        if i < 0:
            break
        n = struct.unpack_from("<I", data, i + 28)[0]
        if n and _payload_ok(data[i + HB:i + HB + n]):
            return i, i + HB, n
        i += 1
    raise ValueError("找不到速度块（qSvE + 0x60 头 + 0x7f 位置标记）")


def tempo_records(payload):
    """-> [dict(off, size, pos, bpm)]；第一项是默认速度。"""
    if not _payload_ok(payload):
        raise ValueError("载荷不是速度块")
    out = [dict(off=0, size=DEFAULT_SIZE,
                pos=struct.unpack_from("<I", payload, 4)[0],
                bpm=_field_bpm(struct.unpack_from("<I", payload, 16)[0]))]
    off = DEFAULT_SIZE
    end = len(payload) - TAIL_SIZE
    while off + EVENT_SIZE <= end:
        out.append(dict(off=off, size=EVENT_SIZE,
                        pos=struct.unpack_from("<I", payload, off + 4)[0],
                        bpm=_field_bpm(struct.unpack_from("<I", payload, off + 16)[0])))
        off += EVENT_SIZE
    return out


def _field_bpm(v):
    return v / float(BPM_SCALE)


def bpm_field(bpm):
    if not 5 <= bpm <= 990:
        raise ValueError("BPM 越界（5..990）：%r" % (bpm,))
    return int(round(bpm * BPM_SCALE))


def tempo_at(records, tick):
    """取 tick 处生效的 BPM（默认块永远生效于 tick 0）。"""
    cur = records[0]["bpm"]
    for r in records[1:]:
        if r["pos"] - POS_ORIGIN <= tick:
            cur = r["bpm"]
        else:
            break
    return cur


# ---------------------------------------------------------------- 写入
def set_tempo(buf, pay_start, off, tick, bpm):
    """就地改写一条速度记录（长度不变）。"""
    o = pay_start + off
    struct.pack_into("<I", buf, o + 4, tick + POS_ORIGIN)
    struct.pack_into("<I", buf, o + 16, bpm_field(bpm))
    return o


def _donor_event():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "templates", "donors", "tempo_event.bin")
    return open(p, "rb").read()


def build_payload(payload, events):
    """从现有载荷重建：默认档沿用，events = [(tick, bpm)] 升序。"""
    if not _payload_ok(payload):
        raise ValueError("载荷不是速度块")
    donor = payload[DEFAULT_SIZE:DEFAULT_SIZE + EVENT_SIZE]
    if len(donor) != EVENT_SIZE:
        donor = _donor_event()
    head = payload[:DEFAULT_SIZE]
    out = bytearray(head)
    for tick, bpm in sorted(events):
        e = bytearray(donor)
        struct.pack_into("<I", e, 4, tick + POS_ORIGIN)
        struct.pack_into("<I", e, 16, bpm_field(bpm))
        out += e
    out += TAIL
    return bytes(out)


def apply_tempo_map(data, events, *, seal=True):
    """把速度变化表写进 data（bytearray/bytes），返回新的 ProjectData。"""
    from . import project as P
    ch = list(P.chunks(data))
    head = P.header(data)
    for k, c in enumerate(ch):
        if c.tag == "qSvE" and _payload_ok(c.payload):
            pay = build_payload(c.payload, events)
            ch[k] = P.with_payload(c, pay)
            return P.seal(ch, head) if seal else P.rebuild(ch, head)
    raise ValueError("找不到速度块")


# ---------------------------------------------------------------- 自检
def _selftest():
    import glob
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tpl = os.path.join(os.path.dirname(root), "_templates")
    n = 0
    fails = []

    def ck(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            fails.append(msg)

    def load(name):
        return open(os.path.join(tpl, name, "Alternatives", "000", "ProjectData"), "rb").read()

    try:
        base = load("_TL-BASE.logicx")
        tem = load("_TL-TEMPO.logicx")
    except OSError:
        print("skip: 需要 _templates/_TL-BASE.logicx 与 _TL-TEMPO.logicx")
        return

    _, ps, pn = find_tempo_chunk(base)
    rb = tempo_records(base[ps:ps + pn])
    ck(len(rb) == 1, "BASE 应只有默认速度")
    ck(abs(rb[0]["bpm"] - 120.0) < 1e-6, "BASE 默认 120")
    ck(rb[0]["pos"] == POS_ORIGIN, "BASE 默认位置 = 38400")

    _, ps, pn = find_tempo_chunk(tem)
    rt = tempo_records(tem[ps:ps + pn])
    ck(len(rt) == 3, "TEMPO 应有 3 条（默认 + 2）")
    ck(abs(rt[0]["bpm"] - 120) < 1e-6, "TEMPO 默认 120")
    ck(abs(rt[1]["bpm"] - 90) < 1e-6, "bar3 = 90")
    ck(abs(rt[2]["bpm"] - 140) < 1e-6, "bar7 = 140")
    ck(rt[1]["pos"] - POS_ORIGIN == 2 * 3840, "bar3 tick = 7680")
    ck(rt[2]["pos"] - POS_ORIGIN == 6 * 3840, "bar7 tick = 23040")

    ck(abs(tempo_at(rt, 0) - 120) < 1e-6, "tick0 生效 120")
    ck(abs(tempo_at(rt, 2 * 3840) - 90) < 1e-6, "bar3 起 90")
    ck(abs(tempo_at(rt, 6 * 3840) - 140) < 1e-6, "bar7 起 140")

    out = apply_tempo_map(base, [(2 * 3840, 100.0), (5 * 3840, 150.0)])
    ck(len(out) == len(base) + 2 * EVENT_SIZE, "重建后应 +128 字节")
    ch = list(__import__("logicfmt.project", fromlist=["x"]).chunks(out))
    pay = [c.payload for c in ch if c.tag == "qSvE" and _payload_ok(c.payload)][0]
    rr = tempo_records(pay)
    ck(len(rr) == 3, "重建后 3 条")
    ck(abs(rr[1]["bpm"] - 100) < 1e-6, "重建 bar2 = 100")
    ck(abs(rr[2]["bpm"] - 150) < 1e-6, "重建 bar5 = 150")
    ck(abs(tempo_at(rr, 4 * 3840) - 100) < 1e-6, "bar4 生效 100")

    print("%d passed, %d failed" % (n - len(fails), len(fails)))
    for f in fails:
        print("  FAIL:", f)
    if not fails:
        print("OK")


if __name__ == "__main__":
    _selftest()
