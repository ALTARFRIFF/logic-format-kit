# -*- coding: utf-8 -*-
"""用 `_CHORD-D.logicx` 做底，就地改写和弦记录，产出 `_T-CHORD.logicx`。

底稿有 15 条记录、每条 1 小节、从第 1 小节连续排布，正好当槽位。
**不插入任何字节**：只改记录内的字段，文件长度完全不变。

用法：python3 logicfmt/build_test_chord.py [--dry-run]
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chord as CH

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(HERE, "_templates")
if not os.path.isdir(TPL):
    TPL = os.path.join(HERE, "templates")
SRC = os.path.join(TPL, "_CHORD-D.logicx")
DST = os.path.join(TPL, "_T-CHORD.logicx")
PD = os.path.join("Alternatives", "000", "ProjectData")

BAR = 3840

# (小节, 根音, 性质, 低音, 长度(bar)), None = No Chord
SPEC = [
    (1, "G",  "maj",  None, 1),
    (2, "A",  "m7",   None, 1),
    (3, "F#", "m7b5", None, 1),
    (4, "Bb", "7",    None, 1),
    (5, "Eb", "maj",  "G",  1),
    (6, "C",  (0, 2, 4, 6, 7, 10), None, 1),
    (7, None, None,   None, 1),
    (8, "A",  "5",    None, 2),
]


def main():
    dry = "--dry-run" in sys.argv
    if os.path.isdir(DST):
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    path = os.path.join(DST, PD)
    data = bytearray(open(path, "rb").read())
    n0 = len(data)

    offs = CH.find_records(data)
    print("底稿 %d 条和弦记录" % len(offs))
    if len(offs) < 8:
        raise SystemExit("槽位不足")

    for i, (bar, root, qual, bass, bars) in enumerate(SPEC):
        off = offs[i]
        tick = (bar - 1) * BAR
        if root is None:
            rec = CH.write_no_chord(data, off, start=tick, length=bars * BAR)
        else:
            rec = CH.write_record(
                data, off, start=tick, length=bars * BAR,
                root=CH.note_index(root),
                ivs=CH.intervals(qual),
                bass=CH.note_index(bass) if bass else CH.NO_PITCH)
        print("  第 %2d 小节  %-10s  mask=%04x" % (bar, CH.describe(rec), rec["mask"]))

    for i in range(len(SPEC), len(offs)):
        CH.write_no_chord(data, offs[i], start=i * BAR, length=BAR)

    if dry:
        print("[dry-run] 未写盘")
        return

    open(path, "wb").write(data)
    src = open(os.path.join(SRC, PD), "rb").read()
    diff = sum(1 for a, b in zip(src, data) if a != b)
    print("\n写入完成：%d -> %d 字节（delta %d），%d 个字节不同"
          % (n0, len(data), len(data) - n0, diff))

    print("\n回读：")
    for rec in CH.read_records(open(path, "rb").read()):
        print("  第 %5.2f 小节  %d 拍  %s"
              % (rec["start"] / BAR + 1, rec["length"] // 960, CH.describe(rec)))


if __name__ == "__main__":
    main()
