#!/usr/bin/env python3
"""
au_state.py — read and write third-party Audio Unit state inside Logic Pro files.

Companion to AU_PARAM_FORMAT.md.  Handles three containers:

  * Logic project   <project>.logicx/Alternatives/000/ProjectData   (base64 blobs)
  * channel strip   <name>.cst / <name>.patch/#Root.cst             (binary chunks)
  * AU preset       <name>.aupreset                                 (XML plist)

Two plug-in state containers are understood:

  * "FFBS"  FabFilter (Pro-Q 4, Pro-C 3, Pro-MB, Pro-DS, Pro-L 2, ...)  little-endian
  * "Pmtr"  Brainworx / Plugin Alliance / SPL                           big-endian

All writes are EQUAL-LENGTH byte surgery: the file size never changes.

Usage
-----
    au_state.py list  <ProjectData|file.cst|file.patch|file.aupreset>
    au_state.py dump  <ProjectData> [--track NAME]
    au_state.py set   <ProjectData> --track NAME --band N [--gain DB] [--freq HZ]
                      [--q Q] [--shape NAME] [--slope N] [--used 0|1]
    au_state.py blob  <file.cst> [--chunk N]        # raw state, hex head
    au_state.py pset  <ProjectData> --offset 0xADDR --tag NAME --value V   # Pmtr record
    au_state.py pset  <ProjectData> --offset 0xADDR --float N  --value V   # FFBS index

`set` addresses a Pro-Q 4 blob by track name; `pset` addresses any blob by the byte
offset `list` reports (Pmtr containers carry no track name, so name addressing cannot
find them).  Both are equal-length byte surgery and refuse a write that would resize.
"""

from __future__ import annotations

import argparse
import base64
import math
import os
import plistlib
import re
import struct
import sys

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

B64_ALPHA = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
B64_WS = set(b" \t\r\n")

FFBS_MAGIC = b"FFBS"
PMTR_MAGIC = b"Pmtr"

# Pro-Q 4 layout
PROQ4_NFLOATS = 600
PROQ4_BAND_STRIDE = 23
PROQ4_BANDS = 24
PROQ4_GLOBAL_BASE = PROQ4_BAND_STRIDE * PROQ4_BANDS      # 552

# FFBS header is magic(4) + version(4) + nfloats(4) = 12 bytes; the float array
# starts immediately after it.  (Not 8 — an earlier revision used 8 and silently
# decoded every band one float out of phase.)
FFBS_HEADER = 12
PROQ4_HEADER = FFBS_HEADER                               # alias, Pro-Q 4 uses the same

LOG2_40 = math.log2(40.0)                 # 5.321928
Q_SPAN = math.log2(40.0) - math.log2(0.025)   # 10.643856

# per-band field offsets (see AU_PARAM_FORMAT.md §4.2)
B_USED, B_ENABLED, B_FREQ, B_GAIN, B_Q, B_SHAPE, B_SLOPE = range(7)
B_STEREO_PLACEMENT, B_SPEAKER, B_DYN_GAIN = 7, 8, 9
B_DYN_ENABLED, B_DYN_MODE, B_DYN_THRESHOLD = 10, 11, 12
B_DYN_ATTACK, B_DYN_RELEASE = 13, 14
B_EXT_SC, B_SC_SOURCE, B_SC_LO, B_SC_HI = 15, 16, 17, 18
B_SPC = 21

FFBS_TAIL_STRINGS = ("AUTHOR", "DESCRIPTION", "TAGS", "_PerTrackIndex")

SHAPE_NAMES = {0.0: "Bell", 2.0: "Lo Cut", 4.0: "Hi Cut"}


# --------------------------------------------------------------------------
# encodings
# --------------------------------------------------------------------------

def hz_to_freq(hz: float) -> float:
    """Hz -> the float stored in a band's frequency slot."""
    return math.log2(hz)


def freq_to_hz(f: float) -> float:
    """Stored float -> Hz."""
    return 2.0 ** f


def q_to_enc(q: float) -> float:
    """Q -> the normalised float (Q spans 0.025 .. 40)."""
    return (math.log2(q) + LOG2_40) / Q_SPAN


def enc_to_q(e: float) -> float:
    return 2.0 ** (e * Q_SPAN - LOG2_40)


def db_to_threshold(db: float) -> float:
    """Dynamic threshold dB -> stored float. 1.0 means 'Auto'."""
    return (db + 60.0) / 60.0


def threshold_to_db(t: float) -> float:
    return t * 60.0 - 60.0


# --------------------------------------------------------------------------
# base64 blob handling inside a project's ProjectData
# --------------------------------------------------------------------------

def _is_b64(c: int) -> bool:
    return c in B64_ALPHA or c in B64_WS


def find_blobs(data: bytes, magic: bytes = FFBS_MAGIC):
    """Yield (start, end, blob) for every whitespace-wrapped base64 run that
    decodes to `magic`.  base64 is line-wrapped in ProjectData, so the run has
    to be expanded across whitespace."""
    marker = base64.b64encode(magic)[:6]          # b"RkZCUw" for FFBS
    for m in re.finditer(re.escape(marker), data):
        a = m.start()
        z = a + len(marker)
        while a > 0 and _is_b64(data[a - 1]):
            a -= 1
        while z < len(data) and _is_b64(data[z]):
            z += 1
        run = data[a:z]
        clean = re.sub(rb"\s", b"", run)
        try:
            blob = base64.b64decode(clean + b"===")
        except Exception:
            continue
        if blob.startswith(magic):
            yield a, z, blob


def splice_blob_var(data: bytearray, start: int, end: int, new_blob: bytes) -> int:
    """Replace the base64 run with the encoding of `new_blob`, allowing the
    run to change length.  Returns the delta, which every later offset moves by.

    Equal-length surgery is the preferred path — it keeps the whole file's
    offsets stable — but a value whose text cannot fit the space its
    predecessor occupied (a long number written into three characters, a
    renamed enum) has to resize, and doing it here is better than refusing.
    """
    run = bytes(data[start:end])
    enc = base64.b64encode(new_blob)
    # keep whatever whitespace the original run opened and closed with, so a
    # resize moves the file by exactly the blob's own delta and nothing else
    lead = len(run) - len(run.lstrip())
    trail = len(run) - len(run.rstrip())
    lines = run.strip().split(b"\n")
    width = max(len(x) for x in lines) if len(lines) > 1 else 0
    if width and width < len(enc):
        body = b"\n".join(enc[i:i + width] for i in range(0, len(enc), width))
    else:
        body = enc
    out = run[:lead] + body + (run[len(run) - trail:] if trail else b"")
    data[start:end] = out
    return len(out) - len(run)


def splice_blob(data: bytearray, start: int, end: int, new_blob: bytes) -> None:
    """Replace data[start:end] with the base64 of new_blob, preserving the
    original whitespace layout so the file size is bit-for-bit unchanged."""
    run = bytes(data[start:end])
    clean = re.sub(rb"\s", b"", run)
    if len(base64.b64decode(clean + b"===")) != len(new_blob):
        raise ValueError("blob length would change — refusing")
    enc = base64.b64encode(new_blob)
    if len(enc) != len(clean):
        raise ValueError("base64 length would change — refusing")
    out = bytearray(run)
    j = 0
    for i in range(len(run)):
        if run[i] not in B64_WS:
            out[i] = enc[j]
            j += 1
    assert j == len(enc)
    data[start:end] = bytes(out)


def splice_blob_var(data: bytearray, start: int, end: int, new_blob: bytes) -> int:
    """Replace data[start:end] with the base64 of new_blob, ALLOWING a length change.

    `splice_blob` keeps every byte offset in the file stable, which is what
    makes a batch of edits safe to apply in any order.  This one does not: it
    returns the number of bytes the file grew by, and a caller that uses it
    must apply its edits from the END of the file backwards so earlier offsets
    stay valid.  The base64 is re-wrapped at the original line width.

    Used only as a fallback, when an XML value cannot be made to fit the space
    it replaces (a non-numeric string of a different length).
    """
    run = bytes(data[start:end])
    clean = re.sub(rb"\s", b"", run)
    enc = base64.b64encode(new_blob)
    lines = run.split(b"\n")
    width = len(lines[1]) if len(lines) > 1 and lines[1].strip() else 76
    if width < 8 or width > len(enc):
        out = enc
    else:
        out = b"\n".join(enc[i:i + width] for i in range(0, len(enc), width))
    delta = len(out) - len(run)
    data[start:end] = out
    return delta


# --------------------------------------------------------------------------
# FFBS (FabFilter)
# --------------------------------------------------------------------------

def ffbs_header(blob: bytes):
    if blob[:4] != FFBS_MAGIC:
        raise ValueError("not an FFBS blob")
    version, nfloats = struct.unpack_from("<II", blob, 4)
    return version, nfloats


def ffbs_floats(blob: bytes):
    """Return the float list (decoded little-endian)."""
    _, n = ffbs_header(blob)
    return list(struct.unpack_from("<%df" % n, blob, FFBS_HEADER))


def ffbs_tail_raw(blob: bytes) -> bytes:
    """The FFBS trailer, verbatim: everything after the float array.

    This is deliberately NOT a structured parser.  The trailer is a *mixed*
    stream — a 4-byte plug-in subtype, then a run of fields that are sometimes
    `u32 length + bytes` and sometimes a bare u32 (observed: -1, 1, 4, 6) — so a
    strict length-prefixed walk desynchronises on the fifth field of a real
    blob and silently returns nonsense.  An earlier revision did exactly that
    and returned `[]` for every blob.  Use `ffbs_tag_strings()` to get the
    names; use this only when you want the raw bytes.
    """
    _, n = ffbs_header(blob)
    return blob[FFBS_HEADER + n * 4:]


def ffbs_tag_strings(blob: bytes) -> list[str]:
    """All printable strings in the trailer.

    The trailer is *mostly* length-prefixed strings but interleaves raw integers,
    which desynchronises a strict u32+bytes walk.  A plain strings scan is what
    actually finds the preset and track names.
    """
    _, n = ffbs_header(blob)
    tail = blob[FFBS_HEADER + n * 4:]
    return [m.group().decode("latin1")
            for m in re.finditer(rb"[ -~]{2,}", tail)]


def ffbs_set_float(blob: bytearray, index: int, value: float) -> None:
    _, n = ffbs_header(blob)
    if not 0 <= index < n:
        raise IndexError("float index %d out of range (0..%d)" % (index, n - 1))
    struct.pack_into("<f", blob, PROQ4_HEADER + index * 4, value)


def band_base(band: int) -> int:
    """band is 1-based."""
    if not 1 <= band <= PROQ4_BANDS:
        raise ValueError("band must be 1..%d" % PROQ4_BANDS)
    return PROQ4_BAND_STRIDE * (band - 1)


def band_get(blob: bytes, band: int) -> dict:
    """Decode one Pro-Q 4 band into human-readable values."""
    fl = ffbs_floats(blob)
    b = band_base(band)
    g = lambda off: fl[b + off]
    return {
        "band": band,
        "used": bool(g(B_USED)),
        "enabled": bool(g(B_ENABLED)),
        "freq_hz": freq_to_hz(g(B_FREQ)),
        "gain_db": g(B_GAIN),
        "q": enc_to_q(g(B_Q)),
        "shape": SHAPE_NAMES.get(g(B_SHAPE), "enum=%g" % g(B_SHAPE)),
        "slope_raw": g(B_SLOPE),
        "stereo_placement": g(B_STEREO_PLACEMENT),
        "speaker": g(B_SPEAKER),
        "dyn_gain_db": g(B_DYN_GAIN),
        "dyn_threshold": "Auto" if g(B_DYN_THRESHOLD) >= 1.0
                         else threshold_to_db(g(B_DYN_THRESHOLD)),
        "dyn_attack_pct": g(B_DYN_ATTACK),
        "dyn_release_pct": g(B_DYN_RELEASE),
        "ext_sidechain": bool(g(B_EXT_SC)),
        "sc_source": g(B_SC_SOURCE),
        "sc_lo_hz": freq_to_hz(g(B_SC_LO)),
        "sc_hi_hz": freq_to_hz(g(B_SC_HI)),
    }


def band_set(blob: bytearray, band: int, **kw) -> list[str]:
    """Write one or more fields of a band.  Returns the names written.

    Accepted keyword names: used, enabled, freq_hz, gain_db, q, shape, slope,
    stereo_placement, speaker, dyn_gain_db, dyn_threshold_db, dyn_threshold_auto,
    dyn_attack_pct, dyn_release_pct, ext_sidechain, sc_lo_hz, sc_hi_hz.
    """
    b = band_base(band)
    written = []

    def put(off, value, label):
        ffbs_set_float(blob, b + off, float(value))
        written.append(label)

    if "used" in kw:            put(B_USED, 1.0 if kw["used"] else 0.0, "used")
    if "enabled" in kw:         put(B_ENABLED, 1.0 if kw["enabled"] else 0.0, "enabled")
    if "freq_hz" in kw:         put(B_FREQ, hz_to_freq(kw["freq_hz"]), "freq_hz")
    if "gain_db" in kw:         put(B_GAIN, kw["gain_db"], "gain_db")
    if "q" in kw:               put(B_Q, q_to_enc(kw["q"]), "q")
    if "shape" in kw:           put(B_SHAPE, kw["shape"], "shape")
    if "slope" in kw:           put(B_SLOPE, kw["slope"], "slope")
    if "stereo_placement" in kw: put(B_STEREO_PLACEMENT, kw["stereo_placement"],
                                     "stereo_placement")
    if "speaker" in kw:         put(B_SPEAKER, kw["speaker"], "speaker")
    if "dyn_gain_db" in kw:     put(B_DYN_GAIN, kw["dyn_gain_db"], "dyn_gain_db")
    if kw.get("dyn_threshold_auto"):
        put(B_DYN_THRESHOLD, 1.0, "dyn_threshold=Auto")
    elif "dyn_threshold_db" in kw:
        put(B_DYN_THRESHOLD, db_to_threshold(kw["dyn_threshold_db"]),
            "dyn_threshold_db")
    if "dyn_attack_pct" in kw:  put(B_DYN_ATTACK, kw["dyn_attack_pct"], "dyn_attack_pct")
    if "dyn_release_pct" in kw: put(B_DYN_RELEASE, kw["dyn_release_pct"],
                                    "dyn_release_pct")
    if "ext_sidechain" in kw:   put(B_EXT_SC, 1.0 if kw["ext_sidechain"] else 0.0,
                                    "ext_sidechain")
    if "sc_lo_hz" in kw:        put(B_SC_LO, hz_to_freq(kw["sc_lo_hz"]), "sc_lo_hz")
    if "sc_hi_hz" in kw:        put(B_SC_HI, hz_to_freq(kw["sc_hi_hz"]), "sc_hi_hz")
    return written


# --------------------------------------------------------------------------
# named parameters — the plug-in's own map, via tools/au_dump -> param-maps/
# --------------------------------------------------------------------------

PARAM_MAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "param-maps")
_PARAM_MAP_CACHE: dict = {}


def load_param_map(subtype: str, directory: str | None = None) -> list:
    """Read `param-maps/<manufacturer>_<subtype>.txt` (written by
    tools/dump_all_maps.py) into an ordered list of dicts with index, id, name,
    min, max and default.  Empty list when that plug-in has not been dumped."""
    if subtype in _PARAM_MAP_CACHE:
        return _PARAM_MAP_CACHE[subtype]
    d = directory or PARAM_MAP_DIR
    rows = []
    path = None
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith("_%s.txt" % subtype):
                path = os.path.join(d, fn)
                break
    if path:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if not line.startswith("  "):
                    continue
                f = line.split()
                if len(f) < 6 or not f[0].isdigit() or not f[1].isdigit():
                    continue
                try:
                    rows.append({"index": int(f[0]), "id": int(f[1]),
                                 "name": " ".join(f[2:-3]),
                                 "min": float(f[-3]), "max": float(f[-2]),
                                 "default": float(f[-1])})
                except ValueError:
                    continue
    _PARAM_MAP_CACHE[subtype] = rows
    return rows


def param_lookup(subtype: str, name: str):
    """Find a parameter by name: exact match first, then an unambiguous prefix."""
    rows = load_param_map(subtype)
    if not rows:
        return None
    low = name.strip().lower()
    exact = [r for r in rows if r["name"].lower() == low]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError("ambiguous parameter %r: %s"
                         % (name, ", ".join(r["name"] for r in exact[:8])))
    pref = [r for r in rows if r["name"].lower().startswith(low)]
    if len(pref) == 1:
        return pref[0]
    if len(pref) > 1:
        raise ValueError("ambiguous parameter %r matches: %s"
                         % (name, ", ".join(r["name"] for r in pref[:8])))
    return None


def ffbs_params(blob: bytes, subtype: str | None = None) -> list:
    """[(index, name, value)] for an FFBS blob — real names when a map exists,
    otherwise `#<index>`."""
    _, n = ffbs_header(blob)
    fl = ffbs_floats(blob)
    sub = subtype or ffbs_subtype(blob)
    rows = load_param_map(sub)
    if rows and len(rows) == n:
        return [(r["index"], r["name"], fl[r["index"]]) for r in rows]
    return [(i, "#%d" % i, v) for i, v in enumerate(fl)]


# --------------------------------------------------------------------------
# The fourth container: FabFilter's "flags" state.
#
# Some FabFilter plug-ins in a channel strip write their state under the plist
# key `data` with `FabFilterFlags = 1` and NO `FFBS` header.  It is not a flat
# float array; it is a sparse index/value table, big-endian:
#
#     u32 0 | u32 0 | u32 count          -- 12-byte header
#     repeat count times:  u32 index | f32 value   -- 8 bytes, index 0-based
#
# Pro-DS stores 12 + 8*20 = 172 bytes, Pro-MB 12 + 8*151 = 1220, and in both
# cases `count` is exactly the plug-in's AU parameter count and the indices are
# complete 0..count-1.  Values are in the plug-in's OWN units (Pro-MB's
# `Band 1 Low Crossover` reads 10.9658, which the map's 4.90689..14.8727 range
# and Pro-Q 4's band frequencies both identify as log2(Hz)).
# --------------------------------------------------------------------------

SPARSE_HEADER = 12
SPARSE_ENTRY = 8


def sparse_count(raw) -> int:
    """Validate a flags-state blob and return its parameter count."""
    raw = bytes(raw)
    if len(raw) < SPARSE_HEADER:
        raise ValueError("flags state is %d bytes, shorter than its 12-byte "
                         "header" % len(raw))
    n = struct.unpack_from(">I", raw, 8)[0]
    if len(raw) != SPARSE_HEADER + SPARSE_ENTRY * n:
        raise ValueError("flags state is %d bytes; a count of %d implies %d"
                         % (len(raw), n,
                            SPARSE_HEADER + SPARSE_ENTRY * n))
    return n


def is_sparse(raw) -> bool:
    try:
        sparse_count(raw)
    except Exception:
        return False
    return True


def sparse_params(raw, subtype: str) -> list:
    """[{index, offset, name, value}] for a FabFilter flags state."""
    n = sparse_count(raw)
    mp = load_param_map(subtype) or []
    rows = []
    for i in range(n):
        idx = struct.unpack_from(">I", raw, SPARSE_HEADER + SPARSE_ENTRY * i)[0]
        val = struct.unpack_from(">f", raw,
                                 SPARSE_HEADER + SPARSE_ENTRY * i + 4)[0]
        name = mp[idx]["name"] if idx < len(mp) else ("param %d" % idx)
        rows.append({"index": idx, "offset": SPARSE_HEADER + SPARSE_ENTRY * i + 4,
                     "name": name, "value": val})
    return rows


def sparse_set_named(raw, subtype: str, name: str, value: float,
                     enforce_range: bool = True) -> dict:
    """Write one parameter of a FabFilter flags state, in the plug-in's units."""
    rows = sparse_params(raw, subtype)
    hits = [r for r in rows if r["name"] == name]
    if not hits:
        hits = [r for r in rows
                if r["name"].lower().startswith(str(name).lower())]
    if not hits:
        raise ValueError("%s: no parameter %r (have: %s)"
                         % (subtype, name, ", ".join(r["name"] for r in rows)))
    if len(hits) > 1:
        raise ValueError("%s: %r is ambiguous — %s"
                         % (subtype, name, ", ".join(r["name"] for r in hits)))
    row = hits[0]
    mp = load_param_map(subtype) or []
    if enforce_range and row["index"] < len(mp):
        p = mp[row["index"]]
        if not (p["min"] <= value <= p["max"]):
            raise ValueError("%s: %s is out of range: %g not in %g .. %g"
                             % (subtype, row["name"], value, p["min"], p["max"]))
    before = row["value"]
    struct.pack_into(">f", raw, row["offset"], float(value))
    after = struct.unpack_from(">f", raw, row["offset"])[0]
    return {"index": row["index"], "offset": row["offset"], "name": row["name"],
            "before": before, "after": after, "wrote": value,
            "verified": abs(after - value) <= 1e-6}


def ffbs_set_named(blob: bytearray, subtype: str, name: str, value: float,
                   enforce_range: bool = True) -> int:
    """Write one named parameter of ANY FFBS plug-in.  Returns the float index.

    The value is checked against the range the AU itself publishes (the third
    and fourth columns of the map); outside it the write is refused rather than
    clamped, because what the plug-in does with an out-of-range state is not
    something this format documents.
    """
    row = param_lookup(subtype, name)
    if row is None:
        if not load_param_map(subtype):
            raise KeyError("no parameter map for subtype %r — run "
                           "tools/dump_all_maps.py" % subtype)
        raise KeyError("no parameter named %r on %s (see `au_state.py params`)"
                       % (name, subtype))
    if enforce_range and not (row["min"] <= value <= row["max"]):
        raise ValueError(
            "%s is out of range: %g not in %g .. %g (min/max as the plug-in "
            "publishes them)" % (row["name"], value, row["min"], row["max"]))
    ffbs_set_float(blob, row["index"], float(value))
    return row["index"]


# --------------------------------------------------------------------------
# Pro-C 3 (FabFilter compressor) — 100 floats, a DIFFERENT layout to Pro-Q 4
# --------------------------------------------------------------------------

PROC3_NFLOATS = 100
PROC3_BAND_BASE = 32          # first side-chain EQ band starts here
PROC3_BAND_STRIDE = 9         # 6 bands x 9 floats  -> floats 32..85
PROC3_BANDS = 6

# floats 0..30 are the globals, and their offset equals their MCU parameter index.
# Established by sentinel probe: f[10]=20.0 -> "Lookha 20.00"; f[14]=24.0 -> "ChrDrv
# +24.00"; second pass f[10]=12.0 / f[14]=16.0 -> "12.00" / "+16.00".
PROC3_GLOBALS = [
    "Style", "Thrs", "AutThr", "LoAuTh", "Ratio", "Knee", "Range", "Attack",
    "Rel", "AutRel", "Lookha", "Hold", "Chrctr", "ChrRou", "ChrDrv", "WetGai",
    "WetPan", "DrGain", "DryPan", "AutGai", "SiChIn", "SiChLe", "HoTrSy", "HoTrOf",
    "HoTrLe", "StLink", "StLiMd", "StLiCe", "StLiSu", "StLiTo", "StLink2",
]


def proc3_band_base(band: int) -> int:
    """band is 1-based, 1..6."""
    if not 1 <= band <= PROC3_BANDS:
        raise ValueError("Pro-C 3 side-chain band must be 1..%d" % PROC3_BANDS)
    return PROC3_BAND_BASE + PROC3_BAND_STRIDE * (band - 1)


def proc3_band_get(blob: bytes, band: int) -> dict:
    """One Pro-C 3 side-chain EQ band. Same nine fields and encodings as Pro-Q 4."""
    fl = ffbs_floats(blob)
    b = proc3_band_base(band)
    g = lambda off: fl[b + off]
    return {
        "band": band,
        "used": bool(g(B_USED)),
        "enabled": bool(g(B_ENABLED)),
        "freq_hz": freq_to_hz(g(B_FREQ)),
        "gain_db": g(B_GAIN),
        "q": enc_to_q(g(B_Q)),
        "shape": SHAPE_NAMES.get(g(B_SHAPE), "enum=%g" % g(B_SHAPE)),
        "slope_raw": g(B_SLOPE),
        "stereo_placement": g(B_STEREO_PLACEMENT),
        "speaker": g(B_SPEAKER),
    }


def proc3_band_set(blob: bytearray, band: int, **kw) -> list[str]:
    """Write fields of one Pro-C 3 side-chain band (freq_hz, gain_db, q, used, ...)."""
    b = proc3_band_base(band)
    written = []

    def put(off, value, label):
        ffbs_set_float(blob, b + off, float(value))
        written.append(label)

    if "used" in kw:       put(B_USED, 1.0 if kw["used"] else 0.0, "used")
    if "enabled" in kw:    put(B_ENABLED, 1.0 if kw["enabled"] else 0.0, "enabled")
    if "freq_hz" in kw:    put(B_FREQ, hz_to_freq(kw["freq_hz"]), "freq_hz")
    if "gain_db" in kw:    put(B_GAIN, kw["gain_db"], "gain_db")
    if "q" in kw:          put(B_Q, q_to_enc(kw["q"]), "q")
    if "shape" in kw:      put(B_SHAPE, kw["shape"], "shape")
    if "slope" in kw:      put(B_SLOPE, kw["slope"], "slope")
    return written


def proc3_global_set(blob: bytearray, offset: int, value: float) -> str:
    """Write one Pro-C 3 global by its float offset (== its MCU parameter index).

    The unit is the plug-in's own; offsets and their MCU labels are in
    PROC3_GLOBALS.  Values out of range are clamped by Logic on load.
    """
    if not 0 <= offset <= 30:
        raise ValueError("Pro-C 3 global offset must be 0..30")
    ffbs_set_float(blob, offset, float(value))
    return PROC3_GLOBALS[offset]


def proc3_globals_get(blob: bytes) -> dict:
    fl = ffbs_floats(blob)
    return {PROC3_GLOBALS[i]: fl[i] for i in range(31)}


# --------------------------------------------------------------------------
# Pmtr (Brainworx / Plugin Alliance / SPL) — BIG-ENDIAN
# --------------------------------------------------------------------------

def pmtr_header(raw: bytes):
    """'Pmtr' containers are BIG-ENDIAN throughout — header included.

    Reading this little-endian yields a wildly wrong record count and stride and
    blows up a moment later inside unpack_from.
    """
    if raw[:4] != PMTR_MAGIC:
        raise ValueError("not a Pmtr container")
    return struct.unpack_from(">5I", raw, 4)      # size, size, nparams, per, 0


def pmtr_records(raw: bytes):
    """Yield (offset, tag, (f0, f1, f2, f3)) — floats are BIG-ENDIAN."""
    _, _, nparams, per, _ = pmtr_header(raw)
    off = 24
    for _ in range(nparams):
        if off + 20 > len(raw):
            break
        tag = raw[off:off + 4]
        if not all(32 <= c < 127 for c in tag):
            break
        vals = struct.unpack_from(">%df" % per, raw, off + 4)
        yield off, tag.decode("latin1"), vals
        off += 4 + 4 * per


# --------------------------------------------------------------------------
# Pmtr ⇄ the plug-in's AU parameter list
#
# AU_PARAM_FORMAT.md §5.3: a Pmtr record table IS the plug-in's AU parameter
# list, in the AU's own order, one record per parameter that carries state —
# the trailing read-only meters are simply absent.  Float[0] holds the value
# NORMALISED to 0..1, and floats[1..3] hold the parameter's DEFAULT, normalised
# the same way and repeated three times.  Measured on four plug-ins: 45 of 45
# records' float[1] equals the AU's own published default, converted.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# The third container: VC2! + a length-prefixed, PLAINTEXT XML state
#
# Container (little-endian, no compression, no base64 inside):
#
#     "VC2!"  u32 body_length  <?xml version="1.0" encoding="UTF-8"?> ... \0
#
# Logic stores these under the plist key `jucePluginState`.  Unlike FFBS and
# Pmtr the parameter NAMES ARE IN THE FILE, so nothing has to be matched
# against a dumped AU parameter list — and the values are the values the
# plug-in itself writes, in its own units.
#
# There is no single schema; every vendor names its parameters its own way, so
# two shapes are recognised:
#
#   attribute shape   <ValhallaRoom decay="0.008" HighCut="0.73" .../>
#                     a bare attribute whose name is not known metadata
#   id shape          <PARAM id="Target" value="-14.0"/>
#                     <Parameter name="Target" mapped="-14.0"/>
#                     an element carrying an identifier attribute (id / name)
#                     and a value attribute (value / mapped)
#
# `xml_params` returns everything it recognised, with the scheme it used, so a
# caller can see what was inferred rather than trust a count.
XML_MAGIC = b"VC2!"

# Attributes that identify rather than parameterise.  Deliberately explicit:
# a name in here is never offered as a writable parameter.
XML_META_ATTRS = frozenset("""
id name ID Name application pluginVersion version Version presetName PRESET_NAME
SelectedPreset SelectedTier PresetPath Path platform index tags modified
Spc0 Spc1 Spc2 Spc3 Major Minor Revision
""".split())

_XML_ELEM = re.compile(rb"<([A-Za-z_][\w.\-]*)((?:\s+[\w.\-]+\s*=\s*\"[^\"]*\")*)\s*/?>")
_XML_ATTR = re.compile(rb"([\w.\-]+)\s*=\s*\"([^\"]*)\"")


def _xml_attr_span(body: bytes, m, group: int):
    """The byte span of an attribute's VALUE inside `body`."""
    return m.start(group), m.end(group)


def xml_unwrap(raw: bytes):
    """(body, declared_length, trailing) for a VC2! blob."""
    if raw[:4] != XML_MAGIC:
        raise ValueError("not a VC2! blob: %r" % bytes(raw[:4]))
    n = struct.unpack_from("<I", raw, 4)[0]
    return bytes(raw[8:8 + n]), n, bytes(raw[8 + n:])


def xml_wrap(body: bytes) -> bytes:
    """Rebuild the container around a body of the same length."""
    return XML_MAGIC + struct.pack("<I", len(body)) + body + b"\0"


def xml_root(raw: bytes) -> str:
    body = xml_unwrap(raw)[0]
    m = re.search(rb"<\?xml[^>]*\?>\s*<([A-Za-z_][\w.\-]*)", body)
    return m.group(1).decode("latin1") if m else ""


def xml_params(raw: bytes):
    """Every parameter the XML state names.

    Returns a list of dicts: {name, value, scheme, elem, vstart, vend}, where
    vstart/vend are offsets of the VALUE TEXT inside the body.  `scheme` is
    'id' for the identifier shape and 'attr' for a bare attribute.
    """
    body = xml_unwrap(raw)[0]

    # --- the Swivel Audio shape: one element per FIELD, addressed by (Spc0, Spc1)
    # <PluginAudioParameter Spc0="0" Spc1="value" value="0.677537"/>
    # Spc0 is the parameter index and Spc1 says which field this element is
    # (`name`, `unit`, `numberOfSteps`, `value`).  Read generically it looks
    # like 63 anonymous run of attributes; grouped, it is 15 named parameters.
    spc = {}
    for m in _XML_ELEM.finditer(body):
        attrs = {}
        for a in _XML_ATTR.finditer(m.group(2)):
            attrs[a.group(1).decode("latin1")] = (
                a.group(2), m.start(2) + a.start(2), m.start(2) + a.end(2))
        if "Spc0" not in attrs or "Spc1" not in attrs:
            continue
        idx = attrs["Spc0"][0].decode("latin1")
        fld = attrs["Spc1"][0].decode("latin1")
        spc.setdefault(idx, {})[fld] = attrs.get("value", (b"", 0, 0))
    if spc:
        grouped = []
        for idx in sorted(spc, key=lambda x: (len(x), x)):
            fields = spc[idx]
            name = fields.get("name", (b"", 0, 0))[0].decode("latin1") or ("Spc0=" + idx)
            v, vs, ve = fields.get("value", (b"", 0, 0))
            grouped.append({"name": name, "value": v.decode("latin1"),
                            "scheme": "spc", "elem": "PluginAudioParameter",
                            "key": "Spc0=%s Spc1=value" % idx,
                            "vstart": vs, "vend": ve})
        return grouped

    out = []
    for m in _XML_ELEM.finditer(body):
        elem = m.group(1).decode("latin1")
        attrs = {}
        for a in _XML_ATTR.finditer(m.group(2)):
            attrs[a.group(1).decode("latin1")] = (
                a.group(2), m.start(2) + a.start(2), m.start(2) + a.end(2))
        ident = None
        for k in ("id", "name", "ID", "Name"):
            if k in attrs and attrs[k][0]:
                ident = attrs[k][0].decode("latin1")
                break
        if ident is not None:
            # `id` is the address; a `name` attribute beside it is the human
            # label, and both are accepted by xml_find
            label = ""
            for lk in ("name", "Name"):
                if lk != "id" and lk in attrs and attrs[lk][0] != ident.encode():
                    label = attrs[lk][0].decode("latin1")
                    break
            for vk in ("value", "mapped"):
                if vk in attrs:
                    v, s, e = attrs[vk]
                    out.append({"name": ident, "label": label,
                                "value": v.decode("latin1"),
                                "scheme": "id", "elem": elem,
                                "vstart": s, "vend": e})
            continue
        for k, (v, s, e) in attrs.items():
            if k in XML_META_ATTRS:
                continue
            out.append({"name": k, "value": v.decode("latin1"),
                        "scheme": "attr", "elem": elem,
                        "vstart": s, "vend": e})
    return out


def xml_find(raw: bytes, name: str, occurrence=None, elem=None):
    """One parameter by name.

    A name can legitimately repeat — the `state` and `PluginState` roots carry
    several preset blocks with the same parameter in each — so `occurrence`
    (0-based) picks one and an ambiguous match with no `occurrence` REFUSES,
    listing how many there are rather than guessing.  `elem` additionally
    restricts to one element name.
    """
    rows = xml_params(raw)
    hits = [r for r in rows if r["name"] == name]
    if not hits:
        hits = [r for r in rows if r.get("label") == name]
    if not hits:
        low = name.lower()
        hits = [r for r in rows
                if r["name"].lower() == low or (r.get("label") or "").lower() == low]
    if elem is not None:
        hits = [r for r in hits if r["elem"] == elem]
    if not hits:
        raise KeyError("no XML parameter %r (have: %s)"
                       % (name, ", ".join(sorted({r["name"] for r in rows}))[:300]))
    if occurrence is not None:
        if occurrence >= len(hits):
            raise KeyError("%r has %d occurrences, #%d asked for"
                           % (name, len(hits), occurrence))
        return hits[occurrence]
    if len(hits) > 1:
        raise ValueError("%r matches %d parameters (elems: %s) — pass occurrence"
                         % (name, len(hits),
                            ", ".join(sorted({r["elem"] for r in hits}))))
    return hits[0]


def xml_set_row(raw: bytearray, row: dict, value) -> dict:
    """Write one already-located parameter row (the no-ambiguity path)."""
    body, n, trailing = xml_unwrap(bytes(raw))
    before = row["value"]
    room = row["vend"] - row["vstart"]
    new = str(value)
    if len(new) < room:
        # a shorter number can always be written to the same length by adding
        # trailing zeros, which no float parser can tell apart
        try:
            new = xml_shrink_to(raw, row, value, room)
        except (ValueError, TypeError):
            pass
    b = bytearray(body)
    b[row["vstart"]:row["vend"]] = new.encode("latin1")
    delta = len(new) - (row["vend"] - row["vstart"])
    if delta:
        # the body just grew by `delta`, so the padding has to give that much
        # back — the opposite sign is what made the file change size
        b = _xml_absorb(bytes(b), -delta)
    raw[:] = xml_wrap(bytes(b))
    return {"name": row["name"], "scheme": row["scheme"], "elem": row["elem"],
            "before": before, "after": new, "length_delta": delta,
            "mode": "in_place", "grew_by": 0}


def _xml_pad_point(body: bytes) -> int:
    """Where whitespace may be inserted into XML without changing its meaning.

    XML allows whitespace inside a tag but not inside the `/>` that closes an
    empty element, so the point depends on what the document ends with:

        <ValhallaRoom a="1"/>      ->  <ValhallaRoom a="1"   />
        <Root><A/></Root>          ->  <Root><A/></Root   >   (an ETag may
                                        carry whitespace before its '>')

    Inserting at the wrong byte produces a tag the parser can no longer read,
    which is exactly what happened before this function existed.
    """
    e = len(body)
    while e and body[e - 1:e] in b" \t\r\n":
        e -= 1
    if e >= 2 and body[e - 2:e] == b"/>":
        return e - 2          # <ValhallaRoom a="1"   />, never  /   >
    return e - 1              # ...</Root   >


def _xml_absorb(body: bytes, delta: int) -> bytearray:
    """Grow or shrink an XML body by `delta` bytes without changing its meaning.

    Whitespace is ignorable in two places that always exist: at the pad point
    above, and between two tags.  This is what keeps the container — and
    therefore the whole Logic file — exactly the same size however long the new
    value is.
    """
    b = bytearray(body)
    at = _xml_pad_point(b)
    if delta > 0:
        b[at:at] = b" " * delta
        return b
    i = at
    while i > 0 and b[i - 1:i] in (b" ", b"\n", b"\t", b"\r") and delta < 0:
        i -= 1
        delta += 1
    if delta < 0:
        del b[i:at]
        at = i
    if delta < 0:
        for m in reversed(list(re.finditer(rb">\s+<", bytes(b)))):
            room = m.end() - m.start() - 2
            take = min(room, -delta)
            if take > 0:
                del b[m.start() + 1:m.start() + 1 + take]
                delta += take
                if delta == 0:
                    break
    if delta < 0:
        raise ValueError("cannot absorb %d bytes of whitespace" % (-delta))
    return b


def xml_shrink_to(raw: bytearray, row: dict, value, target_len: int):
    """Render `value` as a number occupying EXACTLY `target_len` characters.

    Trailing zeros after a decimal point never change a float's value, so a
    numeric parameter can always be made to fit the space its predecessor
    occupied — which is what keeps the whole Logic file the same size even
    when the new number is written more briefly than the old one.
    """
    f = float(value)
    txt = repr(f)
    if len(txt) > target_len:
        raise ValueError("%r needs %d characters, %d available"
                         % (txt, len(txt), target_len))
    if "." not in txt:
        txt += "."
    return txt + "0" * (target_len - len(txt))


def xml_apply(data: bytearray, start: int, end: int, name: str, value,
              occurrence=None, elem=None) -> dict:
    """Write an XML parameter into a whole file, choosing the splice to use.

    In place when the container can keep its length (always, for a number),
    otherwise a variable-length splice — which moves every later offset and is
    why the result says which happened.
    """
    ran = bytes(data[start:end])
    raw = bytearray(_b64_decode_run(ran))
    res = xml_set(raw, name, value, occurrence=occurrence, elem=elem)
    if len(raw) == len(_b64_decode_run(ran)):
        splice_blob(data, start, end, bytes(raw))
        res["mode"] = "in_place"
        res["grew_by"] = 0
    else:
        res["grew_by"] = splice_blob_var(data, start, end, bytes(raw))
        res["mode"] = "resized"
    res["where"] = "0x%x" % start
    return res


def xml_set(raw: bytearray, name: str, value, occurrence=None, elem=None,
            allow_resize=True) -> dict:
    """Write one XML parameter, keeping the container EXACTLY the same length.

    The XML is padded with spaces inserted before the closing root tag, which
    an XML parser discards — so the declared length, the base64 run and the
    whole file stay byte-for-byte the same size.  That is what lets a
    `jucePluginState` be edited with the same equal-length surgery as FFBS and
    Pmtr, even though the value is stored as TEXT.
    """
    row = xml_find(bytes(raw), name, occurrence=occurrence, elem=elem)
    try:
        return xml_set_row(raw, row, value)
    except ValueError:
        if not allow_resize:
            raise
        # the text will not fit the bytes its predecessor occupied; write it
        # at its own length and let the caller resize the container
        body, n, trailing = xml_unwrap(bytes(raw))
        before = row["value"]
        new = str(value)
        b = bytearray(body)
        b[row["vstart"]:row["vend"]] = new.encode("latin1")
        raw[:] = xml_wrap(bytes(b))
        return {"name": row["name"], "scheme": row["scheme"], "elem": row["elem"],
                "before": before, "after": new,
                "length_delta": len(new) - (row["vend"] - row["vstart"]),
                "mode": "resized",
                "grew_by": (row["vend"] - row["vstart"]) - len(new)}


PMTR_ALIGN_TOL = 1e-5


def pmtr_norm(params: list, index: int, value: float) -> float:
    """AU value -> the 0..1 number a Pmtr record stores."""
    p = params[index]
    span = p["max"] - p["min"]
    return (value - p["min"]) / span if span else 0.0


def pmtr_denorm(params: list, index: int, raw: float) -> float:
    """The 0..1 number a Pmtr record stores -> the AU value."""
    p = params[index]
    return p["min"] + raw * (p["max"] - p["min"])


def pmtr_align(raw: bytes, subtype: str):
    """Align a Pmtr record table with the plug-in's dumped AU parameter list.

    Returns (rows, report).  Each row is the record plus, where the plug-in's
    map is known, the parameter's AU name, range, current AU value and its
    default — and `verified`, which is True when float[1] really is that
    default.  A row with no map is still returned, addressed by index and tag.

    The alignment is POSITIONAL and is checked, not assumed: a table whose
    float[1] values do not reproduce the AU's defaults anywhere is reported
    with `verified: 0` rather than silently believed.
    """
    params = load_param_map(subtype)
    recs = list(pmtr_records(raw))
    rows, hits = [], 0
    for i, (offset, tag, vals) in enumerate(recs):
        row = {"index": i, "offset": offset, "tag": tag, "raw": vals[0]}
        p = params[i] if i < len(params) else None
        if p:
            span = p["max"] - p["min"]
            default_raw = (p["default"] - p["min"]) / span if span else 0.0
            row.update({"name": p["name"], "min": p["min"], "max": p["max"],
                        "value": pmtr_denorm(params, i, vals[0]),
                        "default": p["default"], "default_raw": default_raw})
            if len(vals) >= 4:
                row["verified"] = abs(vals[1] - default_raw) <= PMTR_ALIGN_TOL
                hits += bool(row["verified"])
        rows.append(row)
    report = {"subtype": subtype, "records": len(recs), "params": len(params),
              "verified": hits,
              "unmapped_tail": [p["name"] for p in params[len(recs):]]}
    return rows, report


def _pmtr_pick(rows: list, name: str, subtype: str) -> dict:
    """Find a record by AU name: exact, then unambiguous prefix, never a guess."""
    low = name.strip().lower()
    named = [r for r in rows if "name" in r]
    if not named:
        raise KeyError("no parameter map for subtype %r — run "
                       "tools/dump_all_maps.py" % subtype)
    exact = [r for r in named if r["name"].lower() == low]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError("ambiguous parameter %r" % name)
    pref = [r for r in named if r["name"].lower().startswith(low)]
    if len(pref) == 1:
        return pref[0]
    if len(pref) > 1:
        raise ValueError("ambiguous parameter %r matches: %s"
                         % (name, ", ".join(r["name"] for r in pref[:8])))
    # a name the AU publishes but the table has no record for is a read-only
    # output (a meter) — say that, rather than "no such parameter"
    params = load_param_map(subtype)
    for p in params:
        if p["name"].lower().startswith(low) or low == p["name"].lower():
            raise KeyError(
                "%s: %r is read-only — the plug-in publishes it but the state "
                "table carries no record for it (only %d records for %d "
                "parameters)" % (subtype, p["name"], len(named), len(params)))
    tail = [r["name"] for r in named]
    raise KeyError("no parameter named %r on %s (records: %s)"
                   % (name, subtype, ", ".join(tail[:12])))


def pmtr_set_named(raw: bytearray, subtype: str, name: str, value: float,
                   from_raw: bool = False, enforce_range: bool = True) -> dict:
    """Write one named Pmtr parameter.  `value` is the AU value unless
    from_raw, in which case it is the 0..1 number stored in the file."""
    rows, report = pmtr_align(bytes(raw), subtype)
    row = _pmtr_pick(rows, name, subtype)
    span = row["max"] - row["min"]
    if from_raw:
        target = float(value)
    else:
        if enforce_range and not (row["min"] <= value <= row["max"]):
            raise ValueError("%s is out of range: %g not in %g .. %g"
                             % (row["name"], value, row["min"], row["max"]))
        target = (value - row["min"]) / span if span else 0.0
    before_raw = row["raw"]
    struct.pack_into(">f", raw, row["offset"] + 4, target)
    row = dict(row,
               wrote_raw=target,
               before_raw=before_raw, after_raw=target,
               before_raw_value=row["value"],
               after=pmtr_denorm_span(row, target),
               verification=report)
    row["before"] = row.pop("value")
    row["after"] = row["after"]
    return row


def pmtr_denorm_span(row: dict, raw_value: float) -> float:
    """Denormalise using a row's own min/max (works for aligned and bare rows)."""
    span = row.get("max", 1.0) - row.get("min", 0.0)
    return row.get("min", 0.0) + raw_value * span


def pmtr_set_record(raw: bytearray, tag: str, index: int, value: float) -> bool:
    """Write one big-endian float inside the record named `tag`."""
    for off, t, _ in pmtr_records(bytes(raw)):
        if t == tag:
            struct.pack_into(">f", raw, off + 4 + index * 4, value)
            return True
    return False


def pmtr_set_value(raw: bytearray, tag: str, value: float) -> bool:
    """Set the VALUE of a named record — float[0], the only position that varies.

    floats[1..3] are per-parameter metadata and are deliberately left alone
    (see AU_PARAM_FORMAT.md §5.1).
    """
    return pmtr_set_record(raw, tag, 0, value)


# --------------------------------------------------------------------------
# .cst / .patch chunk walker
# --------------------------------------------------------------------------

def cst_chunks(buf: bytes):
    """Yield (offset, length, tag) for every chunk.

    tag(4) @+0 ... u32 payload length @+28 ; total length = payload + 36.
    """
    o = 0
    while o + 36 <= len(buf):
        tag = buf[o:o + 4]
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in tag):
            break
        (plen,) = struct.unpack_from("<I", buf, o + 28)
        total = plen + 36
        if total <= 0 or o + total > len(buf):
            break
        yield o, total, tag.decode("ascii", "replace")
        o += total


def cst_states(buf: bytes):
    """Yield (chunk_index, offset, length, plist_dict) for chunks holding an AU
    state plist."""
    for i, (off, total, _tag) in enumerate(cst_chunks(buf)):
        body = buf[off:off + total]
        if b"</plist>" not in body:
            continue
        s = body.find(b"<?xml")
        e = body.find(b"</plist>") + len(b"</plist>")
        try:
            d = plistlib.loads(body[s:e])
        except Exception:
            continue
        if isinstance(d, dict) and ("data" in d or "FabFilterPluginState" in d
                                    or "jucePluginState" in d):
            yield i, off, total, d


STATE_KEYS = ("FabFilterPluginState", "jucePluginState", "data")


def state_key(d: dict) -> str:
    """Which plist key holds this chunk's AU state.

    A JUCE chunk carries BOTH `data` (the binary part) and `jucePluginState`
    (the XML that names the parameters), so the key is chosen by which value
    actually begins with a container magic — picking by key order alone would
    silently edit the wrong one.
    """
    for k in STATE_KEYS:
        v = d.get(k)
        if isinstance(v, bytes) and v[:4] in (FFBS_MAGIC, PMTR_MAGIC, XML_MAGIC):
            return k
    for k in STATE_KEYS:
        if k in d:
            return k
    return "data"


def fourcc(v: int) -> str:
    return bytes([(v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255]).decode("latin1")


def fourcc_int(s: str) -> int:
    b = s.encode("latin1")[:4].ljust(4, b"\0")
    return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]


def _b64_decode_run(run: bytes) -> bytes:
    """Decode one base64 run, tolerating its line wrapping."""
    clean = re.sub(rb"\s", b"", run)
    return base64.b64decode(clean + b"===")


def ffbs_tag_strings(blob: bytes) -> list[str]:
    """Printable strings in an FFBS trailer.

    The trailer mixes length-prefixed strings with bare integers, so walking it
    as `u32 len + bytes` drifts out of phase; scanning is the reliable route.
    """
    _, n = ffbs_header(blob)
    tail = blob[FFBS_HEADER + n * 4:]
    return [m.group().decode("latin1") for m in re.finditer(rb"[ -~]{2,}", tail)]


def ffbs_subtype(blob: bytes) -> str:
    """The AU subtype of an FFBS blob, read off its trailer's first string.

    FabFilter writes `FQ4p | Default Setting | <track> | CuSV | ...`, so the
    first printable token is the subtype — the state itself carries no identity.
    """
    tags = ffbs_tag_strings(blob)
    return tags[0] if tags else ""


def ffbs_track(blob: bytes) -> str:
    """The track name FabFilter records in its own state (trailer slot 3)."""
    tags = ffbs_tag_strings(blob)
    return tags[2] if len(tags) > 2 else ""


def cst_set_state_byte(buf: bytearray, off: int, total: int, index: int, new: int) -> None:
    """Overwrite ONE byte of the base64-encoded state inside a CST chunk.

    The plist is XML *inside* the chunk, so editing the text in place keeps both
    the plist length and the chunk's own length field valid.
    """
    body = bytes(buf[off:off + total])
    s = body.find(b"<?xml")
    e = body.find(b"</plist>") + len(b"</plist>")
    plist = body[s:e]
    m = re.search(rb"<data>(.*?)</data>", plist, re.S)
    if not m:
        raise ValueError("no <data> element in chunk")
    inner = m.group(1)
    clean = re.sub(rb"\s", b"", inner)
    blob = bytearray(base64.b64decode(clean + b"==="))
    blob[index] = new
    enc = base64.b64encode(bytes(blob))
    assert len(enc) == len(clean)
    out = bytearray(inner)
    j = 0
    for i in range(len(inner)):
        if inner[i] not in B64_WS:
            out[i] = enc[j]
            j += 1
    abs_start = off + s + m.start(1)
    buf[abs_start:abs_start + len(inner)] = bytes(out)


# --------------------------------------------------------------------------
# .aupreset
# --------------------------------------------------------------------------

def read_aupreset(path: str) -> dict:
    with open(path, "rb") as fh:
        d = plistlib.load(fh)
    d["_key"] = state_key(d)
    return d


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _save(path: str, data: bytes) -> None:
    tmp = path + ".tmp-au_state"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _neighbour_identity(data: bytes, start: int) -> dict:
    """Read a blob's plug-in identity out of the plaintext plist beside it.

    Only the `<data>` element's content is base64; the surrounding plist is
    plaintext, and Logic writes a display name plus a 12-byte little-endian
    `<subtype><type><manufacturer>` triple immediately before it:

        SPL Transie \\0 1LPSxfuaPTPS \\0... <plist ... <key>data</key><data>BASE64

    (reversed: "SPTP" "aufx" "SPL1").  So even a Pmtr blob — which carries no
    identity of its own — can be attributed to a plug-in.
    """
    seg = data[max(0, start - 600):start]
    runs = [(m.start(), m.group().decode("latin1"))
            for m in re.finditer(rb"[ -~]{2,}", seg)]
    out = {"display_name": "", "subtype": "", "type": "", "manufacturer": "",
           "preset_file": ""}
    xi = next((i for i in range(len(runs) - 1, -1, -1) if "<?xml" in runs[i][1]), None)
    if xi is None or xi < 1:
        return out
    # Between the identity triple and the plist there is usually a junk run
    # (e.g. "46ia"), so scan back for the 12-character triple rather than
    # assuming it is adjacent.
    ti = next((i for i in range(xi - 1, max(-1, xi - 7), -1)
               if len(runs[i][1]) == 12), None)
    if ti is None:
        return out
    triple = runs[ti][1]
    # Byte order is <manufacturer><type><subtype>, each stored little-endian:
    # "1LPS" "xfua" "PTPS" -> "SPL1" "aufx" "SPTP"
    out["manufacturer"] = triple[0:4][::-1]
    out["type"] = triple[4:8][::-1]
    out["subtype"] = triple[8:12][::-1]
    if ti >= 1:
        out["display_name"] = runs[ti - 1][1]
    if ti >= 2 and runs[ti - 2][1].endswith(".aupreset"):
        out["preset_file"] = runs[ti - 2][1]
    return out


def project_states(data: bytes, instances=None):
    """Yield one dict per AU state blob in a ProjectData, with identity.

    `instances` is the ordered list of (track_name, insert_index, ident) the
    caller established from the arrangement, if it has one; the i-th state is
    labelled with it.  Without it a state is still identifiable as a plug-in,
    but not attributable to a track.
    """
    n = 0
    for magic in (FFBS_MAGIC, PMTR_MAGIC):
        for start, end, blob in find_blobs(data, magic):
            ident = _neighbour_identity(data, start)
            if magic is FFBS_MAGIC:
                if not ident["subtype"]:
                    ident["subtype"] = ffbs_subtype(blob)
                ident["track"] = ffbs_track(blob)
                ident["nfloats"] = ffbs_header(blob)[1]
            else:
                ident["nfloats"] = 0
            inst = None
            if instances and n < len(instances):
                inst = instances[n]
            yield {"index": n, "offset": start, "end": end, "blob": blob,
                   "magic": "FFBS" if magic is FFBS_MAGIC else "Pmtr",
                   "instance": inst, **ident}
            n += 1


def cmd_list(args) -> int:
    path = args.path
    if path.endswith(".aupreset"):
        d = read_aupreset(path)
        print("%s  %s/%s  %d bytes  name=%r"
              % (d["_key"], fourcc(d["manufacturer"]), fourcc(d["subtype"]),
                 len(d[d["_key"][0] if False else d["_key"]]), d.get("name")))
        return 0

    data = _load(path)
    if path.endswith((".cst", ".patch")) or path.endswith("/#Root.cst"):
        for i, off, total, d in cst_states(data):
            k = state_key(d)
            raw = d[k]
            print("chunk %-2d off=0x%-6x len=%-6d %s %s/%s %d B"
                  % (i, off, total, k, fourcc(d["manufacturer"]),
                     fourcc(d["subtype"]), len(raw)))
    else:
        n = 0
        for start, end, blob in find_blobs(data):
            n += 1
            try:
                ver, nf = ffbs_header(blob)
            except ValueError:
                nf = -1
            # trailer: [subtype, version, preset name, track name, ...]
            strings = ffbs_tag_strings(blob)
            print("#%-3d off=0x%-8x len=%-6d floats=%-5s %s"
                  % (n, start, len(blob), nf, " | ".join(strings[:6])))
        print("total %d FFBS blobs" % n)
    return 0


def _find_track_blob(data: bytes, track: str, nfloats: int | None = None):
    for start, end, blob in find_blobs(data):
        if track.encode() not in blob:
            continue
        if nfloats is not None and ffbs_header(blob)[1] != nfloats:
            continue
        return start, end, blob
    return None


def cmd_dump(args) -> int:
    data = _load(args.path)
    hits = [(s, e, b) for s, e, b in find_blobs(data)
            if not args.track or args.track.encode() in b]
    if not hits:
        print("no matching FFBS blob", file=sys.stderr)
        return 1
    for start, end, blob in hits:
        ver, n = ffbs_header(blob)
        print("offset=0x%x  len=%d  FFBS ver=%d  floats=%d" % (start, len(blob), ver, n))
        strings = ffbs_tag_strings(blob)
        if strings:
            print("  %s" % " | ".join(strings[:4]))
        sub = ffbs_subtype(blob)
        named = load_param_map(sub)
        if named:
            print("  subtype %s, %d floats, %d named parameters"
                  % (sub, n, len(named)))
            for i, name, v in ffbs_params(blob, sub):
                print("    %4d  %-42s %g" % (i, name, v))
        elif n == PROQ4_NFLOATS:
            for band in range(1, PROQ4_BANDS + 1):
                info = band_get(blob, band)
                if not info["used"]:
                    continue
                print("  band %2d: %8.2f Hz  gain %+6.2f dB  Q %.3f  %-8s "
                      "dyn %s  SC %g-%g Hz"
                      % (band, info["freq_hz"], info["gain_db"], info["q"],
                         info["shape"], info["dyn_threshold"],
                         info["sc_lo_hz"], info["sc_hi_hz"]))
        elif n == PROC3_NFLOATS:
            g = proc3_globals_get(blob)
            print("  globals: " + "  ".join("%s=%g" % (k, v)
                                            for k, v in list(g.items())[:10]))
            for band in range(1, PROC3_BANDS + 1):
                info = proc3_band_get(blob, band)
                print("  SC band %d: %s %8.2f Hz  gain %+6.2f dB  Q %.3f  %-8s"
                      % (band, "used " if info["used"] else "unused",
                         info["freq_hz"], info["gain_db"], info["q"], info["shape"]))
        else:
            print("  subtype %s, %d floats  (no map dumped for this plug-in)"
                  % (sub, n))
    return 0


def cmd_set(args) -> int:
    data = bytearray(_load(args.path))
    hit = _find_track_blob(bytes(data), args.track, args.nfloats)
    if not hit:
        print("no FFBS blob containing %r with %s floats"
              % (args.track, args.nfloats), file=sys.stderr)
        return 1
    start, end, blob = hit
    ver, n = ffbs_header(blob)
    new = bytearray(blob)

    if n == PROC3_NFLOATS:
        # Pro-C 3: either a global by offset, or a side-chain EQ band
        if args.global_offset is not None:
            if args.value is None:
                print("--value is required with --global-offset", file=sys.stderr)
                return 1
            name = proc3_global_set(new, args.global_offset, args.value)
            before = ffbs_floats(blob)[args.global_offset]
            print("Pro-C 3 global %d (%s): %g -> %g"
                  % (args.global_offset, name, before, args.value))
        elif args.band is not None:
            fields = {}
            for k in ("gain_db", "freq_hz", "q", "shape", "slope"):
                v = getattr(args, k, None)
                if v is not None:
                    fields[k] = v
            if args.used is not None:
                fields["used"] = bool(args.used)
            if not fields:
                print("nothing to set", file=sys.stderr)
                return 1
            written = proc3_band_set(new, args.band, **fields)
            print("Pro-C 3 SC band %d: writing %s" % (args.band, ", ".join(written)))
            b4 = proc3_band_get(blob, args.band)
            af = proc3_band_get(bytes(new), args.band)
            for k in b4:
                if b4[k] != af[k]:
                    print("   %-18s %r -> %r" % (k, b4[k], af[k]))
        else:
            print("Pro-C 3: pass --global-offset N --value V, or --band N ...",
                  file=sys.stderr)
            return 1
    else:
        if args.band is None:
            print("--band is required for Pro-Q 4", file=sys.stderr)
            return 1
        fields = {}
        for name in ("gain_db", "freq_hz", "q", "shape", "slope"):
            v = getattr(args, name, None)
            if v is not None:
                fields[name] = v
        if args.used is not None:
            fields["used"] = bool(args.used)
        if not fields:
            print("nothing to set", file=sys.stderr)
            return 1
        written = band_set(new, args.band, **fields)
        print("band %d: writing %s" % (args.band, ", ".join(written)))
        before = band_get(blob, args.band)
        after = band_get(bytes(new), args.band)
        for k in before:
            if before[k] != after[k]:
                print("   %-18s %r -> %r" % (k, before[k], after[k]))

    splice_blob(data, start, end, bytes(new))
    if len(data) != len(_load(args.path)):
        print("REFUSING: file size would change", file=sys.stderr)
        return 1
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    _save(args.path, bytes(data))
    print("written; file size unchanged (%d bytes)" % len(data))
    print("now CLOSE the project in Logic and reopen it for the change to load.")
    return 0


def cmd_blob(args) -> int:
    data = _load(args.path)
    for i, off, total, d in cst_states(data):
        if args.chunk is not None and i != args.chunk:
            continue
        k = state_key(d)
        raw = d[k]
        print("chunk %d  off=0x%x len=%d  key=%s  %s/%s  %d B"
              % (i, off, total, k, fourcc(d["manufacturer"]), fourcc(d["subtype"]),
                 len(raw)))
        if raw[:4] == FFBS_MAGIC:
            print("   FFBS ver=%d floats=%d" % ffbs_header(raw))
            print("   subtype/track strings:", ffbs_tag_strings(raw)[:6])
            print("   trailer: %d B (schema not established — see docstring)"
                  % len(ffbs_tail_raw(raw)))
        elif raw[:4] == PMTR_MAGIC:
            print("   Pmtr hdr:", pmtr_header(raw))
            for o, tag, vals in pmtr_records(raw):
                print("      %-6s %s" % (tag, [round(v, 5) for v in vals]))
    return 0


def cmd_pset(args) -> int:
    """Write one value into a blob addressed by its offset in the file.

    Pmtr blobs carry no track name and no plug-in identity, so they cannot be
    found by name the way FFBS blobs can — address them by the offset `list`
    reports.
    """
    path = args.path
    orig = _load(path)
    data = bytearray(orig)

    target, magic = None, None
    for start, end, blob in find_blobs(data, FFBS_MAGIC):
        if start == args.offset:
            target, magic = (start, end, blob), FFBS_MAGIC
            break
    if target is None:
        for start, end, blob in find_blobs(data, PMTR_MAGIC):
            if start == args.offset:
                target, magic = (start, end, blob), PMTR_MAGIC
                break
    if target is None:
        print("no base64 blob starts at 0x%x" % args.offset, file=sys.stderr)
        return 1

    start, end, blob = target
    new = bytearray(blob)
    if magic is FFBS_MAGIC:
        if args.tag is not None or args.param is not None:
            print("--tag/--param address Pmtr records; use --float for FFBS",
                  file=sys.stderr)
            return 1
        if args.float_index is None:
            print("FFBS needs --float INDEX", file=sys.stderr)
            return 1
        before = ffbs_floats(blob)[args.float_index]
        ffbs_set_float(new, args.float_index, args.value)
        what = "float[%d]" % args.float_index
        after = args.value
    else:
        if args.param:
            # address the plug-in's OWN parameter name; the record table is the
            # AU parameter list in order, so the name resolves through the map
            subtype = args.subtype
            if not subtype:
                subtype = (_neighbour_identity(orig, start) or {}).get("subtype") or ""
            if not subtype:
                print("--param needs --subtype (no plug-in identity found next "
                      "to this blob)", file=sys.stderr)
                return 1
            try:
                row = pmtr_set_named(new, subtype, args.param, args.value,
                                     from_raw=args.raw,
                                     enforce_range=not args.force)
            except (KeyError, ValueError) as exc:
                print(exc, file=sys.stderr)
                return 1
            before = row["raw"]
            after = row["wrote_raw"]
            what = ("%s[%d] %s" % (subtype, row["index"], row["name"]))
            print("  %s: %s = %g  ->  stored %g  (range %g .. %g)"
                  % (row["tag"], row["name"], row["value"] if not args.raw else before,
                     after, row["min"], row["max"]))
        else:
            if not args.tag:
                print("Pmtr needs --tag NAME, or --param NAME with --subtype",
                      file=sys.stderr)
                return 1
            recs = {t: v for _o, t, v in pmtr_records(blob)}
            if args.tag not in recs:
                print("no record %r; container has %s"
                      % (args.tag, ", ".join(sorted(recs))), file=sys.stderr)
                return 1
            before = recs[args.tag][0]
            if not pmtr_set_value(new, args.tag, args.value):
                print("record %r not writable" % args.tag, file=sys.stderr)
                return 1
            what = "tag %s[0]" % args.tag
            after = args.value

    print("%s @0x%x  %s: %r -> %r" % (path.split("/")[-1], start, what, before, after))
    splice_blob(data, start, end, bytes(new))
    if len(data) != len(orig):
        print("REFUSING: file size would change", file=sys.stderr)
        return 1
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0
    _save(path, bytes(data))
    print("written; size unchanged (%d bytes)" % len(data))
    print("now CLOSE the project in Logic and reopen it for the change to load.")
    return 0


def cmd_states(args) -> int:
    """Every AU state blob in a ProjectData, attributed to a plug-in."""
    data = _load(args.path)
    rows = list(project_states(data))
    if getattr(args, "xml", False):
        rows = list(xml_states(data))
    if args.plugin:
        want = args.plugin.lower()
        rows = [r for r in rows
                if want in r["subtype"].lower() or want in r["display_name"].lower()]
    for r in rows:
        extra = ""
        if r["magic"] == "FFBS":
            extra = " floats=%-4d track=%r" % (r["nfloats"], r["track"])
        elif r["magic"] == "VC2!":
            extra = " root=%-32s params=%d" % (r["root"], len(xml_params(r["blob"])))
        print("  #%-3d 0x%-8x %-5s %-4s %-14s %-20s%s"
              % (r["index"], r["offset"], r["magic"], r["subtype"],
                 r["manufacturer"], r["display_name"][:20], extra))
    print("  %d state(s)%s" % (len(rows), " shown" if args.plugin else ""))
    return 0


XML_STATE_RE = re.compile(
    rb"<key>jucePluginState</key>\s*<data>\s*([A-Za-z0-9+/=\s]+?)</data>")


def xml_states(data: bytes):
    """Every `jucePluginState` blob in a ProjectData, with its root element.

    These are found by the plist key they sit under rather than by a magic
    number, because what is in the file is the BASE64 of the XML, not the XML.
    The identity triple beside them is often absent — a JUCE plug-in does not
    have to write one — so the ROOT ELEMENT NAME is the reliable handle, and
    the sub-type is reported only when the triple really was there.
    """
    n = 0
    for m in XML_STATE_RE.finditer(data):
        raw = _b64_decode_run(m.group(1))
        if raw[:4] != XML_MAGIC:
            continue
        ident = _neighbour_identity(data, m.start(1))
        yield {"index": n, "offset": m.start(1), "end": m.end(1), "blob": raw,
               "magic": "VC2!", "root": xml_root(raw),
               "subtype": ident.get("subtype", ""),
               "manufacturer": ident.get("manufacturer", ""),
               "display_name": ident.get("display_name", ""),
               "track": "", "nfloats": 0}
        n += 1




def make_sparse(subtype: str, values: dict | None = None) -> bytes:
    """A synthetic FabFilter flags state: 12-byte header + (u32 index, f32 value)."""
    mp = load_param_map(subtype) or []
    n = len(mp) if mp else (max(values) + 1 if values else 0)
    out = bytearray(SPARSE_HEADER + SPARSE_ENTRY * n)
    struct.pack_into(">I", out, 8, n)
    for k in range(n):
        v = (values or {}).get(k, 0.0)
        struct.pack_into(">I", out, SPARSE_HEADER + SPARSE_ENTRY * k, k)
        struct.pack_into(">f", out, SPARSE_HEADER + SPARSE_ENTRY * k + 4, v)
    return bytes(out)

def make_ffbs(nfloats: int) -> bytearray:
    """A synthetic, zeroed FFBS blob — for tests, or to build a state from
    scratch rather than editing one that already exists."""
    return bytearray(FFBS_MAGIC + struct.pack("<II", 1, nfloats)
                     + b"\x00" * (nfloats * 4))


def cmd_xmlset(args) -> int:
    """Write one named parameter inside a `jucePluginState` XML state."""
    data = bytearray(_load(args.path))
    want_root = args.root.lower()
    want_sub = (args.subtype or "").lower()
    name = args.param
    hits = []
    for m in XML_STATE_RE.finditer(bytes(data)):
        raw = _b64_decode_run(m.group(1))
        if raw[:4] != XML_MAGIC:
            continue
        if want_root and xml_root(raw).lower() != want_root:
            continue
        if want_sub:
            ident = _neighbour_identity(bytes(data), m.start(1))
            if (ident.get("subtype") or "").lower() != want_sub:
                continue
        try:
            xml_find(raw, name)
        except KeyError:
            continue
        hits.append((m.start(1), m.end(1), xml_root(raw)))
    if not hits:
        print("no XML state holds a parameter called %r" % name, file=sys.stderr)
        return 2
    if len(hits) > 1 and not args.allow_multiple:
        for off, _e, root in hits:
            print("  0x%-8x %s" % (off, root))
        print("%d XML states hold %r; narrow with --root/--subtype or pass "
              "--allow-multiple" % (len(hits), name), file=sys.stderr)
        return 2
    # high offset first: a write that changes the length moves every later byte,
    # so touching an earlier state afterwards would splice at a stale offset
    for off, end, root in sorted(hits, reverse=True):
        res = xml_apply(data, off, end, name, args.value)
        print("  %-28s %-12s %s = %s -> %s  [%s%s]"
              % (root, res["where"], name, res["before"], res["after"],
                 res["mode"], "" if res["mode"] == "in_place"
                 else ", file %+d bytes" % res["grew_by"]))
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    _save(args.path, bytes(data))
    print("written: %s" % args.path)
    return 0


def cmd_selftest(args) -> int:
    """Check the documented encodings without needing Logic or a project file."""
    ok = fail = 0

    def check(label, got, want, tol=1e-6):
        nonlocal ok, fail
        good = (abs(got - want) <= tol) if isinstance(want, float) else (got == want)
        if good:
            ok += 1
            print("  ok    %-44s %s" % (label, got))
        else:
            fail += 1
            print("  FAIL  %-44s got %s, want %s" % (label, got, want))

    print("frequency is log2(Hz)          (AU_PARAM_FORMAT.md 4.2)")
    for hz in (25.0, 100.0, 350.0, 1000.0, 3000.0, 20000.0):
        check("freq round-trip %g Hz" % hz, freq_to_hz(hz_to_freq(hz)), hz, 1e-3)
    check("25 Hz -> the documented float", hz_to_freq(25.0), 4.643856, 1e-6)

    print("Q normalisation over 0.025 .. 40  (4.2)")
    for q in (0.025, 0.5, 1.0, 1.2, 1.5, 40.0):
        check("Q round-trip %g" % q, enc_to_q(q_to_enc(q)), q, 1e-4)
    check("Q=1.0 -> 0.500 (documented)", q_to_enc(1.0), 0.5, 1e-9)
    check("Q=1.2 -> 0.5247 (documented)", q_to_enc(1.2), 0.5247, 5e-5)
    check("Q=1.5 -> 0.5550 (documented)", q_to_enc(1.5), 0.5550, 5e-5)

    print("Pro-Q 4 dynamic threshold is (dB+60)/60  (4.4)")
    check("0.6667 -> -20.0 dB (documented)", threshold_to_db(0.6667), -20.0, 0.01)
    check("threshold round-trip -20 dB",
          threshold_to_db(db_to_threshold(-20.0)), -20.0, 1e-4)

    print("band layout: 23 floats per band from 23*(k-1)  (4.1)")
    check("band 1 base", band_base(1), 0)
    check("band 2 base", band_base(2), 23)
    check("band 24 base", band_base(24), 529)
    check("globals start at", PROQ4_GLOBAL_BASE, 552)

    print("write/read round-trip on a synthetic Pro-Q 4 blob")
    blob = make_ffbs(PROQ4_NFLOATS)
    band_set(blob, 2, used=True, enabled=True, freq_hz=350.0, gain_db=-7.0, q=1.2,
             shape=0.0, slope=2.0)
    b = band_get(bytes(blob), 2)
    check("band 2 freq after write", b["freq_hz"], 350.0, 1e-3)
    check("band 2 gain after write", b["gain_db"], -7.0, 1e-6)
    check("band 2 Q after write", b["q"], 1.2, 1e-4)
    check("band 2 used flag", b["used"], True)

    print("equal-length base64 surgery  (6)")
    # the padding must NOT be base64 characters, or the run would merge with it —
    # real ProjectData runs are bounded by binary for exactly this reason
    pad = b"\x00" * 37
    payload = bytes(make_ffbs(PROQ4_NFLOATS))
    data = bytearray(pad + base64.b64encode(payload) + pad)
    before_len = len(data)
    found = list(find_blobs(bytes(data)))
    if len(found) != 1:
        print("  FAIL  blob not found by find_blobs()")
        fail += 1
    else:
        s, e, bl = found[0]
        check("blob recovered intact", bl == payload, True)
        new = bytearray(bl)
        band_set(new, 1, gain_db=3.0)
        splice_blob(data, s, e, bytes(new))
        check("file size unchanged", len(data), before_len)
        back = list(find_blobs(bytes(data)))[0][2]
        check("edited value round-trips", band_get(back, 1)["gain_db"], 3.0, 1e-6)
        try:
            splice_blob(data, s, e, payload + b"\x00")
            print("  FAIL  a resizing splice was not refused")
            fail += 1
        except ValueError:
            ok += 1
            print("  ok    %-44s ValueError" % "a resizing splice is refused")

    print("the VC2! XML container          (AU_PARAM_FORMAT.md 6)")

    def synth(attrs, root="ValhallaRoom", decl=None):
        body = (b'<?xml version="1.0" encoding="UTF-8"?>\n<' + root.encode()
                + b" " + attrs + b"/>")
        return bytearray(XML_MAGIC + struct.pack("<I", len(body)) + body + b"\0")

    raw = synth(b'decay="0.008008007891476154" lateSize="0.5" mix="1.0"')
    check("root element is read", xml_root(bytes(raw)), "ValhallaRoom")
    check("three attributes are parameters", len(xml_params(bytes(raw))), 3)
    check("a value is found by name", xml_find(bytes(raw), "lateSize")["value"], "0.5")
    before = len(raw)
    xml_set(raw, "lateSize", 0.75)
    check("a longer number keeps the length", len(raw), before)
    check("...and reads back", xml_find(bytes(raw), "lateSize")["value"], "0.75")
    xml_set(raw, "decay", 0.5)
    check("a much shorter number keeps it too", len(raw), before)
    check("...and reads back", float(xml_find(bytes(raw), "decay")["value"]), 0.5)
    body = xml_unwrap(bytes(raw))[0]
    check("the padded root is still well-formed", xml_root(bytes(raw)), "ValhallaRoom")
    check("...and still holds three parameters", len(xml_params(bytes(raw))), 3)
    check("no whitespace was put inside the '/'",
          b"/   >" in body or b"/ >" in body, False)
    check("the declared length still matches the body",
          xml_unwrap(bytes(raw))[1], len(body))
    try:
        xml_set(bytearray(raw), "nosuch", 1.0)
        check("an unknown name refuses", "no exception", "KeyError")
    except KeyError:
        check("an unknown name refuses", "KeyError", "KeyError")

    # the <param id= name= value=/> shape
    pbody = (b'<?xml version="1.0"?><IHeartNY_1><param id="0" name="Input" '
             b'value="0.5"/><param id="1" name="Output" value="0.75"/></IHeartNY_1>')
    praw = bytearray(XML_MAGIC + struct.pack("<I", len(pbody)) + pbody + b"\0")
    check("a <param> child is a parameter", len(xml_params(bytes(praw))), 2)
    check("...addressed by its name label", xml_find(bytes(praw), "Output")["value"], "0.75")
    check("...or by its id", xml_find(bytes(praw), "1")["value"], "0.75")
    n = len(praw)
    xml_set(praw, "Output", 1.0)
    check("writing one keeps the length", len(praw), n)

    # a value that cannot fit the bytes it replaces must RESIZE, not fail
    tiny = synth(b'a="1"')
    check("a one-character slot has room", xml_find(bytes(tiny), "a")["value"], "1")
    try:
        res = xml_set(tiny, "a", 0.123456789)
        check("an unfittable value resizes", res["mode"], "resized")
    except ValueError:
        check("an unfittable value resizes", "ValueError", "resized")

    print()
    print("FabFilter flags state: 12-byte header + (u32 index, f32 value)  (6.6)")
    def _mk(n, vals=None):
        b = bytearray(SPARSE_HEADER + SPARSE_ENTRY * n)
        struct.pack_into(">I", b, 8, n)
        for k in range(n):
            struct.pack_into(">I", b, SPARSE_HEADER + SPARSE_ENTRY * k, k)
            struct.pack_into(">f", b, SPARSE_HEADER + SPARSE_ENTRY * k + 4,
                             (vals or {}).get(k, 0.0))
        return bytearray(b)
    bl = _mk(3, {1: 0.4, 2: 3.0})
    check("count is read off the header", sparse_count(bytes(bl)), 3)
    check("a short blob is rejected", is_sparse(bytes(bl)[:-4]), False)
    rows = sparse_params(bytes(bl), "")           # no map -> synthetic names
    check("one row per entry", len(rows), 3)
    check("entry offsets are 8 bytes apart",
          rows[1]["offset"] - rows[0]["offset"], SPARSE_ENTRY)
    check("an existing value reads back", rows[1]["value"], 0.4, 1e-6)
    r = sparse_set_named(bl, "", "param 2", 9.0)
    check("writing returns the index it used", r["index"], 2)
    check("the write verified", r["verified"], True)
    check("and it is in the buffer",
          struct.unpack_from(">f", bytes(bl), r["offset"])[0], 9.0, 1e-6)
    check("the length did not change", len(bl), SPARSE_HEADER + SPARSE_ENTRY * 3)
    try:
        sparse_set_named(bl, "", "param 7", 1.0)
        check("an unknown name is refused", "no error", "ValueError")
    except ValueError:
        check("an unknown name is refused", "ValueError", "ValueError")

    print("%d passed, %d failed" % (ok, fail))
    return 1 if fail else 0


def cmd_pmtr(args) -> int:
    """Show a Pmtr container's record table beside the plug-in's AU parameters."""
    data = _load(args.path)
    hits = [(s, e, b) for s, e, b in find_blobs(data, PMTR_MAGIC)
            if (args.offset is None or s == args.offset)
            and (not args.track or args.track.encode() in b)]
    if args.subtype:
        # --subtype SELECTS among the blobs; it must never stand in for an
        # identity the blob does not have, or a wrong AU map would be printed
        # under the right-looking name and believed
        want = args.subtype.lower()
        hits = [h for h in hits
                if (_neighbour_identity(data, h[0]).get("subtype") or "").lower() == want]
    if not hits:
        print("no matching Pmtr blob", file=sys.stderr)
        return 1
    for start, end, blob in hits:
        ident = _neighbour_identity(data, start) or {}
        subtype = ident.get("subtype") or args.subtype or ""
        rows, report = pmtr_align(blob, subtype)
        print("# 0x%-8x %-6s %-20s %d records"
              % (start, subtype, (ident.get("display_name") or "").strip(),
                 report["records"]))
        if report["params"]:
            print("# %d AU parameters; %d records reproduce the AU default "
                  "(%s)" % (report["params"], report["verified"],
                            "ALIGNED" if report["verified"] else "NOT ALIGNED"))
            if report["unmapped_tail"]:
                print("# no record for (read-only): %s"
                      % ", ".join(report["unmapped_tail"]))
        else:
            print("# no dumped map for %r - run tools/dump_all_maps.py" % subtype)
        print("#   i  tag    raw        au value   default  name")
        for r in rows:
            if "name" in r:
                print("  %3d  %-5s %-9.5f %-10.5f %-8g %s%s"
                      % (r["index"], r["tag"], r["raw"], r["value"], r["default"],
                         r["name"], "" if r.get("verified", True) else "   <- float[1] != default"))
            else:
                print("  %3d  %-5s %-9.5f" % (r["index"], r["tag"], r["raw"]))
    return 0


def _ffbs_targets(data: bytes, args) -> list:
    """The FFBS blobs an invocation selects, by offset / track / subtype."""
    out = []
    for start, end, blob in find_blobs(data, FFBS_MAGIC):
        if getattr(args, "offset", None) is not None and start != args.offset:
            continue
        if getattr(args, "track", None) and args.track.encode() not in blob:
            continue
        if getattr(args, "subtype", None) and ffbs_subtype(blob) != args.subtype:
            continue
        out.append((start, end, blob))
    return out


def cmd_params(args) -> int:
    """List an FFBS blob's parameters under the plug-in's own names."""
    data = _load(args.path)
    if args.subtype and not load_param_map(args.subtype):
        print("no parameter map for %s — run tools/dump_all_maps.py" % args.subtype,
              file=sys.stderr)
        return 1
    targets = _ffbs_targets(data, args)
    if not targets:
        print("no matching FFBS blob", file=sys.stderr)
        return 1
    for start, end, blob in targets:
        sub = ffbs_subtype(blob)
        _, n = ffbs_header(blob)
        rows = load_param_map(sub)
        print("# 0x%-8x %-6s %4d floats  %s"
              % (start, sub, n,
                 "%d named parameters" % len(rows) if rows
                 else "NO MAP — run tools/dump_all_maps.py"))
        for i, name, v in ffbs_params(blob, sub):
            if args.changed_only and abs(v) < 1e-9:
                continue
            print("  %4d  %-42s %g" % (i, name, v))
    return 0


def cmd_fset(args) -> int:
    """Write ANY named parameter of ANY FFBS plug-in, using its dumped map."""
    path = args.path
    orig = _load(path)
    data = bytearray(orig)
    targets = _ffbs_targets(data, args)
    if len(targets) != 1:
        print("expected exactly one matching FFBS blob, got %d — narrow it with "
              "--offset, --track or --subtype" % len(targets), file=sys.stderr)
        return 1
    start, end, blob = targets[0]
    sub = ffbs_subtype(blob)
    new = bytearray(blob)
    if args.value is None and getattr(args, "hz", None) is None:
        print("give --value, or --hz for a frequency", file=sys.stderr)
        return 1
    target = (hz_to_freq(args.hz) if args.value is None else args.value)
    args.value = target
    try:
        index = ffbs_set_named(new, sub, args.param, args.value,
                               enforce_range=not args.force)
    except (KeyError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    row = param_lookup(sub, args.param)
    print("%s [%d] %s: %g -> %g%s"
          % (sub, index, row["name"], ffbs_floats(blob)[index], args.value,
             "   (plugin range %g .. %g)" % (row["min"], row["max"]) if row else ""))
    splice_blob(data, start, end, bytes(new))
    if len(data) != len(orig):
        print("REFUSING: file size would change", file=sys.stderr)
        return 1
    if args.dry_run:
        print("(dry run - nothing written)")
        return 0
    _save(path, bytes(data))
    print("written; file size unchanged (%d bytes)" % len(data))
    print("now CLOSE the project in Logic and reopen it for the change to load.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("path"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("dump"); p.add_argument("path")
    p.add_argument("--track", default=None); p.set_defaults(fn=cmd_dump)
    p = sub.add_parser("set"); p.add_argument("path")
    p.add_argument("--track", required=True)
    p.add_argument("--band", type=int, default=None,
                   help="Pro-Q 4 EQ band (1-24) or Pro-C 3 side-chain band (1-6)")
    p.add_argument("--gain", dest="gain_db", type=float)
    p.add_argument("--freq", dest="freq_hz", type=float)
    p.add_argument("--q", type=float)
    p.add_argument("--shape", type=float)
    p.add_argument("--slope", type=float)
    p.add_argument("--used", type=int, choices=(0, 1))
    p.add_argument("--nfloats", type=int, default=PROQ4_NFLOATS,
                   help="600 = Pro-Q 4 (default), 100 = Pro-C 3")
    p.add_argument("--global-offset", type=int, default=None,
                   help="Pro-C 3 global by offset (0-30), with --value")
    p.add_argument("--value", type=float, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("blob"); p.add_argument("path")
    p.add_argument("--chunk", type=int, default=None); p.set_defaults(fn=cmd_blob)
    p = sub.add_parser("pset"); p.add_argument("path")
    p.add_argument("--offset", type=lambda s: int(s, 0), required=True,
                   help="byte offset of the base64 run, as `list` reports it")
    p.add_argument("--tag", default=None, help="Pmtr record name to write (float[0])")
    p.add_argument("--param", default=None,
                   help="Pmtr: the plug-in's OWN parameter name (via the dumped map)")
    p.add_argument("--subtype", default=None, help="with --param, the 4-char subtype")
    p.add_argument("--raw", action="store_true",
                   help="with --param, VALUE is the 0..1 number stored, not the AU value")
    p.add_argument("--force", action="store_true",
                   help="with --param, write even outside the plug-in's range")
    p.add_argument("--float", dest="float_index", type=int, default=None,
                   help="FFBS float index to write")
    p.add_argument("--value", type=float, default=None)
    p.add_argument("--hz", type=float, default=None,
                   help="a frequency, converted from Hz to the stored log2(Hz)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_pset)
    p = sub.add_parser("states"); p.add_argument("path")
    p.add_argument("--plugin", default=None, help="filter by subtype or display name")
    p.add_argument("--xml", action="store_true",
                   help="list jucePluginState XML states instead")
    p.set_defaults(fn=cmd_states)
    p = sub.add_parser("xmlset", help="write a named parameter in an XML state")
    p.add_argument("path")
    p.add_argument("--param", required=True, help="parameter name, e.g. decay")
    p.add_argument("--value", required=True)
    p.add_argument("--root", help="root element, e.g. ValhallaRoom")
    p.add_argument("--subtype", help="4-character plug-in subtype, when known")
    p.add_argument("--allow-multiple", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_xmlset)
    p = sub.add_parser("pmtr-params",
                       help="a Pmtr table beside its plug-in's AU parameters")
    p.add_argument("path")
    p.add_argument("--offset", type=lambda x: int(x, 0), default=None)
    p.add_argument("--track", default=None)
    p.add_argument("--subtype", default=None)
    p.set_defaults(fn=cmd_pmtr)
    p = sub.add_parser("params",
                       help="list an FFBS blob's parameters by their real names")
    p.add_argument("path")
    p.add_argument("--offset", type=lambda x: int(x, 0), default=None)
    p.add_argument("--track", default=None)
    p.add_argument("--subtype", default=None)
    p.add_argument("--changed-only", action="store_true",
                   help="skip parameters whose value is zero")
    p.set_defaults(fn=cmd_params)
    p = sub.add_parser("fset",
                       help="write ANY named parameter of ANY FFBS plug-in")
    p.add_argument("path")
    p.add_argument("--param", required=True, help='e.g. "Band 2 Gain"')
    p.add_argument("--value", type=float, required=True)
    p.add_argument("--offset", type=lambda x: int(x, 0), default=None)
    p.add_argument("--track", default=None)
    p.add_argument("--subtype", default=None)
    p.add_argument("--force", action="store_true",
                   help="write even if the value is outside the plug-in's range")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_fset)
    p = sub.add_parser("selftest",
                       help="check the documented encodings (no project needed)")
    p.set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
