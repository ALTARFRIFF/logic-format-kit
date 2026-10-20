# logic-format-kit

**Logic Pro 的 `.patch` / `.cst` / `.logicx` 文件格式资料与参考实现。**

给需要**在 Logic 之外生成或修改 Logic 文件**的人——尤其是让 AI 能自主完成这件事。

> 这不是官方规范。所有结论来自「拿 Logic 自己写出的文件做差分 + 用 Logic 打开验证」。每条都标了验证强度。

---

## 这套资料要解决什么问题

Logic Pro 的文件格式没有公开文档。想批量做 77 条通道条预设、或者让脚本改一个工程的拍号，正常路径是不存在的：没有 CLI，没有 API，`.logicx` 是一个内部结构未知的二进制包。

本资料把这几个格式**解到可以直接读写的程度**，并且给出了**当解码失败时怎么办**的兜底策略。

---

## 能力矩阵（诚实版）

| 能力 | 状态 | 说明 |
|---|---|---|
| 读/写 `.patch` / `.cst` 结构 | ✅ **实测** | 块流、插件槽位、名字、UUID、增删插件 |
| 写插件参数（FabFilter） | ✅ **实测** | Pro-Q 4 / Pro-C 3 / Pro-DS / Pro-MB |
| 写插件参数（Plugin Alliance 家族） | ✅ **实测** | Pmtr 容器，含 SPL / elysia / Maag / Brainworx |
| 写插件参数（Valhalla / JUCE） | ✅ **实测** | VC2! XML |
| 写插件参数（iZotope） | ✅ **实测** | 16 字节头 + zlib + JSON |
| 写插件参数（Waves） | ✅ **`data` 记录表已解** | `[u32 参数 id][f32 值]`，等长改 f32 |
| **读/写任意插件参数（不依赖容器）** | ✅ **实测 8 个插件 / 6 家族** | L2 `logicfmt/classinfo.py`，见下 |
| 写 `.logicx` 拍号（任意小节） | ✅ **实测** | 用户 6 项验收全过 |
| 写 `.logicx` 调号（任意小节，大小调 + 升降方向） | ✅ **实测** | 7 文件 13 记录零例外 |
| 写 `.logicx` 和弦轨 | ✅ **实测** | 55 条记录零例外 + Logic 打开确认 |
| 读/写推子、声像、静音、独奏、输出路由、Send | ✅ **实测** | `logicfmt/mixer.py`；dB 曲线十点标定 |
| 读/写 Aux / Stack 主轨（类型、名字） | ✅ **实测** | `ivnE(467)`，`+155` 三向判别（Aux/Summing/Folder） |
| Stack 成员关系 | ✅ **实测** | Folder = 轨条 `+110`；Summing = 输出路由 |
| 从零新建一条音频轨（**运行时**） | ✅ **可用** | Logic 自己建，其余全部离线改，见 `docs/07` |
| 从零新建一条音频轨（纯离线） | ✅ **Logic 实测通过** | `project.add_audio_track()`；`_Y5`（2 轨→3 轨）、`_Y6`（3 轨→4 轨）轨道行与混音器通道条均正确；见 `docs/07` |
| 从零新建 Stack / Aux | 🟡 **行已出现，Stack 语义未闭合** | 纯离线：`add_stack()` 能把新行建出来、通道条读到 `kind: audio`，但 Logic 仍不认它是 Stack（差 roster UUID 的第 4 个落点；三个已知落点写对仍会崩，见 `_templates/PROJECTDATA-FINDINGS.md` §38 / §38.1）；**运行时 `logic_duplicate_track()` 复制一条已有 Stack 是 ✅ 已实测** |
| 真插入（真让文件变长） | ✅ **已验证** | `insert_in_payload` + `seal`，往 `qSvE` 插记录实测通过 |
| **BPM 变化（任意小节）** | ✅ 实测通过（`logicfmt/tempo.py`） | 位置 = tick+38400，值 = BPM×10000 |
| Marker 位置 | ❌ 未解（载体 `qSxT` 只存 RTF 名字，移动标记不改它一个字节） | — |
| 自动化曲线 | 🟡 载体已定位（`vEuA:52` + `qSvE` 大块），控制点未解 | — |
| MIDI 区域 | 🟡 容器已定位，字段未解 | — |
| 从零生成 `.logicx` | ❌ 不推荐 | 模板 + 改写 |

**覆盖数字**：实测样本库 747 个插件状态 blob，**732 个（98.0%）** 可按键名读写，涉及 62 638 个具名参数。

---

## 三条核心原则

1. **不要手搓容器，让 Logic 写。** 先存一个模板，然后只改模板里已有的字段。
2. **等长写入优先。** 不改长度就不需要修任何父级长度字段。
3. **每次写入都要独立验证。** Logic 不会报错——它只会静默地不生效。

前两条在 77 条 patch 和工程文件的拍号/调号/和弦上都跑通了。第三条救过很多次。

---

## 兜底链：写不进去的时候

| 层 | 机制 | 覆盖率 | 需要装 Logic |
|---|---|---|---|
| **L1** 容器直写 | 认 magic → codec → 等长手术 | 98%（实测） | 否 |
| **L2** AU 自序列化 | 让插件自己吐 `ClassInfo` | **实测 6 家族全通** | 否 |
| **L3** 运行时 | 在 Logic 里设好再存 | 100% | 是 |

> **L2 已经做完并验证了。** `AudioUnitGetProperty(kAudioUnitProperty_ClassInfo)` 能拿到插件自己生成的合法状态，**完全不需要知道任何容器格式**。
>
> ```bash
> logicfmt/classinfo.py probe aufx SPTP SPL1     # 看它吐了哪些键
> logicfmt/classinfo.py set   aufx FQ4p FabF 2 8.45
> logicfmt/classinfo.py selftest                 # 11 passed / 0 failed
> ```
>
> 实测：Plugin Alliance / SPL 的 L2 输出与 Logic 存储**逐字节相同**；FabFilter / Valhalla / Swivel / ADPTR / iZotope 都能写。
> 两个必须知道的坑：**JUCE 的参数 ID 是名字哈希**（不是序号）；**accconus ERA 6 会接受参数值但状态不变**——所以别相信 `status == noErr`，要回读参数值 + 对比 blob 两步都过。

---

## 目录

```
CORE.md                       ← **先读这个**：决策树 + 铁律 + 兜底链 + 未知清单
docs/
  01-cst-format.md            .patch/.cst 块流、双槽位字段、5 个 UUID、名字限制
  02-au-containers.md         5 种插件状态容器 + 参数寻址 + L2（已验证）
  03-project-timeline.md      .logicx 结构 + 拍号/调号/和弦
  04-runtime-and-templates.md 模板怎么造、运行时能做什么、谁来做
  05-verification.md          验证清单 + 五种"假成功"
  06-failure-modes.md         症状 → 根因 → 修法速查
  07-track-creation.md        新建轨道：完整配方（9 插入点 + 计数器 + 验收）
logicfmt/
  # —— 通道条 / 插件参数（docs/01、02）——
  au_state.py                 5 种容器的读/写/校验（~1900 行，含 selftest）
  au_batch.py                 声明式批量写入（spec JSON → 逐条应用）
  mkpatch.py                  从模板 .patch 组装新通道条（槽位/名字/UUID 全处理）
  classinfo.py                L2：让插件自己吐出状态（任意插件写参数的兜底，含 selftest）
  # —— 工程时间线（docs/03）——
  meterkey.py                 拍号/调号编解码（自带 selftest）
  mixer.py                    推子/声像/静音/独奏/输出路由/Send（自带 39 条断言）
  chord.py                    和弦轨编解码（自带 selftest）
  build_test_mk.py            从模板生成一个改拍号/调号的工程
  build_test_chord.py         从模板生成一个改和弦的工程
tools/                        参数表 dump 工具（C + Python）
templates/MANIFEST.md         模板清单与规格
```

---

## 三十秒开始

只依赖 Python 3 标准库。

```bash
python3 logicfmt/chord.py       # 和弦模块自检（不需要 Logic，不需要任何文件）
python3 logicfmt/meterkey.py    # 拍号/调号自检
python3 -m logicfmt.mixer       # 通道条/Send 自检
```

有模板之后：

```bash
python3 logicfmt/build_test_chord.py --dry-run    # 看它要改什么，不写盘
python3 logicfmt/build_test_chord.py             # 真的生成
```

模板**已经随包分发**在 `templates/` 里——直接用，不用自己造。
只有两种情况需要重建：**你的 Logic 版本不同**，或**槽位不够**（`docs/04 §2.6`）。

通道条工具的模板位置默认是 `~/Music/Audio Music Apps/Patches/Audio`，可用环境变量改：

```bash
LFK_PATCH_ROOT="/path/to/Patches/Audio" python3 logicfmt/mkpatch.py spec.json
```

---

## 适用范围与免责

- 实测于 **Logic Pro 12.4，build 6707**。其它版本偏移**可能不同**——换版本第一件事是拿模板重跑自检。
- 所有内容都是**独立逆向**得到的，与 Apple 无关。Logic、Logic Pro 及各插件名是各自所有者的商标。
- 写入有风险。**永远在副本上做，永远保留原文件**，并按 `docs/05` 验证。
- 本项目**故意不覆盖**：从零构造工程、绕过授权、修改他人的版权素材。

---

## 许可

- **代码**（`logicfmt/`、`tools/`）：MIT，见 `LICENSE-MIT`
- **文档**（`CORE.md`、`README*.md`、`docs/`）：CC BY 4.0，见 `LICENSE-CC-BY-4.0`

---

## 英文版

见 `README.en.md`。
