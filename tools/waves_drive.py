#!/usr/bin/env python3
"""Map Audio Unit parameters to state-blob token positions by DRIVING the plug-in.

Direction matters. Hand-edited blobs are discarded by some plug-ins (Waves ignores
them completely), so "change a token, read the parameters" tells you nothing. Going
the other way always works: set ONE parameter through the AU API, ask the plug-in
to serialise its own state, and see which token moved.

That token IS that parameter's index, confirmed by the plug-in rather than guessed.

usage: python3 waves_drive.py <type> <subtype> <manufacturer> [--max N] [--out FILE]
"""

import plistlib
import re
import subprocess
import sys

PROBE = "./tools/au_probe"
DUMP = "./tools/au_dump"
KEY = "Waves_XPst"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def tokens_of(path):
    with open(path, "rb") as fh:
        d = plistlib.load(fh)
    blob = d.get(KEY)
    if blob is None:
        return None
    s = blob.decode("utf-8", "surrogateescape")
    i = s.find("<Parameters")
    if i < 0:
        return None
    gt = s.find(">", i)
    end = s.find("</Parameters>", gt)
    return re.findall(r"\S+", s[gt + 1:end])


def param_ranges(au):
    p = run([DUMP] + au + ["--tsv"])
    out = []
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 6 or not f[0].isdigit():
            continue
        try:
            out.append(dict(index=int(f[0]), ident=int(f[1]), name=f[2],
                            lo=float(f[3]), hi=float(f[4]), default=float(f[5])))
        except ValueError:
            continue
    return out


def save_state(au, set_id=None, set_val=None, path="/tmp/wd.xml"):
    cmd = [PROBE] + au + ["--quiet", "--save", path]
    if set_id is not None:
        cmd += ["--set", str(set_id), repr(set_val)]
    r = run(cmd)
    if r.returncode != 0:
        return None
    return path


def main(argv):
    if len(argv) < 4:
        raise SystemExit(__doc__)
    au = argv[1:4]
    out_file = "/tmp/waves_drive.tsv"
    limit = 9999
    if "--out" in argv:
        out_file = argv[argv.index("--out") + 1]
    if "--max" in argv:
        limit = int(argv[argv.index("--max") + 1])

    params = param_ranges(au)[:limit]
    print("parameters: %d" % len(params))

    base_path = save_state(au)
    base = tokens_of(base_path)
    if not base:
        raise SystemExit("no baseline state")
    print("baseline tokens: %d" % len(base))

    rows = []
    for p in params:
        span = p["hi"] - p["lo"]
        if span <= 0:
            continue
        target = p["lo"] + span * 0.73
        if abs(target - p["default"]) < span * 0.05:
            target = p["lo"] + span * 0.31
        path = save_state(au, p["ident"], target, "/tmp/wd_%d.xml" % p["ident"])
        if not path:
            print("  %-30s state read failed" % p["name"])
            continue
        got = tokens_of(path)
        if not got or len(got) != len(base):
            print("  %-30s token count changed (%s vs %s)"
                  % (p["name"], len(got) if got else "?", len(base)))
            continue
        moved = [i for i, (a, b) in enumerate(zip(base, got)) if a != b]
        rows.append((p["ident"], p["name"], target, moved))
        if moved:
            print("  %-30s set %-12g -> tokens %s" % (p["name"], target, moved))
        else:
            print("  %-30s set %-12g -> no token changed (read-only or derived)"
                  % (p["name"], target))

    with open(out_file, "w") as fh:
        fh.write("id\tname\tvalue\ttokens\n")
        for ident, name, val, moved in rows:
            fh.write("%d\t%s\t%g\t%s\n" % (ident, name, val, ",".join(map(str, moved))))
    unique = sum(1 for r in rows if len(r[3]) == 1)
    print("\n%d parameters driven, %d identified a single token -> %s"
          % (len(rows), unique, out_file))


if __name__ == "__main__":
    main(sys.argv)
