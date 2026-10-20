#!/usr/bin/env python3
"""
mkpatch.py -- build an audio-track channel-strip patch from the two template
patches, by cutting plug-in blocks out of them and writing parameters.

    mkpatch.py <spec.json> [--dry-run] [--out DIR]

The template patches (built by hand in Logic, one .cst each) are the source of
every plug-in BLOCK.  We never synthesise a plug-in state: we copy the block
Logic itself wrote, drop the ones we do not need, renumber the slots, and then
write parameter values into the copied states.

spec.json
---------
{
  "name": "D Kick In",
  "chain": ["Gain", "FabFilter Pro-Q 4", "SPL Transient Designer Plus",
            "FabFilter Pro-C 3", "Black Box HG-2", "bx_meter"],
  "params": {
      "FabFilter Pro-Q 4": {"Band 1 Used": 1, "Band 1 Frequency": 25, "hz": true},
      "SPL Transient Designer Plus": {"Attack": 0.60},
      "...": {}
  }
}

`chain` names plug-ins by their display name as it appears in the template; the
order given IS the slot order (first = insert 1 = most upstream).

Parameter values are given in the plug-in's OWN units (the unit `au_dump`
prints).  Frequency parameters store log2(Hz); pass the parameter's own name
plus the Hz value and set "hz": true for the entry to convert.

Every write is equal-length byte surgery, so the output `.cst` has exactly the
size the cut-and-paste produced: nothing shifts.
"""

from __future__ import annotations

import json
import math
import os
import plistlib
import struct
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import au_state as A
import au_batch as B

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHIPPED = os.path.join(_HERE, os.pardir, "templates", "patch")
PATCH_ROOT = (os.environ.get("LFK_PATCH_ROOT")
              or (os.path.abspath(_SHIPPED)
                  if os.path.isdir(os.path.join(_SHIPPED, "_TEMPLATE-A.patch"))
                  else os.path.expanduser("~/Music/Audio Music Apps/Patches/Audio")))
TEMPLATE_A = os.path.join(PATCH_ROOT, "_TEMPLATE-A.patch/#Root.cst")
TEMPLATE_B = os.path.join(PATCH_ROOT, "_TEMPLATE-B.patch/#Root.cst")

# display name -> (which template, chunk tag).  The 4CC inside the block is the
# real identity; these names are only what a human types in the spec.
DISPLAY = {
    "NrP6": "ERA 6 Noise Remover Pro",
    "MdC6": "ERA 6 Mouth De-Clicker",
    "SPDP": "SPL De-Verb Plus",
    "NCTG": "Nectar 4 Gate",
    "FQ4p": "FabFilter Pro-Q 4",
    "SPTP": "SPL Transient Designer Plus",
    "FC3p": "FabFilter Pro-C 3",
    "FPMb": "FabFilter Pro-MB",
    "FPSr": "FabFilter Pro-DS",
    "bxmt": "bx_meter",
    "uABM": "Unfiltered Audio Bass Mint",
    "bx8j": "bx_clipper",
    "BAH2": "Black Box HG-2",
    "SPTT": "SPL TwinTube",
    "krc1": "elysia karacter mix",
    "bxf3": "bx_refinement V3",
    "bxa2": "bx_saturator V2",
    "bsle": "bx_console SSL 4000 E",
    "MEQ4": "Maag EQ4",
    "mus1": "elysia museq mix",
    "Soma": "Knif Audio Soma",
}
GAIN = "Gain"          # Logic's own gain plug-in: a non-plist block
BY_NAME = {v.lower(): k for k, v in DISPLAY.items()}


def load_templates():
    """{4CC or 'Gain': (slot, total, bytes)} for every plug-in block, both templates."""
    out = {}
    for path in (TEMPLATE_A, TEMPLATE_B):
        data = open(path, "rb").read()
        chunks = list(A.cst_chunks(data))
        for off, total, tag in chunks[1:-1]:
            slot = struct.unpack_from(">Q", data, off + 12)[0] // 256
            if slot >= 16:
                continue                      # name / strip-level blocks
            body = data[off:off + total]
            fcc = _fourcc(body)
            if not fcc:
                # Logic's own plug-ins carry no plist; identify them by the
                # display name Logic writes into the block.
                fcc = next((d for d in ("Gain",) if d.encode() in body), None)
            out[fcc or "?slot%d" % slot] = (slot, total, body)
    return out


def _fourcc(body: bytes):
    if b"</plist>" not in body:
        return None
    s0 = body.find(b"<?xml")
    e0 = body.find(b"</plist>") + 8
    d = plistlib.loads(body[s0:e0])
    if "subtype" not in d:
        return None
    return struct.pack(">I", d["subtype"]).decode("latin1")


def resolve(name: str) -> str:
    key = name.strip().lower()
    if key in BY_NAME:
        return BY_NAME[key]
    if key == "gain":
        return GAIN
    for fcc, disp in DISPLAY.items():
        if key in disp.lower():
            return fcc
    raise SystemExit("unknown plug-in %r; known: %s"
                     % (name, ", ".join(sorted(set(DISPLAY.values()) | {GAIN}))))


def build(spec: dict, source_path: str | None = None, patch_uuid: str | None = None):
    src = source_path or TEMPLATE_A
    data = open(src, "rb").read()
    blocks = load_templates()

    chunks = list(A.cst_chunks(data))
    header = (chunks[0][0], chunks[0][1], data[chunks[0][0]:chunks[0][0] + chunks[0][1]])
    footer = (chunks[-1][0], chunks[-1][1], data[chunks[-1][0]:chunks[-1][0] + chunks[-1][1]])
    passthrough = []                       # name block + strip-level bplist blocks
    for off, total, tag in chunks[1:-1]:
        body = data[off:off + total]
        slot = struct.unpack_from(">Q", data, off + 12)[0] // 256
        if slot >= 16 or not _fourcc(body) and b"Gain" not in body:
            passthrough.append((slot, off, total, body))
    passthrough.sort(key=lambda t: t[0])

    # --- assemble ---------------------------------------------------------
    out = bytearray(header[2])
    placed = []
    for i, name in enumerate(spec["chain"]):
        fcc = resolve(name)
        if fcc not in blocks:
            raise SystemExit("plug-in %r (%s) is not in either template" % (name, fcc))
        _slot, total, body = blocks[fcc]
        body = bytearray(body)
        # The insert position is recorded TWICE and the two are different fields:
        #   +12  a u64 big-endian = (insert + 1) * 256   -- the slot LABEL
        #   +42  a u32 little-endian = insert             -- the position Logic uses
        # Writing only the first one leaves the plug-in where the template had it,
        # which is exactly how a generated patch ends up with the template's order.
        struct.pack_into(">Q", body, 12, (i + 2) * 256)
        struct.pack_into("<I", body, 42, i)
        out += body
        placed.append((i + 1, fcc, len(out) - total, total))
    for _slot, _o, total, body in passthrough:
        body = bytearray(body)
        if b".patch" in body:
            # The internal name block has a HARD 17-byte field (`_TEMPLATE-A.patch`
            # fills it exactly), so a long patch name cannot go in there.  What
            # Logic shows in the Library is the .patch FOLDER name and what the
            # track is called is data.plist's Channel_name -- neither is limited
            # -- so the block gets `cst_name` and keeps the readable name only as
            # bookkeeping.  Falls back to the full name when it fits.
            body = _rename(body, spec.get("cst_name") or spec["name"], patch_uuid)
        else:
            _reidentify(body)
        out += body
    out += footer[2]

    return out, placed


def _looks_like_uuid(b: bytes) -> bool:
    """True when 16 bytes are a well-formed UUID (version 1-5, variant 8/9/a/b)."""
    return len(b) == 16 and 1 <= (b[6] >> 4) <= 5 and (b[8] & 0xC0) == 0x80


def _reidentify(body: bytearray) -> bytearray:
    """Give a passthrough block its OWN trailing UUID.

    Every block Logic writes ends with a 16-byte UUID, and the STRIP-level
    blocks (the channel-strip bplists, ~4.2/1.8/1.5 kB) carry one too.  Those
    are the strip's identity: a generated patch that copies them verbatim
    declares itself to be the template's strip, and Logic then loads the
    template's inserts and ignores the slot numbers we wrote.  Rewriting them
    is what makes the copy a patch of its own.
    """
    if len(body) >= 16 and _looks_like_uuid(bytes(body[-16:])):
        body[-16:] = uuid.uuid4().bytes
    return body


def _rename(body: bytearray, name: str, patch_uuid: str | None = None) -> bytearray:
    """Rewrite the patch name AND the patch's own UUID inside the 228-byte block.

    The name block carries two identities, not one: the NUL-padded name at
    offset 53 (`_TEMPLATE-A.patch`), and -- in its LAST 16 BYTES, offset
    212..228 -- a UUIDv1 that is the strip's own identity inside the `.cst`.
    It is NOT the same value as `data.plist`'s UUID (measured: template A keeps
    027e8a6c-c804-11f1-... here against 89DBB51D-... in the plist), but it is
    the one that lives in the strip we hand to Logic.  Copying the template's
    block verbatim therefore leaves every generated patch claiming to BE the
    template, which is exactly how a new patch ends up opening the template's
    chain.  Both identities are rewritten here.
    """
    if patch_uuid and len(body) >= 228:
        body[212:228] = uuid.UUID(patch_uuid).bytes
    start = body.find(b".patch")
    if start < 0:
        return body
    # walk back to the first printable char of the name
    s = start
    while s > 36 and 32 <= body[s - 1] < 127:
        s -= 1
    e = start + len(b".patch")
    text = (name + ".patch").encode()
    if len(text) > e - s:
        raise SystemExit("patch name %r does not fit the %d-byte name field"
                         % (name, e - s))
    body[s:e] = text + b"\x00" * (e - s - len(text))
    return body


# ---------------------------------------------------------------------------
def write_params(out: bytearray, spec: dict, dry_run=False):
    """Write every parameter in spec['params'] into the assembled patch."""
    name_of = {disp.lower(): fcc for fcc, disp in DISPLAY.items()}
    want = {}
    for pname, pairs in spec.get("params", {}).items():
        want[pname.strip().lower()] = pairs

    report = []
    states = sorted(list(A.cst_states(bytes(out))), key=lambda s: -s[1])
    for insert, off, total, d in states:
        fcc = struct.pack(">I", d.get("subtype", 0)).decode("latin1")
        disp = DISPLAY.get(fcc, fcc)
        pairs = want.get(disp.lower())
        if pairs is None:
            continue
        key = A.state_key(d)
        raw = bytearray(d[key])
        if raw[:4] == A.FFBS_MAGIC:
            raw = bytearray(raw)
            for k, v in pairs.items():
                if k in ("hz", "db", "float", "band"):
                    continue
                val = float(v)
                note = ""
                if pairs.get("hz") and k.lower().endswith("frequency"):
                    val = math.log2(val)
                    note = "   (log2 %g Hz)" % float(v)
                A.ffbs_set_named(raw, fcc, k, val)
                report.append("  %-24s %-28s = %s%s" % (disp, k, v, note))
        elif raw[:4] == A.PMTR_MAGIC:
            raw = bytearray(raw)
            for k, v in pairs.items():
                if k in ("hz", "db", "float", "band"):
                    continue
                r = A.pmtr_set_named(raw, fcc, k, float(v))
                report.append("  %-24s %-28s = %s" % (disp, k, v))
        elif A.is_sparse(raw):
            raw = bytearray(raw)
            for k, v in pairs.items():
                if k in ("hz", "db", "float", "band"):
                    continue
                A.sparse_set_named(raw, fcc, k, float(v))
                report.append("  %-24s %-28s = %s" % (disp, k, v))
        elif raw[:4] == A.XML_MAGIC:
            for k, v in pairs.items():
                if k in ("hz", "db", "float", "band"):
                    continue
                A.xml_set(raw, k, v, allow_resize=False)
                report.append("  %-24s %-28s = %s" % (disp, k, v))
        else:
            continue
        if not dry_run:
            B._write_cst_state(out, off, total, key, bytes(raw))
    return report


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    spec = json.load(open(argv[1]))
    dry = "--dry-run" in argv
    outdir = None
    if "--out" in argv:
        outdir = argv[argv.index("--out") + 1]

    src = TEMPLATE_A
    if spec.get("source"):
        src = os.path.expanduser(spec["source"])
    new_uuid = str(uuid.uuid4()).upper()
    out, placed = build(spec, src, new_uuid)
    print("chain (%d):" % len(placed))
    for ins, fcc, _o, total in placed:
        print("   insert %-2d  %-4s  %-30s %6d B" % (ins, fcc, DISPLAY.get(fcc, fcc), total))
    print("assembled: %d bytes" % len(out))

    rep = write_params(out, spec, dry)
    print("params written (%d):" % len(rep))
    for line in rep:
        print(line)

    if dry:
        print("\n--dry-run: nothing written")
        return 0

    dest = os.path.join(outdir or PATCH_ROOT, spec["name"] + ".patch")
    cst = os.path.join(dest, "#Root.cst")
    os.makedirs(dest, exist_ok=True)
    with open(cst, "wb") as fh:
        fh.write(out)
    # copy data.plist from the template so Logic finds a complete patch
    src_plist = os.path.join(os.path.dirname(src), "data.plist")
    if os.path.exists(src_plist):
        _write_plist(src_plist, os.path.join(dest, "data.plist"), spec["name"], new_uuid)
    print("\nwrote %s  (%d bytes)" % (cst, len(out)))
    return 0


def _write_plist(src_plist: str, dest_plist: str, name: str, patch_uuid: str | None = None):
    """The template's data.plist, with a NAME and a fresh UUID of its own.

    Logic identifies a patch by the UUID in data.plist and shows the name from
    Channel_name.  Copying the template's file verbatim makes Logic resolve the
    new patch to the template -- it opens the template's strip and the track
    ends up called '_TEMPLATE-A' -- so both of those MUST be rewritten.
    """
    d = plistlib.load(open(src_plist, "rb"))
    for ch in d.get("channels", []):
        ch["Channel_name"] = name
        ch["UUID"] = (patch_uuid or str(uuid.uuid4()).upper())
    with open(dest_plist, "wb") as fh:
        plistlib.dump(d, fh, fmt=plistlib.FMT_BINARY)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
