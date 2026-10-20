#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Waves plug-in state — the 516-byte ``data`` key, not the XML.

Waves carries TWO state keys in its ClassInfo:

    Waves_XPst   5-6 KB of text.  ``<PresetChunkXMLTree>`` with a flat list of
                 429 positional tokens.  Looks like the state, is not: the
                 plug-in DISCARDS a hand-edited copy (verified: 410 single-token
                 perturbations, every one silently ignored).
    data         516 bytes, ALWAYS the same length.  This is the real thing.

``data`` is a 12-byte header followed by 8-byte records::

    [u32 big-endian parameter id][f32 big-endian value]  x 63

The parameter id is stored IN the record, so no index table is needed — the
plug-in's own serialisation names its own parameters. Writing a value is an
equal-length ``struct.pack_into`` of the f32: nothing shifts, no length field is
touched, which is the same discipline the ``.cst`` writer uses.

Verified against Abbey Road RS124 (``aufx RS1M ksWV``): setting the record whose
id is 6 to 250.0, id 5 to 500.0 and id 1 to 3.0 and handing the blob back to the
plug-in makes its own parameter readback report exactly 250 / 500 / 3.
"""

import struct

HEADER = 12                        # bytes before the first record
RECORD = 8                         # [u32 id][f32 value]
DATA_LEN = 516                     # Waves writes this fixed size every time
PARAM_COUNT = (DATA_LEN - HEADER) // RECORD     # 63


def is_waves(data):
    """True when *data* looks like the Waves record table."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    if len(data) < HEADER + RECORD or (len(data) - HEADER) % RECORD:
        return False
    seen = set()
    for off in range(HEADER, len(data), RECORD):
        pid, = struct.unpack_from(">I", data, off)
        val, = struct.unpack_from(">f", data, off + 4)
        if pid >= 1 << 20:                 # ids are small ordinals
            return False
        if val != val or abs(val) > 1e12:  # NaN / absurd
            return False
        if pid in seen:                    # ids are unique
            return False
        seen.add(pid)
    return len(seen) >= 8                  # a real table has plenty of records


def records(data):
    """Decode the record table to ``[{offset, id, value}, ...]``."""
    out = []
    for off in range(HEADER, len(data) - RECORD + 1, RECORD):
        pid, = struct.unpack_from(">I", data, off)
        val, = struct.unpack_from(">f", data, off + 4)
        out.append(dict(offset=off, id=pid, value=val))
    return out


def get(data, param_id):
    """Current value of *param_id*, or None when the table has no such record."""
    for r in records(data):
        if r["id"] == param_id:
            return r["value"]
    return None


def set_value(data, param_id, value):
    """Write *value* into *param_id*'s record, in place. Returns True on success."""
    if not isinstance(data, bytearray):
        raise TypeError("pass a bytearray: the record is patched in place")
    for r in records(data):
        if r["id"] == param_id:
            struct.pack_into(">f", data, r["offset"] + 4, float(value))
            return True
    return False


def by_name(name_map):
    """Turn an ``{id: name}`` map into ``{name: id}`` for callers working by name."""
    return {v: k for k, v in name_map.items()}


def build(param_values, header=None):
    """Synthesise a record table from ``{id: value}``. Handy for tests."""
    head = header if header is not None else bytes(HEADER)
    ids = sorted(param_values)
    body = b"".join(struct.pack(">If", pid, float(param_values[pid])) for pid in ids)
    return bytearray(head + body)


def _selftest():
    ok = failed = 0

    def check(label, cond):
        nonlocal ok, failed
        if cond:
            ok += 1
        else:
            failed += 1
            print("  FAIL %s" % label)

    table = build({0: 500.0, 1: 0.0, 2: 1.0, 3: 2.5, 4: 3.0,
                   5: 1000.0, 6: 500.0, 7: 1.0})
    check("is_waves accepts a synthesised table", is_waves(table))
    check("is_waves rejects the XML payload", not is_waves(b"<PresetChunkXMLTree>"))
    check("is_waves rejects a truncated table", not is_waves(table[:20]))
    check("record count", len(records(table)) == len(table[HEADER:]) // RECORD)
    check("get", get(table, 5) == 1000.0)
    check("get missing", get(table, 99) is None)
    check("set_value", set_value(table, 5, 500.0) and get(table, 5) == 500.0)
    check("set_value keeps length", len(table) == HEADER + 8 * 8)
    check("set_value unknown id", not set_value(table, 99, 1.0))
    check("by_name", by_name({5: "Mix"}) == {"Mix": 5})

    real = bytearray(DATA_LEN)
    struct.pack_into(">I", real, 0, 0x1234)
    for k in range(PARAM_COUNT):
        struct.pack_into(">If", real, HEADER + k * RECORD, k, float(k) * 1.5)
    check("real-shaped 516B accepted", is_waves(real) and len(records(real)) == PARAM_COUNT)
    check("write inside the 516B table", set_value(real, 7, -12.0) and get(real, 7) == -12.0)
    check("header untouched", struct.unpack_from(">I", real, 0)[0] == 0x1234)

    print("waves: %d passed, %d failed" % (ok, failed))
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
