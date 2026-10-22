# CORE —— 先读这一份

> 这份文件是整套资料的入口。它只回答三个问题：**你在改什么**、**按什么顺序判断**、**什么时候该去读附录**。
> 具体字节偏移全部在 `docs/` 里，这里不重复。

---

## 0. 本资料的性质（请先接受这个前提）

这是**基于实测的格式笔记**，不是 Apple 的官方规范。所有结论都来自「拿两个只差一处的文件做差分 + 用 Logic 实际打开验证」。因此：

- 每一条断言都标了**验证强度**：`实测`（有文件级证据 + Logic 回读）/ `推断`（有字节证据但未回读）/ `未知`。
- 看到 `未知` 就**不要猜**，改用 §4 的兜底链，或去 §5 找"如何自己测一次"。
- 我们只覆盖到「Logic Pro 12.4 build 6707」。其它版本可能不同。

**第一个铁律：Logic 不会给你报错。** 文件格式错一个字节，结果不是"报错"，而是「打不开」「打开了但对象消失」「自动被改回默认值」。所以**任何一次写入都必须有独立的回读验证**（见 `docs/05-verification.md`），否则你不知道自己成功还是失败。

---

## 1. 你在改什么：三个文件家族

| 家族 | 是什么 | 主要变量 | 附录 |
|---|---|---|---|
| **`.patch` / `.cst`** | 一条**通道条**的预设（插件链 + 参数 + 名字 + 颜色） | 插件槽位、插件状态 blob | `docs/01-cst-format.md` |
| **`.logicx`** | 一个**工程**（轨道、路由、拍号、调号、和弦、自动化、Markers） | 定时对象（tempo/meter/key/chord） | `docs/03-project-timeline.md` |
| **AU 状态 blob** | 单个插件内部状态的字节块，**嵌在上面两者里** | 参数值 | `docs/02-au-containers.md` |

它们不是三层不同的技术，而是**同一个块流**的不同宿主：

```
.logicx/Alternatives/000/ProjectData   ┐
.patch/#Root.cst                       ├─ 都是 tag(4) + 头 + u32 LE 长度@+28 + 载荷   块总长 = 长度 + 36
                                       ┘
        └─ 载荷里可能嵌 bplist / base64 / 插件状态 blob
```

这条同源性很有用：**一个 `.cst` 的解析器可以直接读 `ProjectData` 的通道条部分**。反过来，`.logicx` 里的定时对象用的是**另一套 24/32/48/497 字节的定长记录**，跟通道条无关。

---

## 2. 五条铁律

1. **不要手搓容器，让 Logic 写。** 先用 Logic 存一个"模板"，然后只改模板里的值。这一点在 patch（77 条）和工程（拍号/调号/和弦）上都验证过：手写整个对象图极易静默失败，而改模板里已有的字段几乎不会。
2. **等长写入优先。** 只改已有字节、不改长度，就不需要修正任何父级长度字段，风险最低。需要"变长"时优先用**模板预埋空槽位**（例如模板里预埋 8 条空调号事件，只改数值），而不是真的插入字节。
3. **diff 之前先掩掉 UUID。** Logic **每次保存都会重新生成全部时间型 UUID**。不掩的话同一个工程改一个值会产生上万个"差异"，信号完全被淹没。这是本项目早期反复失败的唯一根因。
   ```python
   # 时间型 UUID 的识别（在任意偏移 i 上测试）
   is_uuid = (b[i+6] & 0xF0) == 0x10 and (b[i+8] & 0xC0) == 0x80 and (b[i+9] & 0xC0) == 0x80
   ```
4. **写入顺序受语义约束。** 改调号会问"是否把和弦轨一起变调"，所以**先写调号、再写和弦**。同类约束还有：拍号变更会改变小节到 tick 的换算，所以**先写拍号、再算其它对象的位置**。
5. **报告"成功"不等于文件变了。** 必须用三种独立证据之一确认：文件 md5 变化 / `cmp -l` 的差异字节数 / 重新解析后读回目标值。曾经出现过"每一行都打印 OK、退出码正常、输出文件与输入 md5 完全相同"的假成功。

---

## 3. 判定决策树（要写一个值，按这个顺序问）

```
你要改的是……
│
├─ 通道条里的插件参数
│  ├─ 该插件在已知容器里？ ──────────────→ 用 L1 容器直写（docs/02）
│  │    Pmtr / FFBS / flags / VC2! / iZotope
│  └─ 不在已知容器里？ ──────────────────→ 用 L2 通用 ClassInfo（docs/02 §8）
│
├─ 通道条的结构（插件顺序、增删插件、名字、颜色）
│  └─ 在模板 .cst 上改（docs/01）：槽位是**两个字段**，别只改一个
│
├─ 工程里的一个"定时事件"（拍号 / 调号 / 和弦 / tempo）
│  ├─ 模板里已有槽位？ ──────────────────→ 就地覆盖（docs/03）★ 最安全
│  └─ 需要更多槽位？ ────────────────────→ 换一个槽位更多的模板（现在不支持真插入）
│
├─ 工程里的轨道 / AUX / Stack / 路由 / 推子
│  ├─ 你已经装有 Logic 且接受运行时工具？ → 用运行时工具（docs/04）
│  └─ 纯离线？ ──────────────────────────→ **本资料未解出**（docs/03 §10）
│
└─ 自动化曲线 / Markers
   ├─ 模板里已有该 lane / marker？ ───────→ 就地改点（部分实测，见 docs/03 §10）
   └─ 需要新建 lane ─────────────────────→ 运行时工具
```

**读到这里就够了**，除非你要动具体偏移。下面的表告诉你该翻哪一页。

---

## 4. 三层兜底链（写不进去时按顺序降级）

| 层 | 机制 | 覆盖率 | 需不需要知道容器格式 | 状态 |
|---|---|---|---|---|
| **L1** | **容器直写**：认 magic → 对应 codec → 等长字节手术 | 实测库内 **98.0%** 的状态 blob（732/747） | 需要 | ✅ 已实现（`logicfmt/`、`au_state.py`） |
| **L2** | **AU 自序列化**：让插件自己吐出状态 | 实测 **8 个插件 / 6 家族全通**（1 个已知例外） | **完全不需要** | ✅ 已实现并自检（`logicfmt/classinfo.py`） |
| **L3** | **运行时**：在 Logic 里设好参数，再存成 patch/工程 | 100% | 不需要 | ✅ 已实证（Logician MCP / 手工） |

### L2 为什么是关键（已经验证了）

每一个 AU 都能被要求"把你现在的状态序列化给我"：

```
AudioComponentFindNext(desc)            → 找到插件
AudioComponentInstanceNew(comp, &inst)  → 实例化（iLok 插件需 LoadOutOfProcess）
AudioUnitSetParameter(inst, id, ...)    → 设参数
AudioUnitGetProperty(inst, kAudioUnitProperty_ClassInfo, ...)
                                        → 拿到插件**自己生成**的合法状态 plist
```

拿到的那块 plist 就是 `.cst` 里该插件要放的东西——**不需要知道任何容器格式**。

```python
import classinfo
ci = classinfo.class_info("aufx", "SPTP", "SPL1", param=0, value=0.75)
classinfo.write_blob_into_cst(cst_bytes, insert_index, ci["blob"])
```

实测结论（`classinfo.py selftest` → 11 passed / 0 failed）：

- **Plugin Alliance / SPL 系：L2 输出与 Logic 存储逐字节相同**（264 字节，0 处差异）。
- FabFilter / Valhalla / Swivel / ADPTR / iZotope ERA / Nectar 4 Gate：都能写。
- **FFBS 的浮点数就是 AU 参数原值**，不做换算（Pro-Q 4 的频率量程本身就是 log2）。
- **JUCE 的参数 ID 是名字哈希**（`105897776` 这种），不是序号——只能按 ID 或名字定位。
- **已知例外**：accconus ERA 6 接受参数值（回读都变了），但它自己吐出的 `jucePluginState` 不变。所以**不能相信 `status == noErr`**，必须回读参数值 + 对比 blob 两步都过。
- Waves 的**真状态**是 ClassInfo 里 516 字节的 `data`：12 字节头 + 63 条 `[u32 BE 参数 id][f32 BE 值]`。同名的 `Waves_XPst`（纯文本 XML，429 token）是**副本**，手改会被插件丢弃。AU 参数对外只读，所以只能走 L1 直写 f32。

详见 `docs/02-au-containers.md §8`。

### L3 的现实地位

不要把它当"退路"，它是**很多问题的唯一路**：轨道/ AUX / Stack / 路由这些对象图本项目没解，运行时工具（`logic_create_track`、`logic_duplicate_track`、`logic_set_track_routing`…）是现成的。

代价是：需要装 Logic、需要一次性建东西、慢（77 条 patch 手工导入要几十分钟）。

---

## 5. 附录索引

| 文件 | 讲什么 | 什么时候读 |
|---|---|---|
| `docs/01-cst-format.md` | `.patch`/`.cst` 块流、**双槽位字段**、5 个 UUID、名字长度限制、增删插件 | 要生成/修改通道条预设 |
| `docs/02-au-containers.md` | 5 种插件状态容器的字节布局 + 参数寻址 + L2 | 要写插件参数 |
| `docs/03-project-timeline.md` | `.logicx` 对象流、**拍号/调号/和弦**的定长记录与公式、模板槽位法 | 要改工程里的时间线对象 |
| `docs/04-runtime-and-templates.md` | 运行时工具能做什么、模板该怎么造、哪些必须人工 | 要建轨道/路由，或要决定"谁来做" |
| `docs/05-verification.md` | 验证清单、假成功的五种形态 | **每次写入前后** |
| `docs/06-failure-modes.md` | 已知失败模式与症状对照 | 出问题时 |
| `docs/07-track-creation.md` | 新建音频轨：9 块增量、全部计数器、Logic 实测验收 | 要建轨时 |

---

## 6. 明确的未知（不要假装知道）

| # | 未知 | 影响 | 绕开方式 |
|---|---|---|---|
| 1 | `.logicx` 的**轨道对象图**（`karT` 及其子对象的完整布局） | 纯离线不能建轨 / 改路由 / 改推子 | 运行时工具，或模板槽位 |
| 2 | **插入增长**（真插入一个定长记录） | 事件数受模板槽位数限制 | 换更大的模板 |
| 3 | **和弦音阶枚举**（19 个值） | 不能写"Lydian"这类音阶 | 只能用 Auto，或在 Logic 里设 |
| 4 | **iZotope 头 16 字节的第 1 个 u32** | 写入前无法确认该字段含义 | 原样保留（实测可行） |
| 5 | **Waves（WaveShell）** | ✅ `data` 记录表（id 存在记录里） | L1 直写 f32 |
| 6 | **删除** | 和弦记录删不掉（Logic 会保留记录） | 用 `No Chord` 事件代替 |
| 7 | 拍号/调号位置的 `+38400` 偏移含义 | 公式已验证，但为什么是这个常数不明 | 照公式用 |

---

## 7. 三十秒快速开始

```bash
# 0) 依赖：只要 Python 3（标准库），不需要 Logic
python3 logicfmt/chord.py        # 和弦模块自检（无需任何文件）
python3 logicfmt/meterkey.py     # 拍号/调号自检
python3 -m logicfmt.mixer        # 通道条 / Send 自检（39 条断言）

# 1) 从模板生成一条测试工程（改 8 个和弦）
python3 logicfmt/build_test_chord.py --dry-run

# 2) 从模板生成一个改拍号/调号的工程
python3 logicfmt/build_test_mk.py --dry-run
```

模板**已经随包分发**（`templates/logicx/` + `templates/patch/`），直接用即可。
只有两种情况需要自己造：**Logic 版本不同**，或**槽位不够**——规格见 `docs/04 §2.6`。

---

## 8. 给"未来的 AI"的一句话总结

> 你有两个可靠的东西：**模板**（Logic 写的，格式一定合法）和**差分**（改一处再比一次，就知道那一处在哪）。
> 你有一个不可靠的东西：**从零构造字节**。
> 所以永远走「拿模板 → 定位已有字段 → 等长改写 → 独立回读验证」这条路。走不通的时候，先问"能不能让用户用 Logic 做一个只差这一处的模板"——这通常比继续猜格式便宜一个数量级。

## 9. 当前能力矩阵（诚实版）

| 对象 | 读 | 写 | 备注 |
|---|---|---|---|
| 拍号变化（任意小节） | ✅ | ✅ | 位置 = ⌈(tick+38400)/256⌉ |
| 调号变化（任意小节） | ✅ | ✅ | code = (minor?0x10:0) \| (7+升降号) |
| 和弦轨区域 | ✅ | ✅ | 性质 = 相对根音的音程位图；音阶编码未解 |
| 通道条：推子/声像/静音/独奏/输出 | ✅ | ✅ | `logicfmt/mixer.py`，39 条断言 |
| Send 电平/目的地 | ✅ | ✅（改已有） | **新建 Send 未实现**，但增量已采齐（`PROJECTDATA-FINDINGS` §63）：它 = 新建 Aux（`ivnE:469`+`karT:58`+`qeSM:309`+`qSvE:16`）+ 每条通道条 +136 插 4 字节 + 一条 `UCuA:76` 记录 + gnoS 登记；下一轮用 donor 法实现 |
| 删除音频轨 | ✅ 读 | ✅ 任意位置（Logic 实测通过：末条 `_RM-TEST`、中间 `_RMID2`） | `project.remove_audio_track()`；退役空行 `+81=0x08`、配对 `ivnE+455`、`karT` 家族 `0x40000` |
| 新建音频轨 | ✅ | ✅ | `project.add_audio_track()`；**Logic 实测通过**（`_Y5`/`_Y6`）；通道条身份须同时写 `+189` 与 `ivnE+455` |
| 从零建工程 `.logicx` | ✅ | ✅ 1-4 轨 | `project.create_project()`；5 文件骨架 + 改名（`qeSM/MSeq`）+ 定速；Logic 实测 `_NEW-T3`（3 轨/96）、`_NEW-T4E`（4 轨/110）；**串行加轨会丢轨**，>4 轨拒绝，见 `docs/07` §12 |
| Aux / Stack 主轨 | `ivnE(467)`，`+155` = `05` Aux / `10` Summing / `14` Folder | ✅ 读、改类型、改名；**✅ 离线新建 Summing**（`add_stack(kind="summing")`，三处 UUID 身份链已通）；**✅ 离线新建 Folder**（`add_stack(kind="folder")`，Logic 实测 `is_stack: true` + `kind: "reduced"` + `child_count: 9`，与手工参考逐字段相同）；运行时 `logic_duplicate_track()` ✅ |
| Stack 成员关系 | Folder = 轨条 `+110`；Summing = 输出路由 | ✅ 读写 |
| 真插入（文件变长） | `insert_in_payload` + `seal` | ✅ 已验证（往 qSvE 插记录） |
| Tempo / 自动化 / MIDI 音符 | ✅ 可写 | ✅ | MIDI 音符在区域的 `qSvE` 容器里 |
| MIDI 区域新建 | ✅ | ✅ | `midi.add_region()`：donor 造区域对象 + `qSvE` 注册表追加一对 40B 记录；Logic 实测 `_MR14` 读出 5 个区域 |
| MIDI 区域移动 | ✅ | ✅ | `midi.move_region()`：只改两处（对象 `+247` 的 tick×256、登记表记录 `+5` 的 `(tick+34560)/256`）；Logic 实测 `_MV-TEST` 读出 1/5/9/15/17 小节 |
| MIDI 区域复制 | ✅ | ✅ | `midi.copy_region()`：走 donor 造对象 + 把音符容器整段换成源区域的；Logic 实测 `_CPY2` 读到 `COPY-A`，3 个音符 C3/E3/G3 vel 80 与源区域一致 |
| Marker 新增 | 🟡 交付物已完整（三件套+qSxT+登记，见 §64），卡点=id/登记行分配 | — | 干净增量已拿到（`qSvE` 48B 记录 + 2×`qSxT` + gnoS 80B 登记行）；gnoS 那 80 字节已证伪（`S1` 去掉 gnoS 增量仍读出 1 个）；真卡点是 `qSvE` 对象 id 与保存后编号一致，缺一条 Logic 就静默丢弃；6 组对照见 `PROJECTDATA-FINDINGS` §50.8 |
| 插件参数 | ✅ | ✅ | 见 `docs/02`，98% 状态数 |
| `PvWr` / `FrAr` 未解容器 | ⚠️ 读得出 magic | ❌ 拒绝写（不盲探） | Polyverse / Slate；本机无样本、Logic 浏览器不列 Polyverse，见 `docs/02` §6 |
| 任意插件参数（通用兜底） | ✅ | ✅ | L2 ClassInfo；Waves 走 `data` 记录表 |

> 交接与未完成项的下一步：见 [HANDOFF.md](HANDOFF.md) 与 [docs/08-roadmap.md](docs/08-roadmap.md)。
