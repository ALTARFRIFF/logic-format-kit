#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""logicfmt.classinfo —— 让插件自己写状态（方案 L2）。

问题
----
每个插件把自己的状态塞进一个私有容器：FabFilter 用 FFBS 浮点数组，
Plugin Alliance 用 Pmtr，JUCE 用 VC2! 文本，iZotope 用 zlib+JSON，
Waves 用 XML 套壳。想改一个参数，传统做法是逐个逆向这些容器。

L2 的思路是绕开它：**把插件实例化，让它自己把状态写出来。**

    AudioComponentFindNext → AudioComponentInstanceNew
    → AudioUnitSetParameter（改值）
    → AudioUnitGetProperty(kAudioUnitProperty_ClassInfo)
    → CFPropertyList → 一个 plist，key 就是容器名，value 就是容器字节

得到的字节与 Logic 自己存进 `.cst` 的**完全同格式**（Plugin Alliance
家族实测逐字节相同），所以可以直接替换掉 `.cst` 里原有的那段。

已实测（本机，2026-10）
------------------------
| 家族                | ClassInfo key          | 参数写入 |
|---------------------|------------------------|----------|
| FabFilter           | FabFilterPluginState   | ✅       |
| Plugin Alliance/SPL | data                   | ✅       |
| accusonus / JUCE    | jucePluginState        | ✅       |
| iZotope             | data                   | ✅       |
| Waves               | Waves_XPst             | ❌ 只读，但可加载预设 |

Waves 的 AU 参数对外只读（SetParameter 返回 0 但读回仍是默认值），
不过它的状态是纯文本 XML（`<PresetChunkXMLTree>` + 一串实数），
所以 Waves 应该走文件直写，不走 L2。

命令行
------
    python3 -m logicfmt.classinfo probe  aumf FQ4p FabF
    python3 -m logicfmt.classinfo blob   aufx SPTP SPL1 --out /tmp/s.blob
    python3 -m logicfmt.classinfo set    aumf FQ4p FabF --param 2 --value 8.4512
    python3 -m logicfmt.classinfo presets aufx RS1M ksWV
    python3 -m logicfmt.classinfo load   aufx RS1M ksWV --preset 7
    python3 -m logicfmt.classinfo selftest
"""

from __future__ import annotations

import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
TOOLS = os.path.join(KIT, "tools")
CLASSINFO_C = os.path.join(TOOLS, "au_classinfo.c")
CLASSINFO = os.path.join(TOOLS, "au_classinfo")
AU_DUMP = os.path.join(TOOLS, "au_dump")

TIMEOUT = 60


class ClassInfoError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 工具编译
# --------------------------------------------------------------------------

def ensure_tools(tools_dir: str | None = None) -> dict:
    """编译 tools/au_classinfo 与 tools/au_dump（缺哪个编译哪个）。返回路径字典。"""
    d = tools_dir or TOOLS
    built = {}
    for stem in ("au_classinfo", "au_dump"):
        c = os.path.join(d, stem + ".c")
        binp = os.path.join(d, stem)
        if (not os.path.exists(binp)) or (os.path.getmtime(c) > os.path.getmtime(binp)):
            cc = shutil.which("clang") or shutil.which("cc")
            if not cc:
                raise ClassInfoError(f"需要 clang 来编译 {c}，但没找到")
            r = subprocess.run([cc, "-O2", "-o", binp, c,
                                "-framework", "AudioToolbox",
                                "-framework", "CoreFoundation"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise ClassInfoError(f"编译 {stem} 失败:\n" + (r.stderr or "")[:2000])
        built[stem] = binp
    return {"classinfo": built["au_classinfo"], "audump": built["au_dump"]}


def _run(args, timeout=TIMEOUT):
    """跑一个工具，返回 (stdout, stderr)。Waves 会往 stderr 喷 objc 警告，忽略。"""
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr


# --------------------------------------------------------------------------
# 读 ClassInfo
# --------------------------------------------------------------------------

def class_info(au_type: str, subtype: str, manufacturer: str, *,
               param: int | None = None, value: float | None = None,
               preset: int | None = None, keep_plist: bool = False) -> dict:
    """实例化一个 AU，返回它自己写出来的状态。

    返回 {name, keys:{key: value}, blob_key, blob, plist}
      - keys 里 data 型值是 bytes，数字是 int，字符串是 str
      - blob_key / blob 是按容器 magic 挑出来的那个状态
    """
    paths = ensure_tools()
    fd, tmp = tempfile.mkstemp(suffix=".plist"); os.close(fd)
    cmd = [paths["classinfo"], au_type, subtype, manufacturer, "--out", tmp]
    if param is not None and value is not None:
        cmd += ["--set", str(param), repr(float(value))]
    if preset is not None:
        cmd += ["--preset", str(preset)]
    try:
        out, err = _run(cmd)
    finally:
        pass

    name = ""
    for line in out.splitlines():
        if line.startswith("# ") and not line.startswith("#   ") and not line.startswith("# ClassInfo"):
            name = line[2:].strip()
            break
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise ClassInfoError(
            "插件的 ClassInfo 读不出来（可能没有这个 AU，或需要授权）\n"
            + "\n".join(l for l in out.splitlines() if l.startswith("#"))[:800])
    d = plistlib.loads(open(tmp, "rb").read())
    if not keep_plist:
        os.unlink(tmp)
    else:
        d["_plist"] = tmp
    key, blob = _pick_blob(d)
    return {"name": name, "keys": d, "blob_key": key, "blob": blob,
            "plist": tmp if keep_plist else None}


FAMILY_BY_MAGIC = (
    (b"FFBS", "FabFilterPluginState", "fabfilter"),
    (b"Pmtr", "data", "plugin-alliance"),
    (b"VC2!", "jucePluginState", "juce"),
)


def _pick_blob(d: dict) -> tuple[str, bytes]:
    """按容器 magic 挑出状态字节。挑不出来就取最大的 data。"""
    datas = {k: bytes(v) for k, v in d.items()
             if isinstance(v, (bytes, bytearray)) and not k.startswith("_")}
    if not datas:
        raise ClassInfoError("ClassInfo 里没有任何二进制状态（这个插件可能不存状态）")
    for magic, prefer, _fam in FAMILY_BY_MAGIC:
        for k, v in datas.items():
            if v.startswith(magic):
                return k, v
    if "Waves_XPst" in datas:
        return "Waves_XPst", datas["Waves_XPst"]
    if "data" in datas or len(datas) == 1:
        # iZotope 等私有容器：头部没有可读 magic，data 就是状态
        return "data", datas.get("data") or max(datas.values(), key=len)
    k = max(datas, key=lambda x: len(datas[x]))
    return k, datas[k]


def state_blob(au_type, subtype, manufacturer, **kw) -> bytes:
    return class_info(au_type, subtype, manufacturer, **kw)["blob"]


# --------------------------------------------------------------------------
# 描述一个状态 blob（用于验证，不依赖插件）
# --------------------------------------------------------------------------

def describe_blob(blob: bytes) -> dict:
    b = bytes(blob)
    out = {"size": len(b), "head": b[:16].hex()}
    if b.startswith(b"FFBS"):
        n = struct.unpack_from("<I", b, 8)[0]
        out.update(family="fabfilter", container="FFBS",
                   version=struct.unpack_from("<I", b, 4)[0], params=n,
                   expected_size=12 + 4 * n)
    elif b.startswith(b"Pmtr"):
        out.update(family="plugin-alliance", container="Pmtr",
                   note="tag 记录表，记录数 = 可在文件里扫出来的参数数")
    elif b.startswith(b"VC2!"):
        n = struct.unpack_from("<I", b, 4)[0]
        xml = b[8:8 + n].decode("utf-8", "replace")
        import re
        m = re.search(r"<([A-Za-z_][\w.-]*)", xml)
        out.update(family="juce", container="VC2!", xml_root=m.group(1) if m else "?",
                   xml_len=n, declared_total=n + 9)
    elif b"XPst" in b[:64]:
        import re
        i = b.find(b"XPst")
        payload = int.from_bytes(b[i - 4:i], "big")
        xml = b[i + 4:].decode("utf-8", "replace")
        toks = re.findall(r'<Parameters[^>]*>(.*?)</Parameters>', xml, re.S)
        nums = sum(len(t.split()) for t in toks)
        out.update(family="waves", container="Waves_XPst/XPst",
                   xml_payload=payload, xml_bytes=len(b) - (i + 4),
                   xml_tokens=nums)
    elif len(b) >= 16 and b[16:18] == b"\x78\x9c":
        import zlib
        declared = struct.unpack_from("<I", b, 12)[0]
        out.update(family="izotope", container="header+zlib",
                   declared_uncompressed=declared,
                   len_field_matches=(struct.unpack_from("<I", b, 8)[0] == len(b) - 12),
                   decompresses=_try_zlib(b[16:], declared))
    else:
        # 未解容器：把 4 字节 magic 作为证据报出来，别静默当空气
        magic = b[:4].decode("latin-1")
        out.update(family="unknown", container="?",
                   magic=magic if magic.isprintable() else None,
                   note="未解容器：读得到、写不了。已知样本：Polyverse `PvWr`(44B)、"
                        "Slate `FrAr`(2522B)、WaveShell —— 缺真值对照对，不做盲探。")
    return out


def _try_zlib(stream: bytes, declared: int) -> bool:
    import zlib
    try:
        return len(zlib.decompress(stream)) == declared
    except zlib.error:
        return False


# --------------------------------------------------------------------------
# 预设
# --------------------------------------------------------------------------

def presets(au_type, subtype, manufacturer) -> list[str]:
    paths = ensure_tools()
    out, _ = _run([paths["classinfo"], au_type, subtype, manufacturer, "--presets"])
    names = []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("#   [") and "]" in s:
            names.append(s.split("]", 1)[1].strip())
    return names


# --------------------------------------------------------------------------
# 参数表（复用 au_dump；不在就退回 param-maps）
# --------------------------------------------------------------------------

def params(au_type, subtype, manufacturer, *, tools_dir=None,
           maps_dir=None) -> list[dict]:
    paths = ensure_tools(tools_dir)
    audump = paths["audump"]
    if os.path.exists(audump):
        out, _ = _run([audump, au_type, subtype, manufacturer, "--tsv", "--values"])
        rows = []
        for line in out.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            f = line.split("\t")
            if len(f) < 6:
                continue
            try:
                rows.append({"index": int(f[0]), "id": int(f[1]), "name": f[2],
                             "min": float(f[3]), "max": float(f[4]),
                             "default": float(f[5])})
            except ValueError:
                continue
        if rows:
            return rows
    # 退回静态表
    d = maps_dir or os.path.join(KIT, "param-maps")
    f = os.path.join(d, f"{manufacturer}_{subtype}.txt")
    if not os.path.exists(f):
        raise ClassInfoError(f"既不能实例化，也没有参数表：{f}")
    rows = []
    for line in open(f, encoding="utf-8", errors="replace"):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        rows.append({"index": int(parts[0]), "id": int(parts[1]),
                     "name": " ".join(parts[2:-4]).strip(),
                     "min": float(parts[-4]), "max": float(parts[-3]),
                     "default": float(parts[-2])})
    return rows


# --------------------------------------------------------------------------
# 写回 .cst
# --------------------------------------------------------------------------

def write_blob_into_cst(cst: bytes, insert_index: int, new_blob: bytes,
                        key: str | None = None) -> bytes:
    """把 L2 生成的状态替换进 .cst 的第 insert_index 个插件块（0 基）。

    长度必须相等 —— 用 L2 的话通常不等（默认状态比调过的状态短），
    所以这里会先检查，不等就直接报错，不做静默填充。
    """
    import au_batch
    buf = bytearray(cst)
    states = list(au_batch.cst_states(buf)) if hasattr(au_batch, "cst_states") else []
    if not states:
        raise ClassInfoError("这个 .cst 里没有找到可写的插件状态")
    target = None
    for st in states:
        idx = st[0] if isinstance(st, tuple) else st.get("index")
        if idx == insert_index:
            target = st
            break
    if target is None:
        raise ClassInfoError(f"没有第 {insert_index} 个插件块")
    off, total, plist = (target[1], target[2], target[3])
    k = key or _pick_blob(plist)[0]
    old = bytes(plist[k])
    if len(old) != len(new_blob):
        raise ClassInfoError(
            f"长度不等，拒绝写入：原 {len(old)} 字节，新 {len(new_blob)} 字节。"
            f"L2 生成的是完整状态，通常需要连同块长度一起处理。")
    au_batch._write_cst_state(buf, off, total, k, new_blob)
    return bytes(buf)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

PROBES = (
    # (type,   subtype, mfr,   family,           param_write)
    ("aumf", "FQ4p", "FabF", "fabfilter",        True),
    ("aufx", "SPTP", "SPL1", "plugin-alliance",  True),
    ("aufx", "dLay", "oDin", "juce",             True),
    ("aufx", "NCTG", "iZtp", "izotope",          True),
    ("aufx", "RS1M", "ksWV", "waves",            False),
    # 只读验证：accconus ERA 6（NrP6）是已知例外——AU 接受了参数值（回读确认
    # 变了），但它自己吐出的 jucePluginState 逐字节不变，所以不测写入。
    ("aufx", "NrP6", "Accu", "juce",             False),
)


def _selftest() -> int:
    print("classinfo selftest")
    ok = bad = skip = 0
    paths = ensure_tools()
    print(f"  tools: {paths['classinfo']}")
    # 未解容器：合成样本（不需要装任何插件）——必须报得出 magic，且不许假装认识
    for tag, size in ((b"PvWr", 44), (b"FrAr", 2522)):
        d = describe_blob(tag + b"\x00" * (size - 4))
        good = d["family"] == "unknown" and d.get("magic") == tag.decode()
        print(f"  {'OK  ' if good else 'FAIL'} {tag.decode():5s} 合成 {size}B  "
              f"family={d['family']} magic={d.get('magic')}")
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
    for au_type, sub, mfr, fam, writable in PROBES:
        try:
            ci = class_info(au_type, sub, mfr)
        except Exception as e:
            print(f"  SKIP {sub}: {str(e).splitlines()[0]}")
            skip += 1
            continue
        desc = describe_blob(ci["blob"])
        good = desc["family"] == fam
        print(f"  {'OK  ' if good else 'FAIL'} {sub:5s} {ci['blob_key']:22s} "
              f"{desc['size']:5d}B  family={desc['family']:15s} (期望 {fam})")
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)

        if writable:
            try:
                ps = params(au_type, sub, mfr)
            except Exception:
                skip += 1
                continue
            if not ps:
                skip += 1
                continue
            # 挑量程连续的参数：布尔开关（0/1）改了可能被量化回原值。
            # 另外有些参数在 AU 层就是只读的（status=0 但回读不变，例如 ERA 6 的
            # On/Off），所以最多试 4 个，只要有一个能让状态变化就算通过。
            wide = [q for q in ps if q["max"] - q["min"] > 2]
            cand = (wide + [q for q in ps if q not in wide])[:4]
            changed, last = False, None
            for p in cand:
                mid = p["min"] + (p["max"] - p["min"]) * 0.371
                last = (p, mid)
                if class_info(au_type, sub, mfr, param=p["id"], value=mid)["blob"] != ci["blob"]:
                    changed = True
                    break
            p, mid = last
            note = "" if changed else f"（试了 {len(cand)} 个，可能都只读）"
            print(f"       {'OK  ' if changed else 'FAIL'} 写参数 #{p['id']} "
                  f"({p['name'][:24]}) = {mid:g} → 状态{'变化' if changed else '未变'}{note}")
            ok, bad = (ok + 1, bad) if changed else (ok, bad + 1)

    # describe_blob 自身的断言
    ff = b"FFBS" + struct.pack("<II", 1, 3) + b"\0" * 12
    assert describe_blob(ff)["params"] == 3
    ok += 1
    print(f"\n  {ok} passed, {bad} failed, {skip} skipped")
    return 0 if bad == 0 else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv.pop(0)

    def need(n, usage):
        if len(argv) < n:
            raise SystemExit(f"用法: {usage}")

    if cmd == "selftest":
        return _selftest()

    if cmd == "probe":
        need(3, "probe <type> <subtype> <manufacturer>")
        ci = class_info(*argv[:3])
        d = describe_blob(ci["blob"])
        print(f"{ci['name']}")
        print(f"  key      {ci['blob_key']}")
        print(f"  size     {d['size']}")
        for k, v in d.items():
            if k in ("size", "head"):
                continue
            print(f"  {k:24s} {v}")
        for k, v in ci["keys"].items():
            if isinstance(v, (bytes, bytearray)):
                print(f"  key {k:24s} data {len(v)}B")
            else:
                print(f"  key {k:24s} {v}")
        return 0

    if cmd == "blob":
        out = None
        if "--out" in argv:
            i = argv.index("--out"); out = argv[i + 1]; del argv[i:i + 2]
        need(3, "blob <type> <subtype> <manufacturer> [--out PATH]")
        b = state_blob(*argv[:3])
        if out:
            open(out, "wb").write(b)
            print(f"{len(b)} bytes -> {out}")
        else:
            sys.stdout.buffer.write(b)
        return 0

    if cmd == "set":
        param = value = None
        if "--param" in argv:
            i = argv.index("--param"); param = int(argv[i + 1]); del argv[i:i + 2]
        if "--value" in argv:
            i = argv.index("--value"); value = float(argv[i + 1]); del argv[i:i + 2]
        out = None
        if "--out" in argv:
            i = argv.index("--out"); out = argv[i + 1]; del argv[i:i + 2]
        need(5, "set <type> <subtype> <manufacturer> --param N --value V [--out PATH]")
        ci = class_info(*argv[:3], param=param, value=value)
        print(f"{len(ci['blob'])} bytes  key={ci['blob_key']}")
        if out:
            open(out, "wb").write(ci["blob"]); print(f"-> {out}")
        return 0

    if cmd == "presets":
        need(3, "presets <type> <subtype> <manufacturer>")
        for i, n in enumerate(presets(*argv[:3])):
            print(f"{i:3d}  {n}")
        return 0

    if cmd == "load":
        preset = None
        if "--preset" in argv:
            i = argv.index("--preset"); preset = int(argv[i + 1]); del argv[i:i + 2]
        need(3, "load <type> <subtype> <manufacturer> --preset N")
        ci = class_info(*argv[:3], preset=preset)
        print(f"loaded preset {preset}; state now {len(ci['blob'])} bytes")
        return 0

    if cmd == "params":
        need(3, "params <type> <subtype> <manufacturer>")
        for r in params(*argv[:3]):
            print(f"{r['index']:4d} {r['id']:5d}  {r['name']:32s} "
                  f"{r['min']:10g} {r['max']:10g} {r['default']:10g}")
        return 0

    raise SystemExit(f"未知子命令: {cmd}")


if __name__ == "__main__":
    sys.exit(main())
