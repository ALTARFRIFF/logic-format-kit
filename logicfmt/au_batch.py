#!/usr/bin/env python3
"""
au_batch.py — apply a table of plug-in parameter edits across many
              channel-strip settings (.cst / .patch) or project files.

This is the batch half of the workflow described in AU_PARAM_FORMAT.md: where
`au_state.py` edits one value in one blob, this reads a declarative spec and
applies many edits, in one equal-length pass per file, with a before/after
report and a hard refusal if any file would change size.

Spec format (JSON)
------------------
{
  "files": [
    {
      "path": "l3out/D Kick Bus.patch/#Root.cst",
      "edits": [
        {"plugin": "SPL Transient Designer", "tag": "Attk", "value": 0.45},
        {"plugin": "FabFilter Pro-Q 4",        "band": 2, "gain_db": -3.0},
        {"plugin": "bx_boom V3",               "tag": "Boom", "value": 0.6}
      ]
    }
  ]
}

An edit addresses a plug-in inside the file by `plugin` (display name, or an
exact 4-character subtype such as "SPTP") and then names the parameter:

  * FabFilter (FFBS)   : either `float` (absolute index) or `band` + one of
                         freq_hz / gain_db / q / shape / slope / used / enabled
  * Brainworx/SPL/elysia (Pmtr): `tag` — writes float[0], leaving the metadata
                         floats untouched (see AU_PARAM_FORMAT.md §5.1)

Usage
-----
    au_batch.py apply spec.json [--dry-run] [--out DIR] [--report FILE]
    au_batch.py plugins <file.cst>          # what is in this file?
    au_batch.py spec-example                # print a starter spec
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import au_state as A  # noqa: E402

# --------------------------------------------------------------------------
# plug-in identity
# --------------------------------------------------------------------------

# display name -> 4-character AU subtype, harvested from real channel strips
PLUGIN_SUBTYPE = {
    # FabFilter
    "Pro-Q 4": "FQ4p", "Pro-Q 3": "FQ3p", "Pro-C 3": "FC3p", "Pro-C 2": "FC2p",
    "Pro-MB": "FMBp", "Pro-DS": "FDSx", "Pro-L 2": "FL2p", "Pro-R 2": "FR2p",
    "Pro-G": "FG4p", "Saturn 2": "FSt2", "Timeless 3": "FTL3", "Volcano 3": "FVL3",
    # Brainworx / Plugin Alliance
    "bx_boom V3": "bxb3", "bx_subfilter": "bxsf", "bx_meter": "bxmt",
    "bx_console Focusrite SC": "BAH2", "bx_console SSL 4000 E": "bx8j",
    "bx_townhouse": "bxth", "bx_digital V3": "bxa2", "bx_stereomaker": "bxsm",
    "bx_saturator": "bxf3", "bx_megasing": "bxgs", "bx_yellowdrive": "bxyd",
    "bx_solo": "bsle", "bx_subsynth": "Soma", "bx_blackdist": "MEQ4",
    # SPL
    "SPL Transient Designer": "SPTP", "SPL Transient Designer Plus": "SPTP",
    "SPL De-Verb": "SPDP", "SPL TwinTube": "SPTT",
    # elysia
    "elysia karacter": "krc1", "elysia museq": "mus1",
}

# subtype -> canonical display name (for the reverse lookup / reporting)
SUBTYPE_NAME = {}
for _n, _s in PLUGIN_SUBTYPE.items():
    SUBTYPE_NAME.setdefault(_s, _n)


def resolve_subtype(plugin: str) -> str:
    """Accept a display name, a subtype, or a case-insensitive variant."""
    if plugin in PLUGIN_SUBTYPE:
        return PLUGIN_SUBTYPE[plugin]
    p = plugin.strip()
    if len(p) == 4 and (p in SUBTYPE_NAME or A.load_param_map(p)):
        return p
    low = p.lower()
    # exact (case-insensitive) first, then containment in EITHER direction so
    # both "Pro-Q 4" and "FabFilter Pro-Q 4" resolve to FQ4p
    for name, sub in PLUGIN_SUBTYPE.items():
        if name.lower() == low:
            return sub
    for name, sub in PLUGIN_SUBTYPE.items():
        n = name.lower()
        if low in n or n in low:
            return sub
    raise KeyError("unknown plug-in %r — pass an exact 4-character subtype "
                   "(known: %s)" % (plugin, ", ".join(sorted(set(PLUGIN_SUBTYPE.values())))))


# --------------------------------------------------------------------------
# per-file editing
# --------------------------------------------------------------------------

FFBS_BAND_FIELDS = {
    "freq_hz": "freq_hz", "freq": "freq_hz",
    "gain_db": "gain_db", "gain": "gain_db",
    "q": "q", "shape": "shape", "slope": "slope",
    "used": "used", "enabled": "enabled",
}


def _edit_ffbs(blob: bytearray, edit: dict, label: str, subtype: str | None = None):
    """Return (description, before, after).

    Three ways to address a parameter, in order of how much you have to know:

      {"param": "Band 2 Gain", "value": -3.0}   by the plug-in's own name — works
                                                for ANY FFBS plug-in with a dumped
                                                map (tools/dump_all_maps.py)
      {"float": 26, "value": -3.0}              by absolute float index
      {"band": 2, "gain_db": -3.0}              Pro-Q 4 / Pro-C 3 band shorthand
    """
    if "param" in edit:
        sub = subtype or A.ffbs_subtype(bytes(blob))
        idx = A.param_lookup(sub, edit["param"])
        if idx is None:
            rows = A.load_param_map(sub)
            if not rows:
                raise ValueError("%s: no parameter map for %s — run "
                                 "tools/dump_all_maps.py" % (label, sub))
            raise ValueError("%s: %s has no parameter named %r"
                             % (label, sub, edit["param"]))
        i = idx["index"]
        before = A.ffbs_floats(bytes(blob))[i]
        # `hz` is a courtesy: a frequency column stores log2(Hz), and writing
        # "40" for 40 Hz is refused as out of range by the map's own 3.32..14.87.
        if "value" in edit:
            v = float(edit["value"])
        elif "hz" in edit:
            v = A.hz_to_freq(float(edit["hz"]))
        elif "db" in edit:
            v = float(edit["db"])
        else:
            raise ValueError('%s: a named edit needs "value" (or "hz"/"db")'
                             % label)
        if not edit.get("force") and not (idx["min"] <= v <= idx["max"]):
            raise ValueError("%s: %s is out of range: %g not in %g .. %g"
                             % (label, idx["name"], v, idx["min"], idx["max"]))
        A.ffbs_set_float(blob, i, v)
        return ("%s[%d]" % (idx["name"], i), before, v)
    if "float" in edit:
        idx = int(edit["float"])
        before = A.ffbs_floats(bytes(blob))[idx]
        A.ffbs_set_float(blob, idx, float(edit["value"]))
        return ("float[%d]" % idx, before, float(edit["value"]))
    if "band" not in edit:
        raise ValueError("%s: FFBS edit needs `float` or `band`" % label)
    band = int(edit["band"])
    fields = {k: edit[k] for k in FFBS_BAND_FIELDS if k in edit}
    if not fields:
        raise ValueError("%s: band %d needs one of %s"
                         % (label, band, "/".join(sorted(set(FFBS_BAND_FIELDS.values())))))
    before = A.band_get(bytes(blob), band)
    written = A.band_set(blob, band, **fields)
    after = A.band_get(bytes(blob), band)
    changed = ", ".join("%s %r->%r" % (w, before[w], after[w]) for w in written
                        if before.get(w) != after.get(w))
    return ("band %d: %s" % (band, changed or "no change"), None, None)


def _edit_sparse(blob: bytearray, edit: dict, label: str, subtype: str):
    """Edit a FabFilter flags state: a sparse (u32 index, f32 value) table."""
    param = edit.get("param")
    if param and "value" in edit:
        name, value = param, edit["value"]
    elif edit.get("float") is not None and "param" in edit:
        name, value = edit["param"], edit["float"]
    else:
        index = edit.get("index", edit.get("float"))
        if index is None or "param" not in edit:
            raise ValueError('%s: a flags-state edit needs {"param": ..., '
                             '"value": ...}' % label)
        rows = A.sparse_params(bytes(blob), subtype)
        if not isinstance(index, int) or not 0 <= index < len(rows):
            raise ValueError("%s: parameter index %r is not in 0..%d"
                             % (label, index, len(rows) - 1))
        name, value = rows[index]["name"], edit["param"]
    r = A.sparse_set_named(blob, subtype, name, float(value))
    return ("%s/%s[%d]" % (subtype, r["name"], r["index"]),
            r["before"], r["after"])


def _edit_pmtr(blob: bytearray, edit: dict, label: str, subtype: str | None = None):
    """Edit one Pmtr record.

    Two addressing modes, and they differ in what the number MEANS:

      {"tag": "Attk",  "value": 0.40}   -> the raw normalised float, float[0]
      {"param": "Attack", "value": 0.4} -> the plug-in's OWN value (needs the
                                           subtype so its AU parameter list can
                                           be read); use "raw" instead of
                                           "value" to give the normalised float
                                           while still addressing by name.

    The record table is the plug-in's AU parameter list in order (see
    AU_PARAM_FORMAT.md §5.1), so a name resolves to a record index.
    """
    if "param" in edit:
        sub = edit.get("subtype") or subtype
        if not sub:
            raise ValueError("%s: Pmtr `param` needs the plug-in's subtype — "
                             "add \"subtype\": \"SPTP\"" % label)
        sub = A.fourcc(sub) if isinstance(sub, (bytes, bytearray)) else sub
        if "value" in edit:
            value, raw_mode = float(edit["value"]), False
        elif "raw" in edit:
            value, raw_mode = float(edit["raw"]), True
        else:
            raise ValueError("%s: Pmtr `param` needs `value` (plug-in units) "
                             "or `raw` (normalised)" % label)
        info = A.pmtr_set_named(blob, sub, edit["param"], value,
                                from_raw=raw_mode,
                                enforce_range=not edit.get("force"))
        return ("%s[%d] %s" % (info["tag"], info["index"], info["name"]),
                info["before"], info["after"])

    if "tag" not in edit:
        raise ValueError("%s: Pmtr edit needs `tag` or `param`" % label)
    tag = edit["tag"]
    recs = {t: v for _o, t, v in A.pmtr_records(bytes(blob))}
    if tag not in recs:
        raise KeyError("%s: no record %r (has: %s)"
                       % (label, tag, ", ".join(sorted(recs))))
    before = recs[tag][0]
    if not A.pmtr_set_value(blob, tag, float(edit["value"])):
        raise ValueError("%s: record %r not writable" % (label, tag))
    return ("%s[0]" % tag, before, float(edit["value"]))


def apply_to_file(path: str, edits, out_path=None, dry_run=False):
    """Apply every edit.  Returns (results, ok)."""
    orig = A._load(path)
    data = bytearray(orig)
    results = []

    # collect the state blobs once, with their identities
    is_cst = path.endswith((".cst", ".patch")) or path.endswith("/#Root.cst")
    states = []
    if is_cst:
        for i, off, total, d in A.cst_states(data):
            key = A.state_key(d)
            raw = d[key]
            if key == "jucePluginState" and raw[:4] == A.XML_MAGIC:
                # a VC2! chunk is addressed by its XML root element, not by a
                # FourCC that may not describe it
                root = A.xml_root(raw) or "?"
                states.append({"kind": "cstxml", "idx": i, "off": off,
                               "total": total, "root": root, "display": root,
                               "dict": d, "start": off, "end": off + total})
                continue
            states.append({"kind": "cst", "idx": i, "off": off, "total": total,
                           "sub": A.fourcc(d["subtype"]),
                           "display": A.fourcc(d["subtype"]),
                           "dict": d, "start": off, "end": off + total})
    else:
        # A project holds every plug-in state as its own base64 run; the identity
        # comes from the plaintext plist beside it (au_state.project_states).
        for st in A.project_states(data):
            states.append({"kind": "proj", "start": st["offset"], "end": st["end"],
                           "sub": st["subtype"], "display": st["display_name"],
                           "index": st["index"], "track": st.get("track", "")})
        # the third container: VC2! + plaintext XML under `jucePluginState`.
        # Its handle is the XML ROOT ELEMENT, because a JUCE plug-in need not
        # write an identity triple next to it.
        for st in A.xml_states(data):
            states.append({"kind": "xml", "start": st["offset"], "end": st["end"],
                           "sub": st["subtype"], "root": st["root"],
                           "display": st["root"], "index": st["index"],
                           "track": st.get("track", "")})

    # PHASE 1 — resolve every edit to the state blobs it names.  Nothing is
    # written yet, so a spec that addresses something that is not there fails
    # as a whole before the first byte moves.
    plan = [[] for _ in edits]
    for i, edit in enumerate(edits):
        label = edit.get("_label", json.dumps(edit))
        root = edit.get("root")
        if root:
            hits = [st for st in states if st["kind"] in ("xml", "cstxml")
                    and st["root"].lower() == root.lower()]
            want = "root %s" % root
        else:
            try:
                want = resolve_subtype(edit["plugin"])
            except KeyError as e:
                results.append({"edit": label, "ok": False, "error": str(e)})
                continue
            hits = [st for st in states if st.get("sub") == want]
        if edit.get("track"):
            hits = [st for st in hits
                    if edit["track"].lower() in st.get("track", "").lower()]
        if edit.get("instance") is not None:
            k = int(edit["instance"])
            hits = [hits[k]] if k < len(hits) else []
        if edit.get("offset") is not None:
            # the exact address of one state blob, as `au_state.py list` prints it
            want_off = (int(edit["offset"], 0) if isinstance(edit["offset"], str)
                        else int(edit["offset"]))
            hits = [st for st in hits if st["start"] == want_off]

        if not hits:
            results.append({"edit": label, "ok": False,
                            "error": "%s not found%s in this file"
                                     % (want, " for track %r" % edit["track"]
                                        if edit.get("track") else "")})
            continue
        if len(hits) > 1 and not edit.get("allow_multiple"):
            results.append({"edit": label, "ok": False,
                            "error": "%d instances of %s — narrow it with \"track\", "
                                     "\"offset\" or \"instance\", or add "
                                     "\"allow_multiple\": true" % (len(hits), want)})
            continue
        plan[i] = list(hits)

    # PHASE 2 — apply.  An XML write may not fit the bytes its predecessor
    # occupied, and then the file RESIZES; every later offset would move, so
    # the states are walked from the end of the file backwards.
    work = sorted(((st["start"], i, st) for i, hs in enumerate(plan) for st in hs),
                  key=lambda t: -t[0])
    for _off, i, st in work:
        edit = edits[i]
        label = edit.get("_label", json.dumps(edit))
        try:
            if st["kind"] in ("cst", "cstxml"):
                desc, before, after = _edit_cst_state(data, st, edit, label)
            elif st["kind"] == "xml":
                r = A.xml_apply(data, st["start"], st["end"], edit["param"],
                                edit["value"])
                desc, before, after = ("%s/%s" % (st["root"], r["where"]),
                                       r["before"], r["after"])
                if r["mode"] == "resized":
                    desc += " (resized %+d)" % r["grew_by"]
            else:
                raw = A._b64_decode_run(bytes(data[st["start"]:st["end"]]))
                new = bytearray(raw)
                if raw[:4] == A.PMTR_MAGIC:
                    desc, before, after = _edit_pmtr(
                        new, edit, label, subtype=st.get("sub"))
                else:
                    desc, before, after = _edit_ffbs(
                        new, edit, label, subtype=st.get("subtype"))
                A.splice_blob(data, st["start"], st["end"], bytes(new))
            results.append({"edit": label, "ok": True, "where": desc,
                            "before": before, "after": after})
            plan[i].remove(st)
        except Exception as e:
            results.append({"edit": label, "ok": False, "error": "%s: %s"
                            % (type(e).__name__, e)})
            plan[i].remove(st)

    if len(data) != len(orig):
        results.append({"edit": "FILE", "ok": True, "resized": len(data) - len(orig),
                        "note": "a value did not fit its predecessor's bytes, so the "
                                "file is %+d bytes; every offset after it moved"
                                % (len(data) - len(orig))})

    if not dry_run and any(r["ok"] for r in results):
        target = out_path or path
        if out_path and out_path != path:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            shutil.copyfile(path, out_path)
        A._save(target, bytes(data))
    return (results, all(r["ok"] for r in results))


def _edit_xml(blob: bytearray, edit: dict, label: str):
    """Edit a `jucePluginState` XML state inside a `.cst` chunk.

    STRICTLY equal-length, unlike the same container in a ProjectData.  A CST
    chunk carries its own length field, so a state that grows means rewriting
    that field and every offset after it — the surgery that made Logic refuse
    to parse a strip.  `xml_set` pads with ignorable whitespace instead, and
    when even that has run out this REFUSES rather than growing.
    """
    param = edit.get("param")
    if not param:
        raise ValueError("%s: an XML edit needs \"param\"" % label)
    root = edit.get("root")
    if root and A.xml_root(bytes(blob)) != root:
        raise ValueError("%s: root is %r, not %r"
                         % (label, A.xml_root(bytes(blob)), root))
    n = len(blob)
    r = A.xml_set(blob, param, edit["value"], allow_resize=False)
    if len(blob) != n:
        raise AssertionError("XML length changed %d -> %d" % (n, len(blob)))
    return ("%s/%s" % (A.xml_root(bytes(blob)), param),
            r.get("before"), r.get("after"))


def _edit_cst_state(data: bytearray, st, edit, label):
    """Edit a plug-in state inside a .cst chunk, keeping every length valid.

    The plist is re-read from `data` on every call, never taken from the
    enumeration snapshot in `st["dict"]`.  Two edits to the SAME chunk would
    otherwise each start from the original bytes and the later one would
    silently undo the earlier — the report said OK for both, and only the
    second was in the file.
    """
    import plistlib
    body = bytes(data[st["off"]:st["off"] + st["total"]])
    s0 = body.find(b"<?xml")
    e0 = body.find(b"</plist>") + len(b"</plist>")
    d = plistlib.loads(body[s0:e0])
    key = A.state_key(d)
    raw = bytearray(d[key])
    if raw[:4] == A.PMTR_MAGIC:
        desc, before, after = _edit_pmtr(
            raw, edit, label, subtype=A.fourcc(d["subtype"]))
    elif raw[:4] == A.XML_MAGIC:
        desc, before, after = _edit_xml(raw, edit, label)
    elif d.get("FabFilterFlags") == 1 and A.is_sparse(raw):
        # no FFBS header: the flags container, a sparse index/value table
        desc, before, after = _edit_sparse(
            raw, edit, label, A.fourcc(d.get("subtype", "----")))
    else:
        desc, before, after = _edit_ffbs(raw, edit, label,
                                         subtype=A.ffbs_subtype(bytes(raw)))
    _write_cst_state(data, st["off"], st["total"], key, bytes(raw))
    return desc, before, after


def _write_cst_state(data: bytearray, off: int, total: int, key: str, new_raw: bytes):
    """Replace the base64 of a <data>/<FabFilterPluginState> element in place."""
    import base64
    import re
    body = bytes(data[off:off + total])
    s = body.find(b"<?xml")
    e = body.find(b"</plist>") + len(b"</plist>")
    plist = body[s:e]
    m = re.search((r"<key>%s</key>\s*<data>(.*?)</data>" % re.escape(key)).encode(),
                  plist, re.S)
    if not m:
        raise ValueError("no <%s> data element in chunk" % key)
    inner = m.group(1)
    clean = re.sub(rb"\s", b"", inner)
    if len(base64.b64decode(clean + b"===")) != len(new_raw):
        raise ValueError("state length would change — refusing")
    enc = base64.b64encode(new_raw)
    out = bytearray(inner)
    j = 0
    for i in range(len(inner)):
        if inner[i] not in A.B64_WS:
            out[i] = enc[j]
            j += 1
    abs_start = off + s + m.start(1)
    data[abs_start:abs_start + len(inner)] = bytes(out)


def cmd_plugins(args) -> int:
    buf = A._load(args.path)
    print("%s:" % args.path)
    for i, off, total, d in A.cst_states(buf):
        raw = d[A.state_key(d)]
        print("  chunk %-2d off=0x%-6x len=%-6d %-6s %s/%s  %d B"
              % (i, off, total, A.state_key(d), A.fourcc(d["manufacturer"]),
                 A.fourcc(d["subtype"]), len(raw)))
    return 0


EXAMPLE = {
    "files": [
        {"path": "l3out/D Kick Bus.patch/#Root.cst",
         "edits": [
             {"plugin": "SPL Transient Designer", "tag": "Attk", "value": 0.45},
             {"plugin": "bx_boom V3", "tag": "Boom", "value": 0.6},
             {"plugin": "FabFilter Pro-Q 4", "band": 2, "gain_db": -3.0},
         ]},
    ]
}


def cmd_spec_example(args) -> int:
    print(json.dumps(EXAMPLE, indent=2))
    return 0


def cmd_apply(args) -> int:
    spec = json.load(open(args.spec))
    dest = args.out
    all_ok = True
    total = 0
    for entry in spec.get("files", []):
        path = entry["path"]
        edits = entry["edits"]
        out_path = None
        if dest:
            root = entry.get("root")
            if not root and len(spec["files"]) > 1:
                root = os.path.commonpath([e["path"] for e in spec["files"]])
            rel = os.path.relpath(path, root) if root else None
            if not rel or rel.startswith(".."):
                # a path outside the common root would come back as a
                # "../../.." chain — keep the part inside the .logicx bundle
                parts = path.split(os.sep)
                i = max((k for k, c in enumerate(parts) if c.endswith(".logicx")),
                        default=None)
                rel = os.path.join(*parts[i + 1:]) if i is not None else os.path.basename(path)
            out_path = os.path.join(dest, rel)
        # label each edit for the report
        for e in edits:
            e["_label"] = "%s::%s" % (os.path.basename(os.path.dirname(path)) or path,
                                      e.get("tag") or e.get("param") or ("band %s" % e.get("band")))
        results, ok = apply_to_file(path, edits, out_path=out_path, dry_run=args.dry_run)
        total += len(results)
        all_ok = all_ok and ok
        print("== %s%s" % (path, "  ->  " + out_path if out_path else ""))
        for r in results:
            if r["ok"]:
                if r.get("before") is None:
                    print("   OK   %-46s %s" % (r["edit"], r["where"]))
                else:
                    print("   OK   %-46s %s: %r -> %r"
                          % (r["edit"], r["where"], r["before"], r["after"]))
            else:
                print("   FAIL %-46s %s" % (r["edit"], r["error"]))
    print()
    print("%d edit(s); %s%s" % (total, "all applied" if all_ok else "SOME FAILED",
                                " (dry run)" if args.dry_run else ""))
    return 0 if all_ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apply"); p.add_argument("spec")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", default=None, help="write edited copies under this dir")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("plugins"); p.add_argument("path"); p.set_defaults(fn=cmd_plugins)
    p = sub.add_parser("spec-example"); p.set_defaults(fn=cmd_spec_example)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
