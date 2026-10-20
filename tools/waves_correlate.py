#!/usr/bin/env python3
"""Correlate XML tokens with Audio Unit parameters across a plug-in's presets.

Swap-free fallback for the perturbation sweep: load every factory preset, record
both the state blob's token list and the plug-in's parameter values, then match
each parameter to the token whose "changed / not changed vs. preset 0" pattern is
identical across the whole preset set. A unique match is a decoded index.

usage: python3 waves_correlate.py <type> <subtype> <manufacturer> [--max N]
"""

import plistlib
import re
import subprocess
import sys

PROBE = "./tools/au_probe"
INFO = "./tools/au_classinfo"
KEY = "Waves_XPst"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def preset_names(au):
    p = run([INFO] + au + ["--presets"])
    names = []
    for line in p.stdout.splitlines():
        # au_classinfo prints "#   [12] Name"; tolerate a bare "12<TAB>Name" too
        m = re.match(r"^\s*#?\s*\[(\d+)\]\s*(.+)$", line) or \
            re.match(r"^\s*(\d+)\t(.+)$", line)
        if m:
            names.append((int(m.group(1)), m.group(2).strip()))
    return names


def blob_tokens(path):
    with open(path, "rb") as fh:
        d = plistlib.load(fh)
    blob = d[KEY]
    s = blob.decode("utf-8", "surrogateescape")
    i = s.find("<Parameters")
    gt = s.find(">", i)
    end = s.find("</Parameters>", gt)
    return re.findall(r"\S+", s[gt + 1:end])


def params(au, plist_path):
    p = run([PROBE] + au + ["--load", plist_path, "--dump"])
    rows = {}
    for line in p.stdout.splitlines():
        if not re.match(r"^\d+\s", line):
            continue
        f = line.split("\t")
        if len(f) >= 4:
            rows[int(f[0])] = (f[2], f[3])
    return rows


def main(argv):
    if len(argv) < 4:
        raise SystemExit(__doc__)
    au = argv[1:4]
    limit = 40
    if "--max" in argv:
        limit = int(argv[argv.index("--max") + 1])

    names = preset_names(au)[:limit]
    if not names:
        raise SystemExit("no presets enumerated")
    print("presets: %d" % len(names))

    toks, pars = [], []
    for idx, nm in names:
        tmp = "/tmp/wc_%d.xml" % idx
        r = run([INFO] + au + ["--preset", str(idx), "--out", tmp])
        if r.returncode != 0:
            print("  preset %d %-30s skipped (rc=%d)" % (idx, nm, r.returncode))
            continue
        toks.append((idx, nm, blob_tokens(tmp)))
        pars.append((idx, nm, params(au, tmp)))
        print("  preset %d %-30s tokens=%d params=%d"
              % (idx, nm, len(toks[-1][2]), len(pars[-1][2])))

    n = len(toks)
    if n < 3:
        raise SystemExit("need at least 3 presets, got %d" % n)

    width = min(len(t[2]) for t in toks)
    print("\ncomparing %d presets over %d tokens" % (n, width))

    def pattern(seq):
        base = seq[0]
        return tuple(1 if seq[k] != base else 0 for k in range(n))

    tpat = [pattern([t[2][i] for t in toks]) for i in range(width)]
    pkeys = sorted(pars[0][2])
    ppat = {}
    for i in pkeys:
        seq = [p[2].get(i, ("", ""))[1] for p in pars]
        ppat[i] = pattern(seq)

    print("%-44s %-6s %s" % ("parameter", "token", "confidence"))
    mapped = 0
    for i in pkeys:
        if sum(ppat[i]) == 0:
            continue
        cand = [t for t in range(width) if tpat[t] == ppat[i]]
        nm = pars[0][2][i][0]
        if len(cand) == 1:
            print("%-44s %-6d unique" % (nm, cand[0]))
            mapped += 1
        elif cand:
            print("%-44s %-6s ambiguous %s" % (nm, "-", cand[:8]))
        else:
            print("%-44s %-6s no-match" % (nm, "-"))
    print("\n%d parameters matched to a unique token" % mapped)


if __name__ == "__main__":
    main(sys.argv)
