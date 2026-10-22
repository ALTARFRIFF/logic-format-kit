# logic-format-kit

**Format notes and a reference implementation for Logic Pro's `.patch` / `.cst` / `.logicx` files.**

For anyone who needs to **generate or modify Logic files outside Logic** — especially so that an AI agent can do it autonomously.

> This is not an official specification. Everything here was derived by diffing files Logic itself wrote and confirming the result by opening them in Logic. Every claim is tagged with how strongly it was verified.

---

## What problem this solves

Logic Pro's file formats are undocumented. If you want to produce 77 channel-strip presets in bulk, or have a script change a project's time signature, there is no supported path: no CLI, no API, and `.logicx` is a binary package whose internal structure is unknown.

This kit decodes those formats far enough to read and write them directly, and — just as importantly — documents **what to do when decoding fails**.

---

## Capability matrix (honest edition)

| Capability | Status | Notes |
|---|---|---|
| Read/write `.patch` / `.cst` structure | ✅ **measured** | block stream, insert slots, names, UUIDs, add/remove plug-ins |
| Write plug-in parameters (FabFilter) | ✅ **measured** | Pro-Q 4 / Pro-C 3 / Pro-DS / Pro-MB |
| Write plug-in parameters (Plugin Alliance family) | ✅ **measured** | Pmtr container; includes SPL / elysia / Maag / Brainworx |
| Write plug-in parameters (Valhalla / JUCE) | ✅ **measured** | VC2! XML |
| Write plug-in parameters (iZotope) | 🟡 **decoded, not round-tripped** | 16-byte header + zlib + JSON |
| Write plug-in parameters (Waves) | ✅ **`data` record table decoded** | `[u32 param id][f32 value]`; f32 patched in place |
| Write **any** plug-in parameter without knowing its container | ✅ **verified on 8 plug-ins / 6 families** | L2 `logicfmt/classinfo.py`, see below |
| Write `.logicx` time signature (any bar) | ✅ **measured** | passed a 6-point acceptance check |
| Write `.logicx` key signature (any bar, major/minor, sharps/flats) | ✅ **measured** | 7 files, 13 records, no exceptions |
| Write `.logicx` chord track | ✅ **measured** | 55 records, no exceptions, confirmed in Logic |
| Read/write fader, pan, mute, solo, output routing, sends | ✅ **verified** | `logicfmt/mixer.py`; dB curve calibrated at ten points |
| Read/write aux / stack main tracks (kind, name) | ✅ **verified** | `ivnE(467)`, one byte `+155` (aux / summing / folder) |
| Stack membership | ✅ **verified** | folder = strip `+110`; summing = output routing |
| Create an audio track from scratch (**runtime**) | ✅ **usable** | Logic creates it, everything else is offline, see `docs/07` |
| Create an audio track from scratch (**pure offline**) | ✅ **verified in Logic** | `logicfmt/project.py: add_audio_track()`, see `docs/07` |
| Create a whole project from scratch (**pure offline**) | ✅ **verified in Logic** (1-4 tracks) | `project.create_project()`: 5-file skeleton + rename + tempo; `_NEW-T3` (3 tracks / 96 BPM), `_NEW-T4E` (4 tracks / 110 BPM); >4 tracks refused, see `docs/07` §12 |
| Create a summing stack from scratch (**pure offline**) | ✅ **verified in Logic** | `add_stack(kind="summing")`: `_S-BASE` -> 132,596 B / 452 blocks, identical `(tag,length)` sequence to the hand-made `_S-STACK`; same UUID written in three places |
| Real insertion (grow the file) | ✅ **verified** | `insert_in_payload` + `seal`, tested on a `qSvE` record |
| Delete an audio track (any position) | ✅ **verified in Logic** | `project.remove_audio_track()`; retired row `+81=0x08`, `ivnE+455` pairing, `karT` family `0x40000`; verified on the last track and on a middle track |
| Move / copy a MIDI region | ✅ **verified in Logic** | `midi.move_region()` (two tick fields only) / `midi.copy_region()` (donor object + whole note container); `_MV-TEST` read 1/5/9/15/17, `_CPY2` read 6 regions with identical notes |
| Markers | ✅ move/rename verified · 🟡 create offline unresolved | `logicfmt/markers.py`; blocker = `qSvE` object ids must match the post-save numbering (the gnoS 80 bytes are disproven), see findings §50.8 |
| MIDI notes (add / edit inside a region) | ✅ **verified in Logic** | `logicfmt/midi.py`: 32-byte records in a `qSvE` container |
| MIDI controller values (CC64 sustain / CC11 / CC1) | ✅ **verified in Logic** | `0xb0`, 48-byte record: `+11` value, `+12` number |
| Add a MIDI controller event | 🟡 refused while the container mixes note records | — |
| Create a MIDI region itself | ✅ **verified in Logic** | `midi.add_region()`: donor object + one 40-byte pair in the `qSvE` registry; `_MR14` read 5 regions |
| Generate the `.logicx` chunk stream from nothing | ❌ not recommended | use the 5-file skeleton (`create_project()`) instead |

> The three remaining gaps (create a marker, create a send, the `PvWr`/`FrAr` containers),
> with their evidence and unlock conditions, are collected in [`docs/08-roadmap.md`](docs/08-roadmap.md).
| Insert/delete fixed-size records | ❌ not implemented | use template slots |
| Generate a `.logicx` from scratch | ❌ not recommended | template + rewrite |

**Coverage figure**: of 747 plug-in state blobs in the measured sample library, **732 (98.0%)** are addressable by parameter name — 62,638 named parameters in total.

---

## Three rules that carry the whole thing

1. **Don't hand-build containers — let Logic write them.** Save a template, then rewrite fields that already exist in it.
2. **Prefer equal-length writes.** If you never change a length, you never have to fix a parent length field.
3. **Verify every write independently.** Logic will not report an error — it will simply not take effect.

Rules 1 and 2 carried 77 generated patches and the project-level time/key/chord writes. Rule 3 has saved the project more than once.

---

## Fallback chain

| Layer | Mechanism | Coverage | Needs Logic installed |
|---|---|---|---|
| **L1** container-direct | detect magic → codec → equal-length byte surgery | 98% (measured) | no |
| **L2** AU self-serialisation | ask the plug-in for its own `ClassInfo` | **verified, all 6 families** | no |
| **L3** runtime | set it in Logic, then save | 100% | yes |

> **L2 is done and verified.** `AudioUnitGetProperty(kAudioUnitProperty_ClassInfo)` returns the plug-in's own serialised state, so **you never need to know any container format at all**.
>
> ```bash
> logicfmt/classinfo.py probe aufx SPTP SPL1     # list the keys it emits
> logicfmt/classinfo.py set   aufx FQ4p FabF 2 8.45
> logicfmt/classinfo.py selftest                 # 11 passed / 0 failed
> ```
>
> Measured: Plugin Alliance / SPL output is **byte-identical** to what Logic stores; FabFilter / Valhalla / Swivel / ADPTR / iZotope all write. Two traps: **JUCE parameter IDs are name hashes** (not indices), and **accconus ERA 6 accepts the value but its state does not change** — so never trust `status == noErr`; read the parameter back *and* diff the blob.

---

## Layout

```
CORE.md                       ← **read this first**: decision tree, rules, fallback chain, unknowns
docs/
  01-cst-format.md            block stream, the two slot fields, five UUIDs, name limits
  02-au-containers.md         five plug-in state containers, parameter addressing, L2 (verified)
  03-project-timeline.md      .logicx structure, time signature / key / chords
  04-runtime-and-templates.md how to build templates, what runtime tools can do, who does what
  05-verification.md          verification checklist, five forms of false success
  06-failure-modes.md         symptom → cause → fix
  07-track-creation.md        track creation: every measured fact + the blocker
logicfmt/                     reference implementation (Python 3, stdlib only)
  au_state.py                 the five containers: read / write / verify
  au_batch.py                 declarative batch writer (spec JSON -> edits)
  mkpatch.py                  assemble a channel strip from template .patch files
  classinfo.py                L2: let the plug-in serialise itself (any-plug-in fallback)
  meterkey.py                 time signature / key signature codec
  mixer.py                    fader / pan / mute / solo / output / sends (39 assertions)
  chord.py                    chord-track codec
  build_test_*.py             generate a test project from a template
tools/                        parameter-map dumpers
templates/MANIFEST.md         template inventory and specs
```

---

## Thirty-second start

Python 3 standard library only.

```bash
python3 logicfmt/chord.py       # chord codec selftest (no Logic, no files needed)
python3 logicfmt/meterkey.py    # time/key codec selftest
python3 -m logicfmt.mixer       # channel strip + sends selftest
```

Once you have templates:

```bash
python3 logicfmt/build_test_chord.py --dry-run    # show what it would change
python3 logicfmt/build_test_chord.py             # do it
```

**Templates ship with the kit** (`templates/`) — use them as-is; you don't need to build your own.
Only two cases require rebuilding: **a different Logic version**, or **not enough slots** (`docs/04 §2.6`).

---

## Scope and caveats

- Measured against **Logic Pro 12.4, build 6707**. Offsets **may differ** in other versions — when you change versions, re-run the selftests against your templates first.
- Everything here was independently reverse-engineered. Not affiliated with or endorsed by Apple. Logic, Logic Pro and all plug-in names are trademarks of their respective owners.
- Writing carries risk. **Always work on a copy, always keep the original**, and verify per `docs/05`.
- Deliberately out of scope: building projects from scratch, circumventing licensing, modifying third-party copyrighted material.

---

## License

- **Code** (`logicfmt/`, `tools/`): MIT — see `LICENSE-MIT`
- **Documentation** (`CORE.md`, `README*.md`, `docs/`): CC BY 4.0 — see `LICENSE-CC-BY-4.0`

---

## 中文版

See `README.md`.
