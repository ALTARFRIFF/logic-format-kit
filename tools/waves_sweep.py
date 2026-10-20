#!/usr/bin/env python3
"""Perturbation sweep: find which XML token drives which Audio Unit parameter.

The plug-in is the only authority on its own serialisation. Instead of guessing at
the layout of a state blob, this changes ONE token, hands the blob back to the
plug-in through kAudioUnitProperty_ClassInfo, and asks the plug-in what its
parameters now read. Whatever moved is what that token controls.

Tokens are rewritten at the SAME character width so every outer length field in
the blob (u32 total, u32 segment) stays valid — the blob is never re-serialised,
only byte-patched, exactly like the .cst writes.

usage:  python3 waves_sweep.py <type> <subtype> <manufacturer> <base.plist> <out.tsv>
"""

import plistlib
import re
import subprocess
import sys

TOOL = "./tools/au_probe"
KEY = "Waves_XPst"
FIELDS_PARAMS = ('<Parameters',)


def blob_of(plist_path):
    with open(plist_path, "rb") as fh:
        d = plistlib.load(fh)
    if KEY not in d:
        raise SystemExit("%s has no %s key (have: %s)" % (plist_path, KEY, sorted(d)))
    return d, d[KEY]


def token_spans(blob):
    """Yield (start, end) of each value token inside the Parameters element."""
    s = blob.decode("utf-8", "surrogateescape")
    i = s.find("<Parameters")
    if i < 0:
        raise SystemExit("no <Parameters> element in the blob")
    gt = s.find(">", i)
    end = s.find("</Parameters>", gt)
    if gt < 0 or end < 0:
        raise SystemExit("unterminated <Parameters> element")
    spans = []
    for m in re.finditer(r"\S+", s[gt + 1:end]):
        spans.append((gt + 1 + m.start(), gt + 1 + m.end()))
    return s, spans


def perturb(tok):
    """Return a same-width, clearly different spelling of tok, or None."""
    if tok in ("*", ""):
        return None
    for pos, ch in enumerate(tok):
        if ch.isdigit():
            new = str((int(ch) + 5) % 10)
            if new == ch:
                new = str((int(ch) + 1) % 10)
            if pos == 0 and new == "0":
                new = str((int(ch) + 1) % 10)
                if new == "0":
                    new = "7"
            return tok[:pos] + new + tok[pos + 1:]
    return None


def dump(au, plist_path=None):
    cmd = [TOOL] + au + (["--load", plist_path] if plist_path else []) + ["--dump"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    rows = {}
    for line in p.stdout.splitlines():
        if not re.match(r"^\d+\s", line):
            continue
        f = line.split("\t")
        if len(f) >= 4:
            rows[int(f[0])] = (f[2], f[3])
    return rows, p.returncode, p.stderr.strip()


def main(argv):
    if len(argv) < 6:
        raise SystemExit(__doc__)
    au = argv[1:4]
    base_plist, out_tsv = argv[4], argv[5]

    base, blob = blob_of(base_plist)
    text, spans = token_spans(blob)
    print("tokens: %d   blob: %d bytes" % (len(spans), len(blob)))

    ref, rc, err = dump(au)
    if not ref:
        raise SystemExit("cannot read baseline parameters (rc=%d) %s" % (rc, err))
    print("parameters: %d" % len(ref))

    hits = []
    tmp = "/tmp/waves_pert.plist"
    for idx, (a, b) in enumerate(spans):
        old = text[a:b]
        new = perturb(old)
        if new is None or new == old:
            continue
        patched = (text[:a] + new + text[b:]).encode("utf-8", "surrogateescape")
        if len(patched) != len(blob):
            print("  !! token %d changed width, skipped" % idx)
            continue
        d = dict(base)
        d[KEY] = patched
        with open(tmp, "wb") as fh:
            plistlib.dump(d, fh, fmt=plistlib.FMT_BINARY)
        got, rc2, err2 = dump(au, tmp)
        if rc2 == 6:
            print("  token %2d %-24s -> blob REJECTED" % (idx, old))
            continue
        moved = [(i, got[i][0], ref[i][1], got[i][1])
                 for i in ref
                 if i in got and got[i][1] != ref[i][1]]
        if moved:
            for i, nm, before, after in moved:
                print("  token %3d %-24s -> param %2d %-24s %s -> %s"
                      % (idx, old, i, nm, before, after))
                hits.append((idx, old, new, i, nm, before, after))
        else:
            hits.append((idx, old, new, None, "", "", ""))

    with open(out_tsv, "w") as fh:
        fh.write("token\told\tnew\tparam_index\tparam_name\tbefore\tafter\n")
        for h in hits:
            fh.write("\t".join("" if x is None else str(x) for x in h) + "\n")
    mapped = sum(1 for h in hits if h[3] is not None)
    print("\n%d tokens swept, %d moved a parameter, %d silent -> %s"
          % (len(hits), mapped, len(hits) - mapped, out_tsv))


if __name__ == "__main__":
    main(sys.argv)
