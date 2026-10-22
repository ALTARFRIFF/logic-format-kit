#!/usr/bin/env python3
"""pairdiff.py — 两份 ProjectData 的「结构 + 字节」差异报告。

用途：取真值。把同一个工程在 Logic 里保存两次（中间只做**一处**改动），
再用本脚本比较两份 ProjectData，直接得到「那一处」的确切增量。

    python3 tools/pairdiff.py before.bin after.bin
    python3 tools/pairdiff.py before.bin after.bin --payload 0 5 --hex

输出三段：
* **结构**：块序列的插入/删除/替换（块头 36 B，按 (tag, length) 对齐）；
* **字节**：同一位置两块长度相同的，逐字节列出不同的偏移；
* **载荷**：对指定块号做 SequenceMatcher，打印插入/删除的原始字节（十六进制）。

注意：真值必须是**同一工程、同一 Logic 会话**的两次保存，否则 id 重编号与 UUID
抖动会淹没真正的增量（踩过的坑见 docs/PROJECTDATA-FINDINGS.md §64）。
"""
import argparse
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logicfmt.project import chunks  # noqa: E402


def structure(a, b):
    sa = [(c.tag, c.length) for c in a]
    sb = [(c.tag, c.length) for c in b]
    sm = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
    rows = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        rows.append((tag, i1, i2, j1, j2,
                     [(c.tag, c.length) for c in b[j1:j2]],
                     [(c.tag, c.length) for c in a[i1:i2]]))
    return rows


def bytes_diff(a, b, stop=None):
    """只比较插入点之前的块：插入之后 same-index 已经不是同一块了。"""
    out = []
    limit = min(len(a), len(b)) if stop is None else min(len(a), len(b), stop)
    for i in range(limit):
        pa, pb = a[i].payload, b[i].payload
        if len(pa) != len(pb):
            out.append((i, a[i].tag, a[i].length, "len", len(pa), len(pb)))
            continue
        d = [k for k in range(len(pa)) if pa[k] != pb[k]]
        if d:
            out.append((i, a[i].tag, a[i].length, d, None, None))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="比较两份 ProjectData")
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--payload", type=int, nargs="*", default=[],
                    help="对这些块号做载荷级 diff")
    ap.add_argument("--hex", action="store_true", help="载荷 diff 打印十六进制")
    ns = ap.parse_args(argv)

    a = chunks(open(ns.before, "rb").read())
    b = chunks(open(ns.after, "rb").read())
    print(f"before: {ns.before}  {os.path.getsize(ns.before):,} B  {len(a)} 块")
    print(f"after : {ns.after}  {os.path.getsize(ns.after):,} B  {len(b)} 块")
    print(f"size delta: {os.path.getsize(ns.after) - os.path.getsize(ns.before):+,} B")
    print(f"chunk delta: {len(b) - len(a):+d}")

    print("\n== 结构 ==")
    for tag, i1, i2, j1, j2, added, removed in structure(a, b):
        print(f"{tag}  A[{i1}:{i2}] -> B[{j1}:{j2}]")
        for t, l in added:
            print(f"   + {t}:{l}")
        for t, l in removed:
            print(f"   - {t}:{l}")

    ops = structure(a, b)
    stop = min([min(o[1], o[3]) for o in ops if o[0] in ('insert', 'delete')], default=None)
    print("\n== 同位置字节差异 ==" + (f"（只比前 {stop} 块，之后已错位）" if stop is not None else ""))
    for row in bytes_diff(a, b, stop):
        i, tag, length, d, la, lb = row
        if d == "len":
            print(f"  {i} {tag}:{length} 长度 {la} -> {lb}")
        else:
            print(f"  {i} {tag}:{length} 差异 {len(d)} 字节 @ {d[:12]}"
                  + (" ..." if len(d) > 12 else ""))

    for idx in ns.payload:
        if idx >= len(a) or idx >= len(b):
            print(f"\n== 载荷 {idx} == 超出范围")
            continue
        pa, pb = a[idx].payload, b[idx].payload
        print(f"\n== 载荷 {idx} ({a[idx].tag}:{a[idx].length} -> {b[idx].tag}:{b[idx].length}) ==")
        sm = difflib.SequenceMatcher(a=pa, b=pb, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            fb = pb[j1:j2].hex() if ns.hex else repr(pb[j1:j2])
            fa = pa[i1:i2].hex() if ns.hex else repr(pa[i1:i2])
            print(f"  {tag} A[{i1}:{i2}] -> B[{j1}:{j2}]  +{j2 - j1 - (i2 - i1)} B")
            print(f"    A: {fa[:160]}")
            print(f"    B: {fb[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
