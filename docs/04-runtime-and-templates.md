# 附录 04 —— 模板与运行时：什么时候谁来做

> 这份资料的核心不是"能解析多少字节"，而是**在什么条件下用哪条路**。本附录给的是分工规则。

---

## 1. 为什么必须有模板

纯离线生成一个 `.logicx` 需要手写完整的对象图。这件事**本项目没有做成**，而且不建议尝试，原因有三：

1. **没有错误反馈。** 格式错了 Logic 不报错，只是打不开或静默丢对象。你无法迭代。
2. **对象图很大**（`ProjectData` 里 450 个 `OCuA`、722 个 `UCuA`、430 个 bplist）。
3. **Logic 自己会补对象。** 新工程再保存会长出约 10 KB —— 说明"缺对象"不是错误，是正常状态。

**模板法把"构造"降级为"改写"**：格式合法性由 Logic 保证，我们只动几个已知字段。这是整套资料里最重要的一条工程决策。

---

## 2. 模板已经随包分发

**你不需要自己造模板。** 本套资料自带一套可直接用的：

```
templates/logicx/_BASE.logicx       1 条音频轨，空白基线
templates/logicx/_METERKEY.logicx   4 个拍号槽位 + 3 个调号槽位
templates/logicx/_CHORD.logicx      15 个和弦槽位
templates/logicx/_SKELETON.logicx   轨道 / AUX / Stack / 自动化的运行时底稿
templates/patch/_TEMPLATE-A.patch   11 个"修正/动态"类插件
templates/patch/_TEMPLATE-B.patch   11 个"染色"类插件
```

明细见 `templates/MANIFEST.md`。**只有在下面的 §2.6 两种情况下**才需要自己造。

### 2.0 造模板的通用规则（只在需要重建时看）

用 **Logic 新建工程 → 存成 package**，**不要改已有文件**（避免污染）。

### 2.1 `_BASE.logicx` —— 最小基线（已分发，通常无需重建）

- 1 条音频轨（新建工程自带即可）
- 不改拍号、不改调号、不加 Marker、不加 BPM 变化、不加音频文件

**用途**：一切"从零开始"的底稿；差分实验的对照组。

### 2.2 `_SKELETON.logicx` —— 结构骨架（已分发）

一次做好，包含所有**以后可能要用的对象形状**（多给多删——删除安全，创建危险）：

| 对象 | 建议数量 | 命名建议 |
|---|---|---|
| 音频轨 | 5 | `A1`…`A5`（无空格） |
| Summing Stack | 1（含 2 条子轨） | 主轨 `SUM1` |
| Folder Stack | 1（含 2 条子轨，保持折叠） | 主轨 `FLD1` |
| Aux | 1–2 | `AUX1`、`AUX2` |
| 软件乐器轨 | 1 | — |

再加：

- BPM 变化：第 4 小节 150、第 8 小节 180
- 拍号变化：第 2 小节 3/4
- 调号变化：第 2 小节 D major（**不勾变调**）
- 音量自动化：bar 1 / 2 / 3 各一个节点
- 2 个 Marker
- 1 段 MIDI region（用来观察 MIDI 对象）

**用途**：所有轨道/路由/Stack/自动化实验的底稿。

### 2.3 `_CHORD.logicx` —— 和弦槽位（已分发，15 个）

在 chord 轨上放 **N 个已知和弦**（N 尽量多，比如 32 个），每小节一个。

**用途**：N 就是你的和弦槽位数。槽位不够时，让用户再存一个更大的。

### 2.4 `_METERKEY.logicx` —— 拍号/调号槽位（已分发，4 + 3 个）

- 拍号变化：8 条（例如第 2/3/4/5/6/7/8/9 小节依次 3/4、5/4、6/8、7/8、2/2、6/4、4/4、3/2）
- 调号变化：8 条（例如第 2/3/4/5/6/7/8/9 小节依次 D、Eb、F#m、A m、Bb、C# m、G、E）

**用途**：只改数值、等长覆盖，**零结构风险**。槽位不够就往里再加几条（见 §2.6）。

> 注意：**拍号会从设定的小节起向后持续生效**，所以这些"每小节一个"的设置本身会互相覆盖。做槽位模板时按"依次变化"来设没问题（每小节都设一次），但**要知道这不是"只改一小节"**。

### 2.6 只有这两种情况才需要自己造

| 情况 | 为什么 | 怎么办 |
|---|---|---|
| **Logic 版本不同** | 记录布局、块头版本字节都可能变 | 按本节规格造一份，然后跑 `logicfmt` 自检 + `templates/MANIFEST.md §5` 的槽位统计 |
| **槽位不够** | 本项目**未实现"真插入"**，事件数受模板槽位数限制 | 在 Logic 里往 `_METERKEY` / `_CHORD` 上再加几条事件，另存为新文件，用它的路径 |

除此之外没有隐藏原因——**一个人造一次，所有人共用**。这正是这套资料的设计意图。

### 2.5 `_TEMPLATE-A.patch` / `_TEMPLATE-B.patch` —— 插件槽位（已分发）

两个 patch，每个都**装一次所有需要的插件**（默认参数即可），中间不留空槽。

`_TEMPLATE-A`（修正/动态类）11 个插件、`_TEMPLATE-B`（染色类）11 个插件就够了——插件身份看 `subtype`，**槽位顺序无所谓**（组装时统一重写槽位字段）。

**用途**：任何新 patch = 从 A/B 里挑插件块拼装 + 改参数。

---

## 3. 什么必须人工（不要试图自动）

| 动作 | 为什么必须人工 | 替代 |
|---|---|---|
| 造模板 | 需要 Logic 的对象图 | — |
| 建轨道 / AUX / Stack | `karT` 对象图未解 | 运行时工具 |
| 改插件**加载**（不是参数） | 需要扫描插件目录 + 写插入块 | 从模板拼块 |
| 造新的 Patch 类型目录 | Logic 的 UI 行为 | — |
| 首次验证一个写入 | 需要人眼看 Logic 是否正常 | — |

**判据**：如果一件事"手动只要 1–2 分钟、而自动要逆向两小时"，就让人做。

---

## 4. 运行时路线能做什么（已实测的工具面）

> 这一节描述的是 **Logician MCP**（一个通过 Mackie Control 协议 + macOS Accessibility 控制 Logic 的桥）。它不是本资料的一部分，但**在离线路线走不通时是唯一出路**。没有它也可以用 Logic 的 Key Commands + AppleScript 达到类似效果，只是更笨。

### 能做

| 能力 | 典型工具 | 备注 |
|---|---|---|
| 建轨（音频 / 软件乐器） | `logic_create_track` | 一次一条 |
| 复制轨（带内容与设置） | `logic_duplicate_track` | Folder / Summing 的**主轨**不支持 |
| 删轨 / 改轨名 | `logic_delete_track` / `logic_rename_track` | |
| 路由（output / input / group） | `logic_set_track_routing` | 把 output 设成 `Bus N` 时 Logic **会自动建 aux** |
| 推子 / pan / mute / solo | `logic_set_track_mix` | |
| 插件参数（任意第三方） | `logic_set_plugin_parameter` | 走控制面，绕过容器格式 |
| 读插件参数 | `logic_list_plugin_parameters` | |
| 加 / 删插件 | `logic_add_plugin` / `logic_remove_plugin` | 走插件浏览器，鼠标无关 |
| 自动化录制 | `logic_record_automation` | 实时播放一遍 |
| Tempo 事件（含中途变化） | `logic_tempo_events` | 可 create / set / delete |
| Marker | `logic_markers` | 可 create / goto / delete，**不能改名** |
| 读拍号 / 读调号 | `logic_list_signatures` / `logic_get_transport` | **只能读** |
| 存工程 / 开关工程 | `logic_save_project` / `logic_reset_to` | |

### 不能做（截至本资料成稿）

| 不能 | 说明 |
|---|---|
| 写拍号 | 只有读工具 → **用附录 03 的离线写入** |
| 写调号 | 同上 |
| 写和弦轨 | 没有工具 → **用附录 03** |
| 建 Track Stack | 无工具；可用 `logic_learn_key_command` 触发 Logic 自己的"建 stack"命令，但语义需要实测 |
| 加载 `.patch` 到轨道 | **全库没有这个工具** → 离线替换通道条字节是唯一出路 |
| 改 Marker 名字 | Marker List 的名字单元格不可写 |
| 批量建 77 条轨 | 只有单条工具，吞吐未测 |

---

## 5. 分工决策表

| 你要做的事 | 走哪条 |
|---|---|
| 生成/修改通道条 preset（插件链 + 参数） | **离线**（`docs/01` + `docs/02`） |
| 写插件参数（任意第三方） | 先 `docs/02` L1 → 不行走 L2 → 再不行走运行时 |
| 改拍号 / 调号 / 和弦 | **离线**（`docs/03`，模板槽位） |
| 建轨道 / AUX / Stack / 路由 / 推子 | **运行时** |
| 自动化曲线 | 模板里已有 lane → 离线；要新建 → 运行时 |
| Tempo 变化 | 运行时（简单）；离线结构未解 |
| Marker | 运行时 |
| 从零造整个工程 | **不要做**。模板 + 运行时组合 |

---

## 6. 模板的版本管理

模板是**唯一的格式真相来源**，所以要像代码一样管：

```
templates/
├── MANIFEST.md          # 每个模板：谁做的、Logic 版本、包含什么、槽位数
├── _BASE.logicx
├── _SKELETON.logicx
├── _CHORD-SLOTS.logicx
├── _METER-KEY-SLOTS.logicx
├── _TEMPLATE-A.patch
└── _TEMPLATE-B.patch
```

`MANIFEST.md` 至少记：

- **Logic 版本 + build**（本次是 12.4 / 6707）
- 每个模板的 `ProjectData` 大小 + md5 前 8 位
- 各槽位数（用脚本量出来，别手记）
- 做模板时**故意没改**的东西（例如"没改拍号"）

> 不同 Logic 版本之间这些偏移**可能变**。换版本第一件事是拿模板重跑一遍自检。

---

## 7. 一个反例：Bus / Output patch 不存在

曾经想给 Bus 和 Output 也做 patch，结论是**做不到，而且不该做**：

- **Bus 通道条**是历史遗留功能，只能从 Environment 建，功能已被 Aux 取代；存"总线处理"时 Logic 一律归到 Aux。
- **Output 通道条**同理。

所以通道类型矩阵只有三种：**Audio / Aux / Software Instrument**。Output 交给工程模板。

> 这类"设计上就不存在"的结论要写进文档，否则后来的人会反复撞同一堵墙。
