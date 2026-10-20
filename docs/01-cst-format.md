# 附录 01 —— `.patch` / `.cst` 通道条预设格式

> 验证强度：**实测**。结论来自 95 个实际 patch、77 条自动生成并装入 Logic 库的文件、以及多轮 Logic 打开确认。

---

## 1. `.patch` 是一个 bundle

```
Xxx.patch/
├── #Root.cst          ← 主体，二进制块流
└── data.plist         ← 元数据（名字、UUID、图标、发送等）
```

`.cst` 是同样的字节流，用于另一种存放位置（Channel Strip Setting）。**两者可以互换看待**。

### `data.plist` 关键字段

| 键 | 作用 |
|---|---|
| `VersionPatches` | 版本号，实测 `40014` |
| `channels[0].Channel_name` | **UI 显示名来源之一** |
| `channels[0].UUID` | 该通道条的 UUID（与 `.cst` 里 4 个 UUID 不同，见 §4） |
| `channels[0].Channel_instID` | 通道类型标识（实测 236 / 240） |
| `channels[0].Channel_sends` | 发送 |
| `channels[0].SequColorIndex` / `Track_icon` | 颜色 / 图标 |

> **UI 名字来自 `.patch` 的文件夹名 + `Channel_name`，不是 `.cst` 里的名字**（见 §5）。所以改文件名的同时要改 `Channel_name`。

---

## 2. `#Root.cst` 是块流

```
OCuA 头块
UCuA 插件块 × N
OCuA 尾块
```

### 每个块的通用布局

| 偏移 | 类型 | 含义 |
|---|---|---|
| `+0` | 4 字节 | tag：`OCuA` 或 `UCuA` |
| `+4` … `+27` | — | 24 字节头（含版本、ID 等；不必理解） |
| **`+28`** | **u32 LE** | **载荷长度** |
| `+36` | — | 载荷起点 |
| — | — | **块总长 = 载荷长度 + 36** |

**每个块自带长度**，所以：

- 删除一个插件 = 直接删掉它的整块字节，**不需要修正任何全局长度**（实测：把 `D Kick Bus` 拆成 11 块再拼回，字节完全一致，16 332 字节）。
- 插入一个插件 = 把已经合法的 `UCuA` 块（从别的 patch 里整块搬来）拼进去，并**改它的槽位字段**（§3）。

### 头块 / 尾块的长度（实测）

| | 载荷 | 总长 |
|---|---|---|
| 音频轨头块 | 285 | **321** |
| Aux 轨头块 | 309 | 345 |
| 尾块 | 14 | **50** |

音频轨有 **15 个插入槽**；库内实际用到的槽是 2–9（`.cst` 内部编号，见 §3）。

---

## 3. ★ 槽位存在**两个**字段（本项目最大的坑）

同一块 `UCuA` 里，位置被写了**两遍**：

| 偏移 | 类型 | 编码 | Logic 是否使用 |
|---|---|---|---|
| `+12` | u64 **大端** | `(insert + 1) × 256` | ❌ **忽略**，只是标签 |
| **`+42`**（= 载荷 `+6`） | **u32 小端** | **insert 本身（0 基）** | ✅ **真正决定位置** |

**显示格子号 = insert + 1。**

只改 `+12` 的后果：插件**完全不动**，一切看起来都"成功"了。这正是当初绕了四轮才找到的根因。

```python
# 把某插件放到第 i 个位置（i 从 0 开始）
struct.pack_into(">Q", body, 12, (i + 2) * 256)   # 标签，跟显示格子一致
struct.pack_into("<I", body, 42, i)               # 真实位置
```

> 为什么 `+12` 是 `(i+2)` 而 `+42` 是 `i`：`+12` 的计数从"头块"开始算，`+42` 的计数从"第一个插件槽"开始算，相差 2。

### 决定性验证（如果将来有人怀疑这条）

拿两个只含不同插件的 patch 拼一个：把模板 A 的 Pro-DS（原位置 9）和模板 B 的 Maag EQ4（原位置 8）装进同一个 `.cst`，**只改 `+12`** → Logic 里 Maag 显第 9 格、Pro-DS 显第 10 格（= 各自**原**位置 + 1）。改两个字段后位置才正确。

---

## 4. 一个 patch 里有 **5 个** UUID

| # | 位置 | 说明 |
|---|---|---|
| 1 | 名字块 `[212:228]` | 见 §5 |
| 2 | 条带块 #1 末尾 16 字节 | 大小 4211（模板实测） |
| 3 | 条带块 #2 末尾 16 字节 | 大小 1845 |
| 4 | 条带块 #3 末尾 16 字节 | 大小 1464 |
| 5 | `data.plist` 的 `UUID` | |

**任何一个留旧值，Logic 就把这个 patch 认成"模板"**（症状：加载 `X.patch` 却显示成模板的名字）。

生成新 patch 时要换全部 5 个，且互不相同。识别 UUID 的判据同 `CORE.md §2` 铁律 3。

> 那三个 4211 / 1845 / 1464 字节的块是 **NSKeyedArchiver plist**，去掉末尾 16 字节 UUID 就能用 `plistlib` 解析：
> - 4211 → **Smart Controls 参数表**（`kSavedValueFloatKey`、`parameterIndex_1`、`slot`…）
> - 1845 → **Smart Control 布局与标签**（`Smart Knob 1…12`、`contentTagLayoutName`）
> - 1464 → **Track / Layer 设置**（`color`、`bypass`、`transpose`、`highNote`…）
>
> 这些目前**只读不写**。

---

## 5. 名字块：228 字节，字段只有 17 字节

| 偏移 | 内容 |
|---|---|
| `+53` | 名字（ASCII） |
| `+212:228` | UUID |

**可用字符 ≤ 10 字节。** 超长会截断或破坏结构。

解决办法（实测可行）：**`.cst` 里放一个 10 字符以内的 ASCII 缩写，完整名字放 `.patch` 文件夹名 + `Channel_name`**。UI 显示的是后者，两者不必一致。

> 例：77 条 patch 的内部名是 `DKickIn`、`VoxLead` 这类缩写，文件夹名才是 `D Kick In`、`Vox Lead`。

---

## 6. 插件身份怎么读

块内载荷是一个 plist，里面有：

```xml
<key>manufacturer</key><integer>1634758764</integer>   <!-- 4CC 大端打包后的整数 -->
<key>type</key><integer>…</integer>
<key>subtype</key><integer>…</integer>
<key>name</key><string>FabFilter Pro-Q 4</string>
```

把 `<integer>` 按**大端**打包成 4 字节就得到 4CC：`FQ4p`（Pro-Q 4）、`FC3p`（Pro-C 3）、`SPTP`（SPL Transient Designer）、`bxmt`（bx_meter）…

`subtype` 就是参数表的键（`docs/02` 用它查参数）。

> 注意：Logic **自带**插件（Gain、Channel EQ…）的块里没有这个 plist，要靠块内出现 `b"Gain"` 这类字面量识别。

---

## 7. 插件状态 blob 在哪、怎么写回

状态在 plist 里，**base64**，包在 `<data>` 里。键名**按容器 magic 决定**，不能按固定顺序试：

```python
STATE_KEYS = ("FabFilterPluginState", "jucePluginState", "data")
# 按 blob 的 magic 选键：
#   FFBS…… → FabFilterPluginState
#   VC2!…… → jucePluginState      ← JUCE 块**同时**有 data 和 jucePluginState
#   Pmtr…… → data
```

### 写回必须走"占位符填充"，不能整段替换

`<data>` 里的 base64 被**折行**，中间有换行和空格。直接 `re.sub` 换掉整段会改变长度 → 整个 `.cst` 的长度字段全错 → Logic 拒绝解析。

正确做法（`au_batch._write_cst_state`）：

```
1. 解出原来的 base64 字符串（含折行）
2. 记录其中所有"非空白字符"的位置
3. 新 base64 必须与原串**字符数完全相同**
4. 把新串逐字符填进那些非空白位置，空白原样保留
```

因为 base64 是定长的，**改 4 个浮点字节 → base64 字符数不变 → 文件大小和所有偏移都不动**。这就是"等长字节手术"。

---

## 8. 增删插件 / 复制 patch

### 删除

删掉该 `UCuA` 块的整段字节即可（每块自带长度）。

### 插入

从另一个 patch 里整块搬一个 `UCuA` 过来，然后**改写它的两个槽位字段**（§3），再按插入位置依次给后面的块重新编号。

### 复制成新 patch（实测流程）

```
1. 复制整个 .patch 目录
2. 改 .cst 里 4 个 UUID + data.plist 里 1 个 UUID（§4）
3. 改 .cst 名字块的缩写名（§5）+ data.plist 的 Channel_name
4. 改文件夹名
5. 重新解析确认：插件数、槽位、名字、UUID 两两不同
```

> 装库路径：`~/Music/Audio Music Apps/Patches/<类型>/<名字>.patch`
> 类型目录：`Audio/`、`Aux/`、`Instrument/`、`Mix Bus/`、`Input/`、`Bus/`、`Output/`

---

## 9. 通道类型的边界（实测结论）

- **Patch / CST 不保存 I/O 路由。**
- **Bus 通道条**属于历史遗留，只能从 Environment 建，功能已被 Aux 取代；存"总线处理"时 Logic 一律归到 Aux。
- **Output 通道条**同理，没有实用的保存路径。

所以**通道类型矩阵只有三种**：Audio / Aux / Software Instrument。Output 交给工程模板。

另外：**CSS（Channel Strip Setting）无法携带 Send 信息**，需要 Send 就存成 Patch。

---

## 10. 自检清单（写完必跑）

```python
# 1) 块流自洽：从 0 开始按 长度+36 走，必须正好走到文件末尾
# 2) 插件数与预期一致，每个块的 (manufacturer,type,subtype) 都能解析
# 3) 每个插件块的 +12 == (显示槽+1)*256 且 +42 == 显示槽-1
# 4) 5 个 UUID 两两不同，且都不等于模板的值
# 5) 名字块 ≤ 10 字节，data.plist 的 Channel_name 是想要的完整名
# 6) 文件能被自己的解析器重新读出全部参数（读回值 == 写入值）
```

一条真实教训：**只做 1–5 不够**。有一次 6 通过、1–5 通过，但文件里两个 UUID 相同，Logic 仍然把 patch 当成模板。**UUID 唯一性是硬条件。**
