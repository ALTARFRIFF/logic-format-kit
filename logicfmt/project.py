"""logicfmt.project —— `.logicx` 的 ProjectData 读写

已实证（详见 `_templates/PROJECTDATA-FINDINGS.md`）：

* 文件 = 24 字节头 + 平铺的块流。每块 `tag(4)` + 24 字节头 + `u32 LE 长度 @+28`
  + 载荷，块总长 = 长度 + 36。三个样本文件都恰好走到末尾、零失配。
* 块内 `+8` 的 u32 是**对象 ID**，值 = `index << 18`，顺序递增。
  （不是随机 UUID —— 这是「从零建轨」可行的关键。）
* 音频轨的通道条三块 `OCuA(237)` / `UCuA(303)` / `UCuA(348)` 是**常量**，
  两次新增逐字节相同，可以直接复用。

这个模块只做「能安全读写」的那部分：遍历、重建、按 ID 寻址、分轨。
「新增一条音频轨」的完整合成还在做（需要 `gnoS` 的四张表），
所以这里不提供 `add_audio_track()` 以免给出一个半成品。

用法：
    import project
    ch = project.chunks(data)          # -> [Chunk]
    project.rebuild(ch)                # -> bytes（与 chunk 顺序的拼接等价）
    project.show(ch)                   # 打印块流
    project.find_tracks(ch)            # 粗分轨

命令行：
    python3 project.py roundtrip <ProjectData>     # 遍历→重建→逐字节比对
    python3 project.py show      <ProjectData>
    python3 project.py tracks    <ProjectData>
"""

from __future__ import annotations

import os
import re
import uuid
import struct
import sys

MAGIC = b"\x23\x47\xc0\xab"
HEADER = 24
HEADER_BYTES = b"\x23\x47\xc0\xab\xd1\x09\x03\x00\x04\x00\x00\x00\x01\x00\x08\x00\x25\xce\x2b\x00\x00\x00\x00\x00"
CHUNK_HEADER = 36
LEN_OFF = 28
ID_OFF = 8
ID_SHIFT = 18

TAGS = {
    "gnoS": "Song",
    "karT": "Trak",
    "qSvE": "EvSq",
    "qeSM": "MSeq",
    "ivnE": "EnvI",
    "nCuA": "AUCh",
    "OCuA": "AuCO",
    "UCuA": "AuCU",
    "tSnI": "InSt",
    "lytS": "Styl",
    "MneG": "GenM",
    "rpyH": "HypY",
    "OgnS": "Sngo",
    "MroC": "CorM",
    "ryaL": "LayR",
    "tScS": "ScSt",
    "ediV": "Vide",
    "tSxT": "TxSt",
}

# 音频轨通道条的常量块（逐字节验证过）
STRIP_CONST = (("OCuA", 237), ("UCuA", 303), ("UCuA", 348))


class Chunk:
    """一个块。`raw` 是含 36 字节头的完整字节。"""

    __slots__ = ("pos", "tag", "length", "raw")

    def __init__(self, pos: int, tag: str, length: int, raw: bytes):
        self.pos = pos
        self.tag = tag
        self.length = length
        self.raw = raw

    @property
    def payload(self) -> bytes:
        return self.raw[CHUNK_HEADER:]

    @property
    def end(self) -> int:
        return self.pos + CHUNK_HEADER + self.length

    @property
    def name(self) -> str:
        return TAGS.get(self.tag, self.tag)

    def id(self) -> int | None:
        """对象 ID = `+8` 的 u32；不是 `index<<18` 形状时返回 None。"""
        if self.length < 4:
            return None
        v = struct.unpack_from("<I", self.raw, ID_OFF)[0]
        return v if v % (1 << ID_SHIFT) == 0 else None

    def index(self) -> int | None:
        i = self.id()
        return None if i is None else i >> ID_SHIFT

    def with_id(self, index: int) -> bytes:
        """返回一份把 `+8` 改成 `index<<18` 的 raw（不改自身）。"""
        b = bytearray(self.raw)
        struct.pack_into("<I", b, ID_OFF, (index << ID_SHIFT) & 0xFFFFFFFF)
        return bytes(b)

    def __repr__(self) -> str:
        return f"<{self.tag}({self.name}) @{self.pos} len={self.length}>"


# --------------------------------------------------------------------------
# 遍历 / 重建
# --------------------------------------------------------------------------

def chunks(data: bytes, strict: bool = True) -> list[Chunk]:
    """从 24 字节头开始平铺遍历。`strict` 时走不到末尾就抛异常。"""
    if len(data) < HEADER + CHUNK_HEADER:
        raise ValueError("文件太短")
    if data[:4] != MAGIC:
        raise ValueError(f"不是 ProjectData：头是 {data[:4].hex()}")
    out: list[Chunk] = []
    p = HEADER
    while p + CHUNK_HEADER <= len(data):
        tag = data[p:p + 4]
        if not all(32 <= c < 127 for c in tag):
            break
        ln = struct.unpack_from("<I", data, p + LEN_OFF)[0]
        end = p + CHUNK_HEADER + ln
        if end > len(data):
            break
        out.append(Chunk(p, tag.decode("ascii"), ln, data[p:end]))
        p = end
    if strict and p != len(data):
        raise ValueError(f"块流没有走到末尾：停在 {p} / {len(data)}")
    return out


def rebuild(ch: list[Chunk], head: bytes = HEADER_BYTES) -> bytes:
    """把块流拼回字节。`head` 是文件开头的 24 字节（块流之前的那段）。"""
    return head + b"".join(c.raw for c in ch)


def header(data: bytes) -> bytes:
    return data[:HEADER]


def total_length_field(data: bytes) -> int:
    """头 `+16` 的 u32（含义未确认，仅供观察）。"""
    return struct.unpack_from("<I", data, 16)[0]


# --------------------------------------------------------------------------
# 结构查询
# --------------------------------------------------------------------------

def counts(ch: list[Chunk]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in ch:
        out[c.tag] = out.get(c.tag, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def find(ch: list[Chunk], tag: str, length: int | None = None) -> list[Chunk]:
    return [c for c in ch if c.tag == tag and (length is None or c.length == length)]


def find_one(ch: list[Chunk], tag: str, length: int | None = None) -> Chunk:
    got = find(ch, tag, length)
    if len(got) != 1:
        raise ValueError(f"{tag}({length}) 命中 {len(got)} 个，期望 1 个")
    return got[0]


def max_index(ch: list[Chunk]) -> int:
    """已用过的最大对象序号（用于分配新 ID）。"""
    best = -1
    for c in ch:
        i = c.index()
        if i is not None and i > best:
            best = i
    return best


def names_in(chunk: Chunk) -> list[str]:
    """粗提块里可读的 ASCII 字符串（≥3 字符）。"""
    out, cur = [], []
    for b in chunk.payload:
        if 32 <= b < 127:
            cur.append(chr(b))
        else:
            if len(cur) >= 3:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 3:
        out.append("".join(cur))
    return out


def find_tracks(ch: list[Chunk]) -> list[dict]:
    """把块流切成轨道段。

    `karT` 的 `index()`（`+8>>18`）为 0 的那种总长 36 的块是容器起点；
    容器后面紧跟它的子块，直到下一个容器。
    """
    starts = [i for i, c in enumerate(ch) if c.tag == "karT" and c.length == 0]
    tracks = []
    for n, i in enumerate(starts):
        j = starts[n + 1] if n + 1 < len(starts) else len(ch)
        kids = ch[i + 1:j]
        tracks.append({
            "chunk_index": i,
            "pos": ch[i].pos,
            "id": ch[i].id(),
            "index": ch[i].index(),
            "size": sum(CHUNK_HEADER + k.length for k in [ch[i]] + kids),
            "children": [(k.tag, k.length) for k in kids],
        })
    return tracks


def find_strips(ch: list[Chunk]) -> list[dict]:
    """音频轨的通道条 = 相邻的 `OCuA(237)` + `UCuA(303)` + `UCuA(348)` 三块。

    已验证（`_P1` 1 组 / `_TRK1` 2 组 / `_TRK2` 3 组），每新增一条音频轨就多一组。
    推子、pan、路由、Send 都应该在这三块里。
    """
    want = [("OCuA", 237), ("UCuA", 303), ("UCuA", 348)]
    out = []
    for i in range(len(ch) - 2):
        if all((ch[i + k].tag, ch[i + k].length) == want[k] for k in range(3)):
            out.append({"chunk_index": i, "pos": ch[i].pos,
                        "id": ch[i].id(), "index": ch[i].index(),
                        "size": sum(CHUNK_HEADER + ch[i + k].length for k in range(3)),
                        "names": [n for c in ch[i:i + 3] for n in names_in(c)][:4]})
    return out


def show(ch: list[Chunk], limit: int | None = None) -> None:
    print(f"{len(ch)} chunks")
    for c in (ch if limit is None else ch[:limit]):
        idx = c.index()
        extra = f" id={c.id()}" if c.id() is not None else ""
        nm = names_in(c)
        if nm:
            extra += "  " + " | ".join(nm[:2])
        print(f"  @{c.pos:7d} {c.tag}({c.name}) len={c.length:6d}{extra}")


# --------------------------------------------------------------------------
# 自检 / CLI
# --------------------------------------------------------------------------

def _selftest(paths: list[str] | None = None) -> int:
    import glob
    import os
    ok = bad = 0
    files = paths or sorted(glob.glob(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "logicx", "*.logicx", "Alternatives", "000", "ProjectData")))
    if not files:
        print("  (没有找到模板工程，跳过 roundtrip 自检)")
    for f in files:
        data = open(f, "rb").read()
        try:
            ch = chunks(data)
            back = rebuild(ch, data[:HEADER])
            same = back == data
            ok, bad = (ok + 1, bad) if same else (ok, bad + 1)
            print(f"  {'ok  ' if same else 'FAIL'} {os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(f))))}"
                  f"  {len(data)} B / {len(ch)} chunks")
            assert max_index(ch) >= 0
            for t in find_tracks(ch)[:1]:
                assert t["size"] > 0
        except Exception as e:
            bad += 1
            print(f"  FAIL {f}: {e}")
    print(f"\n{ok} passed, {bad} failed")
    cok, cbad = _selftest_channels()
    ok += cok
    bad += cbad
    sok, sbad = _selftest_stacks()
    ok += sok
    bad += sbad
    iok, ibad = _selftest_insert()
    ok += iok
    bad += ibad
    print(f"{ok} passed, {bad} failed")
    return 0 if bad == 0 else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv.pop(0)
    if cmd == "selftest":
        return _selftest(argv or None)
    if not argv:
        raise SystemExit(f"用法: project.py {cmd} <ProjectData>")
    path = argv[0]
    data = open(path, "rb").read()
    ch = chunks(data)

    if cmd == "roundtrip":
        back = rebuild(ch, data[:HEADER])
        print(f"{len(data)} B -> {len(back)} B, identical: {back == data}")
        print(f"chunks: {len(ch)}, max object index: {max_index(ch)}")
        print(f"header +16: {total_length_field(data)}  (file-24 = {len(data) - HEADER})")
        return 0 if back == data else 1

    if cmd == "show":
        show(ch, int(argv[1]) if len(argv) > 1 else None)
        return 0

    if cmd == "tracks":
        for t in find_tracks(ch):
            print(f"  karT @{t['pos']:7d} size={t['size']:7d}  {len(t['children'])} children")
        return 0

    if cmd == "strips":
        st = find_strips(ch)
        print(f"{len(st)} audio-track channel strips")
        for n, t in enumerate(st):
            print(f"  #{n} @{t['pos']:7d} size={t['size']}  {t['names']}")
        return 0

    if cmd == "channels":
        for c in channel_objects(data):
            print(f"  @{c['start']:7d} [{c['kind']:<8s}] {c['name'] or '?'}")
        return 0

    raise SystemExit(f"未知命令: {cmd}")


# ==========================================================================
# Aux / Summing Stack / Folder Stack 主轨 —— 都是 ivnE 对象
# ==========================================================================
CH_SIZES = (467,)          # 只有 467 是 Aux / Stack 主轨；471 是 Environment 通道对象
CH_NAME_OFF = {467: 160}
CH_KIND_BYTE = 155
CH_KINDS = {0x05: "aux", 0x10: "summing", 0x14: "folder"}
# kind -> (payload+155, payload+45, payload+86, payload+148)
CH_KIND_CODES = {"aux": (0x05, 0x01, 0x42, 0x24),
                 "summing": (0x10, 0x03, 0x40, 0x24),
                 "folder": (0x14, 0x01, 0x40, 0x23)}


def _ascii_run_at(p, off):
    if off >= len(p) or not (0x20 <= p[off] < 0x7F):
        return ""
    j = off
    while j < len(p) and 0x20 <= p[j] < 0x7F:
        j += 1
    return p[off:j].decode("ascii", "replace")


def channel_objects(data):
    """列出所有 Aux / Stack 主轨通道（来自 ivnE 467/471）。"""
    out = []
    for c in chunks(data):
        if c.tag != "ivnE" or c.length not in CH_SIZES:
            continue
        p = c.payload
        off = CH_NAME_OFF.get(c.length)
        out.append({"start": c.end - 36 - c.length,
                    "payload": c.end - c.length,
                    "length": c.length,
                    "name": _ascii_run_at(p, off) if off is not None else "",
                    "kind": CH_KINDS.get(p[CH_KIND_BYTE], "unknown"),
                    "kind_byte": p[CH_KIND_BYTE]})
    return out


def _channel_at(data, start):
    for c in chunks(data):
        if c.tag == "ivnE" and c.end - 36 - c.length == start:
            if c.length not in CH_SIZES:
                raise ValueError(f"ivnE at {start} has length {c.length}, expected {CH_SIZES}")
            return c
    raise KeyError(f"no ivnE chunk starting at {start}")


def set_channel_kind(data, start, kind):
    """把 Aux / Summing / Folder 三类互相转换（等长，原地改 4 个字节）。"""
    if kind not in CH_KIND_CODES:
        raise ValueError(f"kind must be one of {sorted(CH_KIND_CODES)}, got {kind!r}")
    c = _channel_at(data, start)
    b = bytearray(data)
    p = c.end - c.length
    b155, b45, b86, b148 = CH_KIND_CODES[kind]
    b[p + CH_KIND_BYTE] = b155
    b[p + 45] = b45
    b[p + 86] = b86
    b[p + 148] = b148
    return bytes(b)


def rename_channel(data, start, new_name):
    """改名。新名不能超过原名字段的字节数（不改变文件长度）。"""
    c = _channel_at(data, start)
    off = CH_NAME_OFF[c.length]
    p = c.end - c.length
    old = _ascii_run_at(c.payload, off)
    if not old:
        raise ValueError(f"ivnE at {start}: no name field at +{off}")
    if len(new_name) > len(old):
        raise ValueError(
            f"name too long: field holds {len(old)} bytes ({old!r}), "
            f"new name is {len(new_name)} bytes ({new_name!r}); "
            "lengthening the record is not supported (would shift the whole file)")
    b = bytearray(data)
    b[p + off: p + off + len(old)] = new_name.encode("ascii").ljust(len(old), b"\x00")
    return bytes(b)


def _selftest_channels():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [os.path.join(here, "templates", "logicx"),
             os.path.join(here, "..", "_templates")]
    root = next((c for c in cands
                 if os.path.exists(os.path.join(c, "_STACK.logicx"))), cands[-1])
    stack = os.path.join(root, "_STACK.logicx", "Alternatives/000/ProjectData")
    route = os.path.join(root, "_ROUTE.logicx", "Alternatives/000/ProjectData")
    if not os.path.exists(stack):
        print("channel selftest: templates not found, skipped")
        return 0, 0
    ok = bad = 0
    d = open(stack, "rb").read()
    chans = {c["name"]: c for c in channel_objects(d)}
    for name, want in (("SUM1", "summing"), ("FLD1", "folder")):
        got = chans.get(name, {}).get("kind")
        if got == want:
            ok += 1
        else:
            bad += 1
            print(f"FAIL {name}: kind {got!r} != {want!r}")
    # 三向转换往返
    base = chans["SUM1"]["start"]
    once = set_channel_kind(d, base, "folder")
    back = set_channel_kind(once, base, "summing")
    if back == d:
        ok += 1
    else:
        bad += 1
        print("FAIL kind round-trip")
    # 改名等长往返
    r = rename_channel(d, base, "ZZZZ")
    if channel_objects(r)[[c["start"] for c in channel_objects(r)].index(base)]["name"] == "ZZZZ":
        ok += 1
    else:
        bad += 1
        print("FAIL rename")
    if rename_channel(r, base, "SUM1") == d:
        ok += 1
    else:
        bad += 1
        print("FAIL rename round-trip")
    # 超长名必须报错而不是静默截断
    try:
        rename_channel(d, base, "TOOLONGNAME")
        bad += 1
        print("FAIL long rename should have raised")
    except ValueError:
        ok += 1
    # _ROUTE 的 4 个 Aux
    if os.path.exists(route):
        rd = open(route, "rb").read()
        kinds = sorted(c["kind"] for c in channel_objects(rd))
        if kinds == ["aux"] * 4:
            ok += 1
        else:
            bad += 1
            print(f"FAIL _ROUTE aux kinds: {kinds}")
    free = []
    if not os.path.exists(os.path.join(root, "_ROUTE.logicx")):
        free.append("_ROUTE")
    print(f"channel selftest: {ok} passed, {bad} failed"
          + (f" (skipped: {', '.join(free)})" if free else ""))
    return ok, bad


# ==========================================================================
# 音频轨通道条：输出路由 + Folder Stack 成员标志
# ==========================================================================
OUT_OFF = 92          # 0 = Stereo Out, 0xFF = No Output, 否则 bus + 1
FOLDER_FLAG_OFF = 110  # 0x01 = 该轨是某个 Folder Stack 的子轨
STRIP_OUT = (201, 237, 241, 249)


def audio_tracks(data):
    """列出所有音频轨通道条及其输出路由 / Folder 成员标志。"""
    import re
    out = []
    for c in chunks(data):
        if c.tag != "OCuA" or c.length not in STRIP_OUT:
            continue
        nm = ""
        for n in names_in(c):
            n = n.strip()
            if re.fullmatch(r"Audio \d+", n):
                nm = n
                break
        if not nm or len(c.payload) <= FOLDER_FLAG_OFF:
            continue
        p = c.payload
        raw = p[OUT_OFF]
        bus = None if raw in (0x00, 0xFF) else raw - 1
        out.append({"name": nm,
                    "start": c.end - 36 - c.length,
                    "payload": c.end - c.length,
                    "length": c.length,
                    "out_raw": raw,
                    "out": "stereo" if raw == 0x00 else ("none" if raw == 0xFF else f"bus{bus}"),
                    "bus": bus,
                    "in_folder": p[FOLDER_FLAG_OFF] == 0x01})
    return out


def set_folder_member(data, start, flag):
    """把一条音频轨标记/取消标记为 Folder Stack 子轨（等长，改 1 字节）。"""
    for t in audio_tracks(data):
        if t["start"] == start:
            b = bytearray(data)
            b[t["payload"] + FOLDER_FLAG_OFF] = 0x01 if flag else 0x00
            return bytes(b)
    raise KeyError(f"no audio-track strip starting at {start}")


def _selftest_stacks():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [os.path.join(here, "templates", "logicx"), os.path.join(here, "..", "_templates")]
    root = next((c for c in cands if os.path.exists(os.path.join(c, "_SKELETON.logicx"))), cands[-1])
    path = os.path.join(root, "_SKELETON.logicx", "Alternatives/000/ProjectData")
    if not os.path.exists(path):
        print("stack selftest: _SKELETON not found, skipped")
        return 0, 0
    ok = bad = 0
    d = open(path, "rb").read()
    tracks = {t["name"]: t for t in audio_tracks(d)}
    want = {"Audio 3", "Audio 4"}
    got = {n for n, t in tracks.items() if t["in_folder"]}
    if got == want:
        ok += 1
    else:
        bad += 1
        print(f"FAIL folder members: {sorted(got)} != {sorted(want)}")
    # 求和 Stack 的成员只体现在输出路由上
    if tracks["Audio 1"]["out"] == "bus1" and tracks["Audio 2"]["out"] == "bus1":
        ok += 1
    else:
        bad += 1
        print(f"FAIL summing routing: {tracks['Audio 1']['out']}, {tracks['Audio 2']['out']}")
    # 开关往返
    s = tracks["Audio 3"]["start"]
    if not set_folder_member(d, s, False) == d and set_folder_member(set_folder_member(d, s, False), s, True) == d:
        ok += 1
    else:
        bad += 1
        print("FAIL folder flag round-trip")
    if audio_tracks(set_folder_member(d, s, False))[[t["start"] for t in audio_tracks(d)].index(s)]["in_folder"] is False:
        ok += 1
    else:
        bad += 1
        print("FAIL folder flag readback")
    print(f"stack selftest: {ok} passed, {bad} failed")
    return ok, bad


# ==========================================================================
# 真插入：改块载荷 -> 重算块长 -> 重算文件总长
# ==========================================================================
def with_payload(chunk, payload: bytes):
    """返回一个载荷被替换、长度字段已重算的块。"""
    raw = bytearray(chunk.raw[:CHUNK_HEADER]) + payload
    struct.pack_into("<I", raw, LEN_OFF, len(payload) & 0xFFFFFFFF)
    return Chunk(chunk.pos, chunk.tag, len(payload), bytes(raw))


def seal(ch, head: bytes) -> bytes:
    """把块流拼回字节，并重算文件头 `+16` 的总长度。"""
    body = b"".join(c.raw for c in ch)
    h = bytearray(head)
    struct.pack_into("<I", h, 16, len(body) & 0xFFFFFFFF)
    return bytes(h) + body


def insert_in_payload(chunk, off: int, blob: bytes):
    """在块载荷的第 `off` 字节处插入 `blob`（真正变长）。"""
    p = chunk.payload
    if not (0 <= off <= len(p)):
        raise ValueError(f"insert offset {off} outside payload (0..{len(p)})")
    return with_payload(chunk, p[:off] + blob + p[off:])


def _selftest_insert():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [os.path.join(here, "templates", "logicx"), os.path.join(here, "..", "_templates")]
    root = next((c for c in cands if os.path.exists(os.path.join(c, "_SIG-C.logicx"))), cands[-1])
    path = os.path.join(root, "_SIG-C.logicx", "Alternatives/000/ProjectData")
    if not os.path.exists(path):
        print("insert selftest: _SIG-C not found, skipped")
        return 0, 0
    try:
        from . import meterkey as MK
    except ImportError:
        import meterkey as MK
    ok = bad = 0
    d = open(path, "rb").read()
    ch = chunks(d)
    head = d[:HEADER]
    qi = next(i for i, c in enumerate(ch)
              if c.tag == "qSvE" and len(MK.meter_blocks(c.payload)) > 1)
    pay = ch[qi].payload
    blocks = MK.meter_blocks(pay)
    n_before = len(blocks)
    last = blocks[-1]["B"]
    new = bytearray(pay[last:last + 48])
    new[11], new[12], new[24] = 0x03, 0x06, 19          # 6/8 @ 第 20 小节
    ch2 = list(ch)
    ch2[qi] = insert_in_payload(ch[qi], last + 48, bytes(new))
    out = seal(ch2, head)
    if len(out) == len(d) + 48:
        ok += 1
    else:
        bad += 1
        print(f"FAIL size {len(d)} -> {len(out)} (want +48)")
    if struct.unpack_from("<I", out, 16)[0] == len(out) - HEADER:
        ok += 1
    else:
        bad += 1
        print("FAIL header +16 not updated")
    ch3 = chunks(out)
    if len(ch3) == len(ch) and ch3[qi].length == ch[qi].length + 48:
        ok += 1
    else:
        bad += 1
        print(f"FAIL reparse: chunks {len(ch3)} vs {len(ch)}, qSvE {ch3[qi].length} vs {ch[qi].length}")
    got = MK.meter_blocks(ch3[qi].payload)
    if len(got) == n_before + 1 and got[-1]["num"] == 6 and got[-1]["den_exp"] == 3:
        ok += 1
    else:
        bad += 1
        print(f"FAIL meter after insert: {len(got)} blocks, last={got[-1] if got else None}")
    if all(b["bar"] == b0["bar"] for b, b0 in zip(got[:n_before], blocks)):
        ok += 1
    else:
        bad += 1
        print("FAIL existing meter records were disturbed")
    kb, ka = MK.key_records(pay), MK.key_records(ch3[qi].payload)
    ins = last + 48
    if len(ka) == len(kb) and all(
            a["off"] == (b["off"] + 48 if b["off"] >= ins else b["off"]) for a, b in zip(ka, kb)) \
            and all(a["code"] == b["code"] for a, b in zip(ka, kb)):
        ok += 1
    else:
        bad += 1
        print(f"FAIL key records: {[r['off'] for r in kb]} -> {[r['off'] for r in ka]}")
    print(f"insert selftest: {ok} passed, {bad} failed")
    return ok, bad


if __name__ == "__main__":
    raise SystemExit(main())


# ===== 新增一条音频轨 ======================================================
# 全部配方来自 _P1(1轨) / _TRK1(2轨) / _TRK2(3轨) 三次实测。

def _u32(p, o):
    return struct.unpack_from("<I", p, o)[0]


def _find_run(p, code, rec):
    """载荷里 `code` 开头的连续等长记录串 (起点, 格数)，取最长的一段。"""
    best, i = (0, 0), 0
    while i + rec <= len(p):
        if _u32(p, i) == code:
            j, n = i, 0
            while j + rec <= len(p) and _u32(p, j) == code:
                j += rec
                n += 1
            if n > best[1]:
                best = (i, n)
            i = j
        else:
            i += 1
    return best


def audio_strip_runs(ch):
    """音频轨通道条三元组的起始下标（`payload[4]==0x40` 排除 Inst）。"""
    out = []
    for i in range(len(ch) - 2):
        c = ch[i]
        if (c.tag == "OCuA" and c.length in (237, 241, 249)
                and c.payload[4] == 0x40
                and ch[i + 1].tag == "UCuA" and ch[i + 2].tag == "UCuA"):
            out.append(i)
    return out


def audio_envs(ch):
    """音频轨的 `ivnE:471`（Environment 通道条）下标。

    判据不能要求名字以 `Audio ` 开头 —— 用户自定义轨名（`A1`、`Drums` …）时
    会一个都匹配不上。`ivnE:471` 里唯一的非音频对象是 `Preview`，排除它即可。
    """
    out = []
    for i, c in enumerate(ch):
        if c.tag != "ivnE" or c.length != 471:
            continue
        nm = c.payload[160:176].split(b"\0")[0]
        if nm and nm != b"Preview":
            out.append(i)
    return out


def kart_runs(ch):
    """`karT:58` 连续串的 (首, 末)，且其后紧跟 `karT:0`。"""
    out, i, n = [], 0, len(ch)
    while i < n:
        if ch[i].tag == "karT" and ch[i].length == 58:
            j = i
            while j + 1 < n and ch[j + 1].tag == "karT" and ch[j + 1].length == 58:
                j += 1
            if j + 1 < n and ch[j + 1].tag == "karT" and ch[j + 1].length == 0:
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _mk(tag, payload, idx=None):
    raw = bytearray(CHUNK_HEADER + len(payload))
    raw[0:4] = tag.encode()
    if idx is not None:
        struct.pack_into("<I", raw, ID_OFF, (idx << ID_SHIFT) & 0xFFFFFFFF)
    struct.pack_into("<I", raw, LEN_OFF, len(payload))
    raw[CHUNK_HEADER:] = payload
    return Chunk(0, tag, len(payload), bytes(raw))


def _clone(src, payload: bytes, idx=None):
    """克隆一个块：**保留原头部字节**，只换载荷与（可选）对象 ID。

    不能改用 `_mk()` —— 它会把 `+4..+22` 的版本/类型/哨兵字节清零。
    `gnoS`（Song）的头部被清零后，Logic 直接报
    `Read Error! This is not a Logic Pro project!`，而块长与载荷全部正确，
    逐块比对也看不出来（那 18 个字节不在任何载荷里）。
    """
    raw = bytearray(src.raw[:CHUNK_HEADER]) + payload
    struct.pack_into("<I", raw, LEN_OFF, len(payload) & 0xFFFFFFFF)
    if idx is not None:
        struct.pack_into("<I", raw, ID_OFF, (idx << ID_SHIFT) & 0xFFFFFFFF)
    return Chunk(0, src.tag, len(payload), bytes(raw))

def add_audio_track(data, name=None, rnd=None, _shift_env_ids=True):
    """在工程末尾**新建**一条音频轨。

    验收：对 `_TRK1`（2 轨）执行后，与真实 `_TRK2`（3 轨）对比 ——
    **文件大小（135,225）、462 个块的签名序列、逐块载荷全部对上**。
    残留 323 字节，全部属于「每次保存都会重生成的随机字节」：

    * `gnoS` 196、`karT` 32、`OCuA:237` 16、`UCuA` ×2 各 16、`ivnE` 16 = 292
      —— 16 字节的 v1 UUID（时间型，前缀字节天然共享，所以每个 UUID 只差 4~5 字节）
    * `qeSM` 31 —— `+8/+9` 的 u16 随机分配 ID（16 个 `qeSM` 全部不同，
      `+4` 恒 0，六种哈希候选无一命中，且出现过 16 位碰撞）

    **本函数写入的计数器一览**（全部实测对上）：

    | 块 | 字段 | 规则 |
    |---|---|---|
    | `gnoS` | `u32 @94` | +4（全局对象序号）|
    | `gnoS` | `u32 @137` | +8 |
    | `gnoS` | 两条「轨数」记录 | u8+u32 双写，A = n+1、B = n，尾部常量 6 |
    | `gnoS` | 4 段登记 | `0x14`/`0x17` 两张表各 +1 格（24B）和 1 条镜像（16B），序号 +4 |
    | `ivnE` | `u16 @82` | 非音频 +66；已有音频不动；克隆条 = 上一条 +64 |
    | `ivnE` | `len-303` | 非音频 +1（名字变长，只能从末尾数）|
    | `ivnE` | `+173` / `+45` / `+80` | 克隆条 173=1、45=0、80=1；原末条 45=2、80=0 |
    | `qSvE` | 第 0 块 `+20` | +1 |
    | `qSvE` | 新增 80B 块 | `+16` +4、`+20` +1、`+32` +4 |
    | `nCuA` | 尾部 + `+26`/`+28` | 追加 `01 00 00 00`，两个计数器各 +1 |
    | `OCuA:201` | `+6` / `+67` | 混音器条区（`+4==0x40` 且 `+67` ∈ 48..57）里 `+6 >= n_audio` 的那几条各 +1 |
    | `karT:58` | 族成员 | 每个 ≥2 成员的族追加 1 个成员：`+8` = 族内最大 +4，`(32,64)` 末位标从模板移到新成员 |
    | 全部 | 对象 ID | `序号 << 18`（顺序分配）|

    `OCuA` 的名字只占 `61..75`（**15 字节**），`76` 是通道类型常量（音频 `0xAB`）。
    用 16 字节宽去写名字会把 `0xAB` 抹成 0 —— 这是最后修掉的一个字节。

    **已在 Logic Pro 12.4 实测通过**（2026-10-19）：
    对 `_TRK1`（2 轨）执行 `add_audio_track("Audio 3")` → 打开 `_Y5`，
    三个独立见证同时成立：

    * `logic_list_tracks` → 3 行，`Audio 1 / Audio 2 / Audio 3`
    * `logic_list_strips` → 5 条（`Audio1 Audio2 Audio3 St Out Master`），与真
      `_TRK2` 完全一致
    * `logic_track_info("Audio 3")` → `kind: "audio"`、`Input 1`、`Stereo Output`、
      `channel_mode: Mono`、`volume_db: 0`，与真 `_TRK2` 的 Audio 3 逐字段相同

    再对 `_TRK2`（3 轨）执行 `add_audio_track("Audio 4")` → 4 轨（`_Y6`），
    说明不是只对某一个谱系成立。

    **最后一个缺口是通道条的身份要写在两处**：通道条区块载荷 `+189` 的 16 字节
    UUID，以及 Environment 区对应 `ivnE:471` 载荷 `+455` 的同一 16 字节。只写
    一处时轨道行会正常出现、混音器却没有那条通道条（`kind: "reduced"`）——
    由 `link_strip_envs()` 统一处理。
    """
    # UUID 必须是**合法的时间型 UUID**（version=1、variant=0b10）。之前用
    # os.urandom(16) 生成的是非法 UUID —— Logic 解析 Song 时会在那里失败，
    # 报「This is not a Logic Pro project!」，而逐字节对齐看不出任何问题。
    def _rnd(n):
        if n == 16:
            return int(uuid.uuid1().int).to_bytes(16, "big")
        return os.urandom(n)
    rnd = rnd or _rnd
    ch = list(chunks(data))
    head = header(data)
    new_idx = (max_index(ch) or 0) + 1

    strips = audio_strip_runs(ch)
    envs = audio_envs(ch)
    if not strips:
        raise ValueError("工程里没有可参照的音频轨")
    n_audio = len(strips)
    title = name or (u"Audio %d" % (n_audio + 1))

    # 0. 既有对象让位：新轨插在通道序列的第 n_audio 位，其后的通道序号 +1
    # 注：`OCuA` 的 +6/+67 只在紧邻新轨的少数通道上变，不能全局 +1
    #     （全局改会让 258 个编曲区行头一起变，差异从 429 涨到 935）。
    # `ivnE` 的 u16 @+82 每条新轨统一 +66
    for i, c in enumerate(ch):
        if (_shift_env_ids and c.tag == "ivnE" and c.length in (469, 471, 473)
                and c.payload[160:166] != b"Audio "):
            p = bytearray(c.payload)
            v = (p[82] | (p[83] << 8)) + 66
            p[82] = v & 0xFF
            p[83] = (v >> 8) & 0xFF
            # 名字后面那个序号字段：位置 = 载荷长度 - 303（名字定长 16 字节、
            # 但载荷长度随名字长度变化，所以只有一个「从末尾数」的偏移是稳定的）
            q = len(p) - 303
            if 0 <= q < len(p):
                p[q] = (p[q] + 1) & 0xFF
            ch[i] = with_payload(c, bytes(p))
    # 0b. 混音器通道条的局部让位：+4==0x40 且 +67 落在 48..57（混音器条区）且
    #     +6 >= n_audio 的那几条，+6 与 +67 各 +1。判据来自 _TRK1 的 i=84/85/86
    #     （+6 = 2/3/4、+67 = 51/52/53），_TRK2 里只有这三条变。
    for i, c in enumerate(ch):
        if c.tag != "OCuA" or c.length != 201:
            continue
        p = bytearray(c.payload)
        if len(p) < 68 or p[4] != 0x40:
            continue
        if 48 <= p[67] <= 57 and p[6] >= n_audio:
            p[6] = (p[6] + 1) & 0xFF
            p[67] = (p[67] + 1) & 0xFF
            ch[i] = with_payload(c, bytes(p))

    # 1. 通道条三元组
    si = strips[-1]
    def _name(payload, off, nm, width=16):
        b = bytearray(payload)
        b[off:off + width] = nm.encode()[:width].ljust(width, b"\0")
        return b
    # `OCuA` 的名字只占 61..75（15 字节）；76 是通道类型常量（音频 0xAB / 乐器 0x29），
    # 最早用 16 字节宽去写，把 0xAB 抹成了 0 —— 这就是最后那 1 字节差异。
    oc_p = _name(ch[si].payload, 61, title, width=15)
    oc_p[6] = n_audio
    oc_p[189:205] = rnd(16)          # UUID 从 189 开始（193 是 `cb ?? 11 f1` 标记）
    u1_p = bytearray(ch[si + 1].payload); u1_p[-16:] = rnd(16)
    u2_p = bytearray(ch[si + 2].payload); u2_p[-16:] = rnd(16)

    # 2. ivnE:471（Environment 通道对象）。
    #    注意：有些工程（如 `_SKELETON`，5 条音频轨全用自定义名）**根本没有**音频
    #    的 `ivnE:471`，此时要跳过这一段，而不是报错。
    ev = None
    if envs:
        ei = envs[-1]
        ev = _name(ch[ei].payload, 160, title)
        struct.pack_into("<I", ev, 16, _u32(ev, 16) + 4)
        ev[168] = (n_audio + 1) & 0xFF
        ev[171] = n_audio & 0xFF
        ev[173] = 1      # 克隆出的新条：0 -> 1
        ev[45] = 0
        ev[80] = 1
        # 原来的「最后一条音频条」要让位：+45 = 2、+80 = 0
        _src = bytearray(ch[ei].payload)
        _src[45] = 2
        _src[80] = 0
        ch[ei] = with_payload(ch[ei], bytes(_src))
        _v82 = (ev[82] | (ev[83] << 8)) + 64
        ev[82] = _v82 & 0xFF
        ev[83] = (_v82 >> 8) & 0xFF
        ev[455:471] = rnd(16)

    # 3. karT:58：给每个「多成员族」各追加 1 个成员
    #    实测规律（_TRK1 -> _TRK2）：新轨**不新建族**，而是在每个「≥2 成员」的族里，
    #    挑 `payload+8` 最大的那个成员当模板，紧跟其后插一条 `+8` 最大+4 的克隆；
    #    若该模板带着 `+40/+43 = (32,64)` 这对「末位标记」，标记要移到新成员上，
    #    模板自己清零（这正是之前 71 字节差异的来源）。
    fams = {}
    for i, c in enumerate(ch):
        if c.tag == "karT" and c.length == 58:
            fams.setdefault(int.from_bytes(c.raw[8:12], "little"), []).append(i)
    kt = []
    for guard, idxs in fams.items():
        if len(idxs) < 2:
            continue
        t = max(idxs, key=lambda i: _u32(ch[i].payload, 8))
        tp = bytearray(ch[t].payload)
        newp = bytearray(tp)
        struct.pack_into("<I", newp, 8, _u32(tp, 8) + 4)
        raw = bytearray(ch[t].with_id(guard >> ID_SHIFT))
        raw[CHUNK_HEADER:CHUNK_HEADER + ch[t].length] = bytes(newp)
        raw[CHUNK_HEADER + 24:CHUNK_HEADER + 40] = rnd(16)
        tp[40] = 0
        tp[43] = 0
        ch[t] = with_payload(ch[t], bytes(tp))
        kt.append((t, Chunk(0, ch[t].tag, ch[t].length, bytes(raw))))

    # 4. 末尾三元组：克隆最后一个 qeSM:309 之后的一组
    trio_i = None
    for i in range(len(ch) - 2, 1, -1):
        if (ch[i].tag == "qeSM" and ch[i].length == 309
                and ch[i - 1].tag == "qSvE" and ch[i - 1].length == 16
                and ch[i - 2].tag == "karT" and ch[i - 2].length == 0):
            trio_i = i - 2
            break
    if trio_i is None:
        raise ValueError("找不到末尾三元组")
    trio = [Chunk(0, c.tag, c.length, c.with_id(new_idx))
            for c in ch[trio_i:trio_i + 3]]
    # 三元组里那个 `qeSM:309` 的 `payload+8` 是**全工程唯一的 u16 ID**。
    # 直接克隆会撞号（实测 `_TRK2` 16 个 ID 全不同，克隆后出现重复 115），
    # 而 Logic 打开时会因重复 ID 报「This is not a Logic Pro project!」。
    _used = {int.from_bytes(c.payload[8:10], "little")
             for c in ch if c.tag == "qeSM" and len(c.payload) >= 10}
    for _ in range(512):
        cand = 1 + int.from_bytes(rnd(2), "little") % 0xFFFE
        if cand not in _used:
            break
    _tp = bytearray(trio[2].payload)
    _tp[8:10] = cand.to_bytes(2, "little")
    trio[2] = with_payload(trio[2], bytes(_tp))

    # 5. qSvE 增长
    qi = max((i for i, c in enumerate(ch) if c.tag == "qSvE"),
             key=lambda i: ch[i].length)
    qp = bytearray(ch[qi].payload)
    # 只有第 0 个 80 字节块的 +20 会 +1（它是「块数」计数器）；其余块不动。
    qp[20] = (qp[20] + 1) & 0xFF
    blk = bytearray(qp[-96:-16])
    struct.pack_into("<I", blk, 16, _u32(blk, 16) + 4)   # +16 每块 +4
    blk[20] = (blk[20] + 1) & 0xFF                       # 新块 +20 也 +1
    struct.pack_into("<I", blk, 32, _u32(blk, 32) + 4)   # 新块 +32 +4
    qp[-16:-16] = bytes(blk)

    # 6. nCuA +4
    ni = max((i for i, c in enumerate(ch) if c.tag == "nCuA"),
             key=lambda i: ch[i].length)
    _np = bytearray(ch[ni].payload + b"\x01\x00\x00\x00")
    _np[26] = (_np[26] + 1) & 0xFF
    _np[28] = (_np[28] + 1) & 0xFF
    npay = bytes(_np)

    # 7. gnoS 四段登记
    gp = bytearray(ch[0].payload)
    for code in (0x14, 0x17):
        for rec in (24, 16):
            start, cnt = _find_run(gp, code, rec)
            if not cnt:
                continue
            seq = _u32(gp, start + (cnt - 1) * rec + 4) + 4
            # 新记录 = **上一条记录整体克隆**，只把 `+4` 的序号 +4。
            # 不能随机填后面的字节：那些字节里混着递增计数器
            # （实测形如 `9c e2 b4 24 / b0 e2 b4 24 / c4 e2 b4 24`，步长 0x14），
            # 随机填充会把 Song 写坏，Logic 报
            # `Read Error! This is not a Logic Pro project!`。
            at = start + cnt * rec
            ins = bytearray(gp[start + (cnt - 1) * rec:at])
            struct.pack_into("<I", ins, 4, seq)
            gp[at:at] = bytes(ins)

    # 7b. gnoS 的计数器（用结构特征串定位，不写死绝对偏移）
    struct.pack_into("<I", gp, 94, _u32(gp, 94) + 4)      # 全局对象序号
    # g137：实测 _TRK1(2轨)=0、_TRK2(3轨)=8、_TRK3(4轨)=8 —— 并非每轨递增，
    # 加轨时保持原值不动（原先无条件 +8 会把 4 轨写成本该没有的 16）。
    # 记录形如： [u8 N][00 01 00][u32 N][90 02 00 00][06 00 10 08 03]
    _hits = [m for m in range(len(gp) - 11)
             if gp[m:m + 3] == b"\x00\x01\x00"
             and gp[m + 7:m + 11] == b"\x90\x02\x00\x00"]
    for k, m in enumerate(_hits[:2]):
        val = (n_audio + 1) if k == 0 else n_audio
        gp[m - 1] = val & 0xFF                             # u8 副本
        struct.pack_into("<I", gp, m + 3, val)             # u32 副本
        gp[m + 11] = 6                                     # 固定值 6

    ch[0] = _clone(ch[0], bytes(gp))
    ch[ni] = with_payload(ch[ni], npay)
    ch[qi] = with_payload(ch[qi], bytes(qp))

    after = {}
    after.setdefault(si + 2, []).extend([_clone(ch[si], bytes(oc_p), new_idx),
                                        _clone(ch[si + 1], bytes(u1_p)),
                                        _clone(ch[si + 2], bytes(u2_p))])
    if ev is not None:
        after.setdefault(ei, []).append(_clone(ch[ei], bytes(ev), new_idx))
    for t, k in kt:
        after.setdefault(t, []).append(k)
    after.setdefault(trio_i + 2, []).extend(trio)

    out = []
    for i, c in enumerate(ch):
        out.append(c)
        out.extend(after.get(i, []))
    return link_strip_envs(normalize_counters(normalize_counters(seal(out, head))))


def link_strip_envs(data):
    """把每条音频通道条的 UUID 抄进它对应的 `ivnE:471` 的载荷 `+455`。

    Logic 里**一个通道条的身份由两处共同声明**：通道条自己载荷 `+189` 起的
    16 字节 UUID，以及 Environment 区里对应 `ivnE:471` 载荷 `+455` 起的同一
    16 字节。只写一处，通道条不会被登记进混音器 —— 实测表现为：轨道行能出现
    （`logic_list_tracks` 有 3 条），但 `logic_list_strips` 只有 2 条音频条，
    `logic_track_info` 把该轨读成 `kind: "reduced"`。

    配对按名字（通道条 `+61` 的 NUL 结尾串 == `ivnE` 载荷里第一段可打印串）。
    """
    buf = bytearray(data)
    ch = chunks(buf)
    by_name = {}
    for c in ch:
        p = c.payload
        if (c.tag == "OCuA" and c.length in STRIP_OUT and len(p) > 205
                and p[4] == 0x40):
            nm = bytes(p[61:76]).split(b"\x00")[0].decode("utf-8", "replace").strip()
            if nm:
                by_name[nm] = c.pos + 36 + 189
    for c in ch:
        if c.tag != "ivnE" or c.length != 471 or len(c.payload) < 471:
            continue
        m = re.search(rb"[ -~]{3,24}", bytes(c.payload))
        if not m:
            continue
        nm = m.group().decode("utf-8", "replace").strip()
        src = by_name.get(nm)
        if src is None:
            continue
        dst = c.pos + 36 + 455
        buf[dst:dst + 16] = buf[src:src + 16]
    return bytes(buf)


def normalize_counters(data):
    """重写「块头序号」类字段。**这是让新建轨道真正被 Logic 认出的最后一步。**

    `_TRK1 → _TRK2`、`_TRK2 → _H2`（Logic 亲手新建）都靠这四条规则闭合：

    1. `OCuA` 块头 `+12`（u32）= **序号 << 16**：按文件顺序，`length == 14` 的写 0，
       其余写 0,1,2,…。`_H2` 里 290 个 `OCuA` 有 289 个吻合。
    2. `OCuA` 块头 `+8`（u32）= **`0x240000`**（9 << 18）—— **通道条类型码**，
       不是对象序号；写成 `new_idx << 18` 时 Logic 静默丢弃整条通道条。
    3. `UCuA`（303/348）块头 `+14` = **它所属通道条的序号**（与 `+12` 高 16 位一致）。
    4. `ivnE:471` 块头 `+10`（u16）= **`index << 2`**，`index = (+8) >> 18`。

    参数 `data` 是完整的 ProjectData 字节；返回同样完整、已重写的字节。
    """
    d = bytearray(data)
    ch = chunks(bytes(d), strict=False)
    ordinal = 0
    strip_open = False
    for c in ch:
        if c.tag == "OCuA":
            cur = struct.unpack_from("<I", d, c.pos + 8)[0]
            if (cur >> ID_SHIFT) >= 250:      # 我自己写进去的 new_idx<<18，改成类型码
                struct.pack_into("<I", d, c.pos + 8, 9 << ID_SHIFT)
            v = 0 if c.length == 14 else ((ordinal << 16) & 0xFFFFFFFF)
            if c.length != 14:
                ordinal += 1
            if struct.unpack_from("<I", d, c.pos + 12)[0] != v:
                struct.pack_into("<I", d, c.pos + 12, v)
            strip_open = c.length != 14
        elif c.tag == "UCuA" and strip_open and c.length in (303, 348):
            v = (ordinal - 1) & 0xFF
            if d[c.pos + 14] != v:
                d[c.pos + 14] = v
        elif c.tag == "ivnE" and c.length == 471:
            idx = struct.unpack_from("<I", d, c.pos + 8)[0] >> ID_SHIFT
            if idx >= 250:                      # 新克隆的，取该家族现有最大 + 1
                idx = _max_env_index(d, ch) + 1
            v = (idx * 4) & 0xFFFF
            if struct.unpack_from("<H", d, c.pos + 10)[0] != v:
                struct.pack_into("<H", d, c.pos + 10, v)

    ch2 = chunks(bytes(d), strict=False)
    # `karT:58` 的 +18 = 同一 `+8` 组内的下标（从 0 起）
    seen = {}
    for c in ch2:
        if c.tag == "karT" and c.length == 58:
            key = struct.unpack_from("<I", d, c.pos + 8)[0]
            d[c.pos + 18] = seen.get(key, 0) & 0xFF
            seen[key] = seen.get(key, 0) + 1
    # 「总数」块：第 2 个 length==14 的 OCuA 的 +14 u16 = `OCuA` 总数，其余为 0
    ocs = [c for c in ch2 if c.tag == "OCuA"]
    short = [c for c in ocs if c.length == 14]
    for c in short:
        struct.pack_into("<H", d, c.pos + 14, 0)
    if len(short) >= 2:
        struct.pack_into("<H", d, short[1].pos + 14, (len(ocs) - len(short)) & 0xFFFF)
    # 通道条之外的 UCuA 组（256/1903/831）：+14 = 音频轨数 + 7
    n_audio = sum(1 for c in ocs
                  if c.length in (237, 241, 249) and c.payload[4] == 0x40)
    for c in ch2:
        if c.tag == "UCuA" and c.length in (256, 831, 1903):
            d[c.pos + 14] = (n_audio + 7) & 0xFF
    # `karT` 的 +8 u32 = 序号 << 16，+10 u16 = 同一个序号；我给新块写的 new_idx<<18 要改回来
    ks = [c for c in ch2 if c.tag == "karT"]
    top = 0
    for c in ks:
        v = struct.unpack_from("<I", d, c.pos + 8)[0]
        idx = v >> 16
        if idx < 200 and idx % 4 == 0:
            top = max(top, idx)
    # 顺序修正：把「同属一批新增」的 karT（序号落在 top 之后、且非 1024）按文件顺序重排
    batch = [c for c in ks
             if 20 <= ((struct.unpack_from("<I", d, c.pos + 8)[0] >> 16) & 0xFFFF) < 200]
    base = 20
    for n, c in enumerate(batch):
        idx = base + 4 * n
        struct.pack_into("<I", d, c.pos + 8, (idx << 16) & 0xFFFFFFFF)
        struct.pack_into("<H", d, c.pos + 10, idx & 0xFFFF)

    last = ks[-1] if ks else None
    for c in ks:
        v = struct.unpack_from("<I", d, c.pos + 8)[0]
        if v >= (250 << 16):
            if last is not None and c.pos == last.pos:
                idx = 1024                     # 末尾三元组里的 karT 固定 1024
            else:
                top += 4
                idx = top
            struct.pack_into("<I", d, c.pos + 8, (idx << 16) & 0xFFFFFFFF)
            struct.pack_into("<H", d, c.pos + 10, idx & 0xFFFF)
        else:
            idx = v >> 16
            if struct.unpack_from("<H", d, c.pos + 10)[0] != (idx & 0xFFFF):
                struct.pack_into("<H", d, c.pos + 10, idx & 0xFFFF)
    return bytes(d)


def _max_env_index(d, ch):
    """现有 ivnE:471 里最大的合法序号（`< 250`）。"""
    best = -1
    for c in ch:
        if c.tag == "ivnE" and c.length == 471:
            idx = struct.unpack_from("<I", d, c.pos + 8)[0] >> ID_SHIFT
            if idx < 250 and idx > best:
                best = idx
    return best


def add_stack(data, kind="summing", name=None, rnd=None):
    """在工程末尾**新建**一条 Stack（Summing / Folder）主行。

    做法是「先建一条音频轨、再把它改造成 Stack」——因为两者共用同一条 9 插入点
    配方（见 `PROJECTDATA-FINDINGS.md` §36），差别只有三处：

    | 项 | 音频轨 | Stack |
    |---|---|---|
    | 通道对象 | `ivnE:471` | **`ivnE:469`**（Aux / Stack 主通道）|
    | 行头池 `OCuA:201` | 不动 | 消耗掉 1 个空行头（`−1`）|
    | 通道条覆盖块 | `UCuA:303` | Summing 同；**Folder 用 `UCuA:350`** |

    `kind` 取 `"summing"` 或 `"folder"`。`name` 省略时用 `Sum N` / `Folder N`。

    参数 `data` 是完整 ProjectData；返回完整字节。

    验收（`_S-BASE` 1 轨 → `_TG12`，132,596 字节 = 参考 `_S-STACK`，Logic 实测）：
    轨道行出现且名为 `Sum 1`，通道条读回 `kind: "audio"`、`child_count: 20`、
    `output: "Stereo Output"`、`volume_db: 0`、`pan: "0"` —— 与用户亲手做的
    `_S-STACK` 逐项一致。**尚未成立的一点**：`logic_set_track_stack` 仍报
    「no track stack disclosure arrow」，即 Logic 还不把它当 Stack 呈现；
    最可能是「还没有子轨」（参考样本有 2 条），需要把子轨 output 指到它的 bus 才算完整。
    """
    kind = (kind or "summing").lower()
    if kind not in ("summing", "folder"):
        raise ValueError("kind 只能是 'summing' 或 'folder'")
    if kind == "folder":
        raise NotImplementedError(
            "Folder Stack 的纯离线新建尚未实现。配方已实测完整（见 "
            "_templates/PROJECTDATA-FINDINGS.md §40），与 Summing 差三处："
            "① 覆盖块是单个 UCuA:350（Summing 是 UCuA:303 + UCuA:348 两块）；"
            "② nCuA 要 +4（Summing 的 add_stack 是把它还原）；"
            "③ 新 qeSM 长度 307 且由一个既有 qeSM:305 缩 2 字节而来（Summing 是新增 309）。"
            "暂不交付的原因：Summing 版本本身还产不出可打开的工程（§39），"
            "Folder 走同一路径只会得到同样不可打开的文件。"
            "Summing 已可用；Folder 请等待或改用逻辑自带的 Create Folder Stack。")

    ch0 = list(chunks(data))
    # 「音频轨条数」计数器：`add_audio_track` 会把它们 +1，但 Stack 主通道是 aux，
    # 不是音频轨，所以这里先把原值记下来，末尾还原（实测 `_S-BASE`/`_S-STACK` 三处恒定）。
    _g0 = next((c for c in ch0 if c.tag == "gnoS"), None)
    keep_ctr = None
    if _g0 is not None and len(_g0.payload) >= 924:
        keep_ctr = (_g0.payload[210:212], _g0.payload[214:216], _g0.payload[923:924])
    # 新 Stack 占用的 bus：已有 aux 条数 + 1（第一座 Stack → Bus 1）
    n_aux = sum(1 for c in ch0
                if c.tag == "OCuA" and c.length in (237, 241, 249) and c.payload[4] == 0x42)
    bus = n_aux + 1
    # 找「通道类」的 ivnE:469 —— 判别字节 +155：0x10 = aux/stack，0x09 = Click，
    # 0x14 = Master（注意 467 谱系里 0x14 是 folder，两个谱系语义不同）。
    want_byte = 0x10 if kind == "summing" else 0x14
    cands = []
    for c in ch0:
        if c.tag != "ivnE" or c.length != 469 or c.payload[155] != want_byte:
            continue
        nm = c.payload[160:160 + c.payload[158]]
        if nm in (b"Click", b"Master"):
            continue
        cands.append(c)
    donor = cands[-1] if cands else _stack_donor(kind)

    # 1. 先按音频轨走一遍已验证的配方
    n_audio = len(audio_strip_runs(ch0))
    default_name = ("Sum %d" if kind == "summing" else "Folder %d") % (n_audio + 1)
    ncu0 = [c for c in ch0 if c.tag == "nCuA"]
    out = add_audio_track(data, name=name or default_name, rnd=rnd,
                          _shift_env_ids=False)
    # 既有通道对象的 id（u16 LE @+82）**不能让位**：真值 `_S-STACK` 里它们与 `_S-BASE`
    # 完全相同（`0x4b / 0x9f / 0xf3 / 0x47 / 0x09`），而 `add_audio_track` 的「+66」会把
    # 它们整体推高，导致 Logic 找不到 bus 的接收者。这里还原既有 id，并给新建的通道对象
    # 分配一个未被占用的 id。
    _orig = {}
    for _c in chunks(data):
        if _c.tag == "ivnE" and len(_c.payload) > 176:
            _orig[bytes(_c.payload[160:176])] = _c.payload[82] | (_c.payload[83] << 8)
    _chs = chunks(out)
    _used = set()
    for _i, _c in enumerate(_chs):
        if _c.tag != "ivnE" or len(_c.payload) <= 176:
            continue
        _nm = bytes(_c.payload[160:176])
        if _nm in _orig:
            _pl = bytearray(_c.payload)
            _pl[82] = _orig[_nm] & 0xFF
            _pl[83] = (_orig[_nm] >> 8) & 0xFF
            _chs[_i] = with_payload(_c, bytes(_pl))
            _used.add(_orig[_nm])
    _fresh = (max(_used) + 1) if _used else 1
    # 新建的通道对象是**最后一个** `ivnE`（`add_audio_track` 追加出来的）；按名字判断会
    # 和既有对象撞名，所以这里按位置认定，并保证 id 不与任何既有 id 重复。
    _last = max(i for i, c in enumerate(_chs) if c.tag == "ivnE" and len(c.payload) > 176)
    for _i, _c in enumerate(_chs):
        if _c.tag != "ivnE" or len(_c.payload) <= 176:
            continue
        if _i == _last:
            _pl = bytearray(_c.payload)
            _pl[82] = _fresh & 0xFF
            _pl[83] = (_fresh >> 8) & 0xFF
            _chs[_i] = with_payload(_c, bytes(_pl))
    out = seal(_chs, out[:24])
    ch = list(chunks(out))
    head = header(out)
    if kind == "summing" and ncu0:
        # 样本实测：Summing Stack 不改变 nCuA；Folder Stack 才 +4。
        keep = max(ncu0, key=lambda c: c.length)
        for i, c in enumerate(ch):
            if c.tag == "nCuA" and c.length == keep.length + 4:
                ch[i] = _clone(keep, keep.payload)
                break

    # 1b. Stack 主行的混音器通道条：payload[4] 从 0x40(音频) 改成 0x42(aux/stack)，
    #     并在 +61 写混音器名。参考样本 Sum 1 的条就是 0x42 + "Aux 1"。
    if kind == "summing":
        strips = [i for i, c in enumerate(ch)
                  if c.tag == "OCuA" and c.length == 237 and c.payload[4] == 0x40]
        if strips:
            i = strips[-1]
            pl = bytearray(ch[i].payload)
            pl[4] = 0x42
            mixname = ("Aux %d" % (n_audio + 1)).encode("ascii")[:10]
            pl[61:61 + 12] = mixname + b"\x00" * (12 - len(mixname))
            ch[i] = _clone(ch[i], bytes(pl))

    # 2. 拿最后一条 ivnE:469 当模板，克隆出新的 Stack 通道
    env471 = [i for i, c in enumerate(ch) if c.tag == "ivnE" and c.length == 471]
    if not env471:
        raise ValueError("内部错误：新建的音频轨没有产生 ivnE:471")
    slot = env471[-1]

    # 名字：469 谱系里 +158 是名字长度、+160 是名字（定宽 16）
    ttl = (name or default_name)[:16]
    pay = bytearray(donor.payload)
    old_len = pay[158] if 158 < len(pay) else 0
    pay[160:160 + max(old_len, len(ttl))] = ttl.encode()[:16].ljust(max(old_len, len(ttl)), b"\0")
    pay[158] = len(ttl)
    ch[slot] = _clone(donor, bytes(pay))

    # 3. 行头池不必在这里删 —— 见第 5 步：条三块是**顶替**一格空行头，
    #    「池子少一个」与「三块落到 idx86」是同一个动作的结果。
    #    （早期这里单独删过一格，会导致池子少两个、行配错行头 → kind: "reduced"。）

    # 4. Folder 的通道条覆盖块是 UCuA:350（把新插入的 UCuA:303 换掉）
    if kind == "folder":
        idx303 = [i for i, c in enumerate(ch) if c.tag == "UCuA" and c.length == 303]
        if idx303:
            i = idx303[-1]
            c = ch[i]
            pay = bytearray(c.payload) + b"\0" * 47      # 303 -> 350
            ch[i] = _mk("UCuA", bytes(pay))

    # 5. 位置：Stack 的条三块**顶替**一格空行头，不是独立插入。
    #    实测 `_S-BASE` 的 ord5 = idx86 是 `OCuA:201` 且 `payload[4]==0x42 && payload[6]==0x00`；
    #    `_S-STACK` 在同一位置换成了 `OCuA:237(0x42)` + 两个 `UCuA`。
    #    所以「池子少一个」与「三块落在 idx86」是同一个动作的结果。
    if kind == "summing":
        trio = None
        for i, c in enumerate(ch):
            if (c.tag == "OCuA" and c.length in (237, 241, 249)
                    and c.payload[4] == 0x42
                    and i + 2 < len(ch) and ch[i + 1].tag == "UCuA" and ch[i + 2].tag == "UCuA"):
                trio = ch[i:i + 3]
                del ch[i:i + 3]
                break
        if trio is not None:
            tgt = None
            for i, c in enumerate(ch):
                if (c.tag == "OCuA" and c.length == 201
                        and c.payload[4] == 0x42 and c.payload[6] == 0x00
                        and not c.payload[97:113].split(b"\0")[0]):
                    tgt = i
                    break
            if tgt is None:
                for i, c in enumerate(ch):
                    if c.tag == "OCuA" and c.length == 201 and not c.payload[97:113].split(b"\0")[0]:
                        tgt = i
                        break
            if tgt is not None:
                ch[tgt:tgt + 1] = trio

    # 6. Stack 主行的「行记录」标志：样本实测 Stack 行是 `karT:58` 的 payload +2 = 0x01
    #    （音频轨是 0x14），且它出现在 +8 相同的一对记录中的**前一个**。
    #    `_S-BASE`（1 轨、无 Stack）的 +2 序列里 0x01 出现 0 次；
    #    `_S-STACK`（1 轨 + Sum 1）里出现在 +8=23 那一对上。
    if kind == "summing":
        recs = [(i, c) for i, c in enumerate(ch) if c.tag == "karT" and c.length == 58]
        spans = {}
        for i, c in recs:
            spans.setdefault(int.from_bytes(c.payload[8:12], "little") // 4, []).append(i)
        if spans:
            newidx = max(spans)
            if spans[newidx]:
                i = spans[newidx][0]
                pl = bytearray(ch[i].payload)
                pl[2] = 0x01
                ch[i] = _clone(ch[i], bytes(pl))

    # 6b. 行记录与条的两个字段（实测值，来自 `_S-STACK` 的 Sum 1）
    #     karT:58(+2=0x01) 载荷 `+40..43 = e0 00 10 40`、`+52..55 = 70 00 00 00`；
    #     条 `OCuA:237(0x42)` 载荷 `+6=0`、`+65='1'`（混音器格子名）、
    #     `+86=1`、`+94=1`、`+123=2`，并且 `+221:237` 必须有一段 UUID（缺了 Logic 会崩）。
    if kind == "summing":
        for i, c in enumerate(ch):
            if c.tag == "karT" and c.length == 58 and c.payload[2] == 0x01:
                pl = bytearray(c.payload)
                pl[40:44] = bytes.fromhex("e0001040")
                pl[52:56] = b"\x00\x00\x00\x00"
                ch[i] = _clone(c, bytes(pl))
        for i, c in enumerate(ch):
            if c.tag == "OCuA" and c.length == 237 and c.payload[4] == 0x42:
                pl = bytearray(c.payload)
                pl[6] = 0
                pl[65] = 0x31
                pl[86] = 1
                pl[94] = 1
                pl[123] = 2
                ch[i] = _clone(c, bytes(pl))
                break

    # 7. 通道条 ↔ 通道对象 的 UUID 配对。
    #    `add_audio_track` 里的 `link_strip_envs` 把条 `+189` 与 `ivnE:471` 的某处配对好了，
    #    但第 2 步把 `ivnE:471` 换成了 donor 的 `ivnE:469` 载荷，这一换就断了：
    #    实测 `_TG10` 里条的 UUID 在 env 载荷里 **找不到**（`find` = -1）。
    #    参考样本 `_S-STACK` 里，条的 `payload[189:205]` 出现在 env 载荷的 **+453**。
    #    不断这条链，Logic 就把行配到行头上 —— 读回 `kind: "reduced"`、`output: null`。
    if kind == "summing":
        strip = None
        for i, c in enumerate(ch):
            if c.tag == "OCuA" and c.length == 237 and c.payload[4] == 0x42:
                strip = i
                break
        if strip is not None:
            uid = ch[strip].payload[189:205]
            want = ttl.encode()[:16]
            for i, c in enumerate(ch):
                if (c.tag == "ivnE" and c.length == 469
                        and c.payload[160:160 + len(want)] == want):
                    pl = bytearray(c.payload)
                    if len(pl) >= 469:
                        pl[453:469] = uid
                        ch[i] = _clone(c, bytes(pl))
                    break

    # 9. 子轨路由 + 计数器还原（这两项决定 Summing Stack 是否真的成立）
    if kind == "summing":
        import struct as _st
        for i, c in enumerate(ch):
            if c.tag == "OCuA" and c.length in (237, 241, 249) and c.payload[4] == 0x40:
                pl = bytearray(c.payload)
                pl[92:94] = _st.pack("<H", bus + 1)   # 输出改到新 aux 的 bus
                ch[i] = _clone(c, bytes(pl))
            elif (c.tag == "OCuA" and c.length in (237, 241, 249)
                    and c.payload[4] == 0x42):
                pl = bytearray(c.payload)
                pl[86] = bus
                ch[i] = _clone(c, bytes(pl))
    # 10. 行序：Stack 主行（`karT:58` +2 = 0x01）必须排在它的子轨行之前。
    #     实测 `_S-STACK` 是 `01` 在前、`00` 在后；`add_audio_track` 追加出来的顺序是反的，
    #     只把这对相邻块交换即可（其余 `14` 组的次序由同一次插入位置决定）。
    if kind == "summing":
        for i in range(1, len(ch)):
            if (ch[i].tag == "karT" and ch[i].length == 58 and ch[i].payload[2] == 0x01
                    and ch[i - 1].tag == "karT" and ch[i - 1].length == 58
                    and ch[i - 1].payload[2] == 0x00):
                ch[i - 1], ch[i] = ch[i], ch[i - 1]
                break
        # 新的 `14` 行要**紧贴在最后一个 `14` 行之前**，而不是跟在第一个 `14` 行后面：
        # 真值 `_S-STACK` 的次序是 `14/22, 00/18, 00/19, 14/23, 14/20, 00/21`，新增的 `14/23`
        # 落在两个 `00` 之后。相对次序（22, 23, 20）本来就对，差的是与 `00` 行的交错。
        def _rows14():
            return [i for i, c in enumerate(ch)
                    if c.tag == "karT" and c.length == 58 and c.payload[2] == 0x14]
        r14 = _rows14()
        if len(r14) >= 3:
            ords = {i: int.from_bytes(ch[i].payload[8:12], "little") // 4 for i in r14}
            newest = max(ords, key=lambda i: ords[i])
            if r14[-1] - newest != 1:
                item = ch.pop(newest)
                ch.insert(_rows14()[-1], item)
        # `+14 = 0x01` / `+40 = 0x20` 这两个标记不在 `payload[2]==0x01` 那一条上，而在**紧随其后的
        # 那条 `0x00`** 上（真值 chunk 415 是 0x01、chunk 416 才有这两个标记）。
        for i, c in enumerate(ch):
            if c.tag == "karT" and c.length == 58 and c.payload[2] == 0x01:
                if i + 1 < len(ch):
                    nxt = ch[i + 1]
                    if nxt.tag == "karT" and nxt.length == 58:
                        pl = bytearray(nxt.payload)
                        pl[14] = 0x01
                        pl[40] = 0x20
                        ch[i + 1] = _clone(nxt, bytes(pl))
                break
    
    # 11. Stack 的「roster UUID」要写进**三处**（实测 `_S-STACK` 里同一个 UUID 出现 3 次）：
        #     辅助条（`OCuA:237` `payload[4]==0x42`）的 `+221`、子轨条（`0x40`）的 `+205`、
        #     以及被顶替的那条 `OCuA:201` 行头的 `+153`。漏写这三处时 Logic 打不开工程却也不报错，
        #     表现是子轨的 output 解析不出 bus（回退成 Stereo Output）。
        auxes = [(i, c) for i, c in enumerate(ch) if c.tag == "OCuA" and c.length == 237
                 and len(c.payload) > 4 and c.payload[4] == 0x42]
        plains = [(i, c) for i, c in enumerate(ch) if c.tag == "OCuA" and c.length == 237
                  and len(c.payload) > 221 and c.payload[4] == 0x40]
        rows201 = [(i, c) for i, c in enumerate(ch) if c.tag == "OCuA" and c.length == 201
                   and len(c.payload) > 169 and c.payload[4] == 0x42 and c.payload[6] == 0x00]
        if auxes and plains and rows201:
            roster = int(uuid.uuid1().int).to_bytes(16, "big")
            for idx, off in ((auxes[-1][0], 221), (plains[0][0], 205), (rows201[-1][0], 153)):
                c = ch[idx]
                pl = bytearray(c.payload)
                pl[off:off + 16] = roster
                ch[idx] = _clone(c, bytes(pl))

    out2 = seal(ch, head)
    # `normalize_counters` 不幂等：第一遍新块仍是 >=250<<16 的占位值，进不了
    # 「20..199 批次重编号」分支，所以必须跑两遍（与 `add_audio_track` 一致）。
    res = link_strip_envs(normalize_counters(normalize_counters(out2)))
    # 计数器还原必须放在 `normalize_counters` **之后**：它自己也会写这几处。
    if keep_ctr is not None:
        b = bytearray(res)
        for c in chunks(b):
            if c.tag == "gnoS" and len(c.payload) >= 924:
                base = c.pos + 36
                b[base + 210:base + 212] = keep_ctr[0]
                b[base + 214:base + 216] = keep_ctr[1]
                b[base + 923:base + 924] = keep_ctr[2]
                break
        res = bytes(b)
    return res


def _stack_donor(kind="summing"):
    """工程里没有现成 aux 时，从随包的样本工程里取一条 Stack 主通道当模板。"""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nm = "stack_summing.bin" if kind == "summing" else "stack_folder.bin"
    path = os.path.join(here, "templates", "donors", nm)
    with open(path, "rb") as f:
        d = f.read()
    want_byte = 0x10 if kind == "summing" else 0x14
    for c in chunks(d):
        if c.tag != "ivnE" or c.length != 469 or c.payload[155] != want_byte:
            continue
        nm = c.payload[160:160 + c.payload[158]]
        if nm in (b"Click", b"Master"):
            continue
        return c
    raise ValueError("样本工程里也没有 Stack 主通道，%s 已损坏" % path)


def P_tag_payload(tag: str, payload: bytes):
    """用给定 tag + 载荷拼一个块（头部其余字段取 0，交给 `normalize_counters` 修）。"""
    import struct as _s
    h = bytearray(36)
    h[0:4] = tag.encode("ascii")
    _s.pack_into("<H", h, 4, 7)
    _s.pack_into("<H", h, 6, 14)
    _s.pack_into("<I", h, 20, 0xFFFFFFFF)
    _s.pack_into("<I", h, 28, len(payload))
    return h + payload
