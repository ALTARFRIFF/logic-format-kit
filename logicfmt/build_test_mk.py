# -*- coding: utf-8 -*-
"""用 `_SIG-C.logicx` 做底，就地改写全部拍号块与调号记录，产出 `_T-MK.logicx`。

`_SIG-C` 自带 4 个拍号块 + 3 条调号记录，正好够用，所以**不插入任何字节**：
只改块内数值与位置，文件长度完全不变。

用法：python3 logicfmt/build_test_mk.py [--dry-run]
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meterkey as MK

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(HERE, "_templates")
if not os.path.isdir(TPL):
    TPL = os.path.join(HERE, "templates")
SRC = os.path.join(TPL, "_SIG-C.logicx")
DST = os.path.join(TPL, "_T-MK.logicx")
PD = os.path.join("Alternatives", "000", "ProjectData")

METERS = [(3, 7, 8), (7, 5, 4), (11, 2, 2), (15, 6, 4)]
KEYS = [(4, -3, False, False, "Eb major"),
        (8, +4, True, False, "C# minor"),
        (12, -2, False, True, "Bb major + Disable b/#")]


def main():
    dry = "--dry-run" in sys.argv
    if not os.path.isdir(SRC):
        raise SystemExit("缺少底稿：%s" % SRC)
    if os.path.isdir(DST):
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)

    path = os.path.join(DST, PD)
    data = bytearray(open(path, "rb").read())
    n0 = len(data)

    q, ps, plen = MK.find_qsve0(data)
    payload = bytes(data[ps:ps + plen])
    blocks = [b for b in MK.meter_blocks(payload) if b["bar"] != MK.DEFAULT_BAR]
    keys = [k for k in MK.key_records(payload) if k["pos"] != 0]
    print("底稿 qSvE@%d  载荷 %dB  拍号块 %d  调号记录 %d"
          % (q, plen, len(blocks), len(keys)))
    if len(blocks) < len(METERS) or len(keys) < len(KEYS):
        raise SystemExit("槽位不足")

    rows = MK.build_rows(METERS)

    print("\n拍号：")
    for k, blk in enumerate(blocks):
        bar, num, den = METERS[k]
        tick = MK.tick_at_bar(rows, bar)
        pos = MK._meter_pos(tick)
        print("  第 %2d 小节 -> %d/%d   tick=%6d   pos=%4d   (回读 tick %d)"
              % (bar, num, den, tick, pos, pos * 256 - MK.POS_ORIGIN))
        if not dry:
            MK.set_meter(data, ps, blk["B"], num, den, bar, tick)

    print("\n调号：")
    for k, (bar, acc, minor, disable, label) in enumerate(KEYS):
        code = MK.key_code(acc, minor=minor, disable_bp=disable)
        tick = MK.tick_at_bar(rows, bar)
        print("  第 %2d 小节 -> %-24s code=0x%08x  tick=%6d"
              % (bar, label, code, tick))
        if not dry:
            MK.set_key(data, ps, keys[k]["off"], tick, code)

    if dry:
        print("\n[dry-run] 未写盘")
        return

    open(path, "wb").write(data)
    src = open(os.path.join(SRC, PD), "rb").read()
    diff = sum(1 for a, b in zip(src, data) if a != b)
    print("\n写入完成：%d -> %d 字节（delta %d），%d 个字节不同"
          % (n0, len(data), len(data) - n0, diff))

    d2 = open(path, "rb").read()
    _q, ps2, pl2 = MK.find_qsve0(d2)
    p2 = d2[ps2:ps2 + pl2]
    print("\n回读（Logic 打开后应当看到的位置）：")
    for b in MK.meter_blocks(p2):
        if b["bar"] == MK.DEFAULT_BAR:
            print("  默认 %d/%d" % (b["num"], 1 << b["den_exp"]))
            continue
        tick = b["pos"] * 256 - MK.POS_ORIGIN
        print("  %d/%d  pos=%d  tick=%d" % (b["num"], 1 << b["den_exp"], b["pos"], tick))
    for k in MK.key_records(p2):
        if k["pos"] == 0:
            print("  默认调号 code=0x%08x（全局）" % k["code"])
            continue
        dec = MK.decode_key(k["code"])
        bar, beat = MK.bar_of_tick(rows, k["pos"] - MK.POS_ORIGIN)
        acc = dec["accidentals"]
        nm = ("%d 个%s" % (abs(acc), "升" if acc > 0 else "降")) if acc else "无升降"
        print("  调号 code=0x%08x %s %s%s -> 第 %d 小节 + %g 拍"
              % (k["code"], "小调" if dec["minor"] else "大调", nm,
                 " Disable b/#" if dec["disable_bp"] else "", bar, beat))


if __name__ == "__main__":
    main()
