# 附录 02 —— 第三方插件状态容器与参数寻址

> 验证强度：**实测**。库内 747 个状态 blob 中 **732 个（98.0%）** 已解；238 个参数名 + 值经 Logic 回读确认。

---

## 1. 先按 magic 分类

一个状态 blob 通常就是 `.cst` 里 `<data>` 解 base64 后的字节。**看开头 4 字节就能决定用哪个 codec**：

| magic | 容器 | 厂家家族 | 库内计数 |
|---|---|---|---|
| `FFBS` | **FFBS** | FabFilter（Pro-Q / Pro-C / Pro-L / Timeless / Volcano…） | 304 |
| `Pmtr` | **Pmtr** | Plugin Alliance / Brainworx / SPL / elysia / Maag | 331 |
| `VC2!` | **VC2!** | 一切 JUCE 写的插件（Valhalla、iZotope ERA、Streamliner…） | 66 |
| *（无 magic）* | **flags / sparse** | FabFilter 的 Pro-DS / Pro-MB | 46 |
| `ea db 3d 00` / `83 fb 80 00` / … | **iZotope 私有** | Nectar / Neutron / Ozone / RX | 9 |
| 其它 | 未知 | Polyverse `PvWr`(44B)、Slate `FrAr`(2522B)、WaveShell | 6 |

> `flags` 容器**没有 magic**，靠 plist 里的 `FabFilterFlags = 1` 认出来。这是唯一的无 magic 容器。

---

## 2. FFBS —— FabFilter

```
+0    "FFBS"
+4    u32 LE  版本
+8    u32 LE  参数个数
+12   float32 LE × 参数个数
```

- **小端**，扁平 `float32` 数组，**每参数一个值，顺序 = 插件自己的 AU 参数顺序**。
- 没有标签、没有默认值冗余。所以**必须**靠外部参数表（`param-maps/`）把下标映射到名字。

| 插件 | subtype | 值个数 | 结构 |
|---|---|---|---|
| Pro-Q 4 | `FQ4p` | **600** | 24 段 × 23 字段 + 48 全局；第 n 段基址 = `23 × (n−1)` |
| Pro-C 3 | `FC3p` | **100** | `f31` = Stereo Link LFE；`f86…f99` = 全局（Mix、Input/Output Level、Bypass、Oversampling…） |
| Timeless 3 | `F3Ts` | 1007 | — |
| Volcano 3 | `FV3l` | 896 | — |

### Pro-Q 4 单段的字段偏移（相对该段基址）

| + | 字段 | 编码 |
|---|---|---|
| 0 | Used | 0/1（**关闭的段也必须显式写 0**） |
| 2 | Frequency | **`log2(Hz)`**（默认 `9.96578` = 1000 Hz；范围 3.32193=10Hz … 14.8727=30kHz） |
| 3 | Gain | dB，−30 … 30 |
| 4 | Q | **归一化 0…1**，见下 |
| 5 | Shape | 枚举，见下 |
| 6 | Slope | `n × 6 dB/oct`，0…10（10 = Brickwall） |

**Q 的归一化**（`QMIN=0.025, QMAX=40`）：

```python
q_norm = lambda q: log(q / 0.025) / log(40 / 0.025)
# Q 1.0 → 0.5      Q 1.2 → 0.5247      Q 1.5 → 0.5550
```

**Shape 枚举**（用 `tools/au_dump --string 5 <v>` 实测）：

```
0 Bell   1 Low Shelf   2 Low Cut   3 High Shelf   4 High Cut
5 Notch  6 Band Pass   7 Tilt Shelf  8 Flat Tilt  9 All Pass
```

**Slope 枚举**：`0 = 0 dB/oct`，`1 = 6`，`2 = 12`，`3 = 18`，`4 = 24`，`5 = 30`，`6 = 36`，`8 = 72`，`10 = Brickwall`。
→ 写 12 dB/oct 就是 `2`，24 dB/oct 就是 `4`。

### Pro-C 3 的 Mix

```
Mix = (1 + f88) × 100%      # AU 范围 0…2
```

---

## 3. Pmtr —— Plugin Alliance / Brainworx / SPL / elysia / Maag

```
+0    "Pmtr"
+4    记录区（tag 记录表）
```

- **大端**。
- 记录区是 **AU 参数列表按顺序排列**，每条记录对应一个参数。

### 记录格式

| 字段 | 含义 |
|---|---|
| `tag` | 4 字节**标签**（如 `Attk`、`Thrs`）——**是标签不是地址，千万别拿它当偏移用** |
| `float[0]` | **归一化值**：`(au − min) / (max − min)` |
| `float[1..3]` | **该参数的默认值**（同样的归一化），连写三遍 ← 这是对齐校验，用来确认记录与参数表对上了 |

- **只读参数（电平表）没有记录**。所以「记录数 ≠ 参数数」是正常的。
  - 例：SPL Transient Designer `SPTP` 有 14 个参数但只有 12 条记录；`Signal` 是纯显示。
- 对齐校验：`float[1] == float[2] == float[3]`，容差 `1e-5`。对不上就是参数表选错了。

### 寻址

**按偏移**，不按 tag：

```python
# 在记录区里，第 k 条的 float[0] 在 rec_start + 4 + k*16
# 改值 = 写 4 字节大端 float
```

也可以按名字（`param-maps/<mfr>_<subtype>.txt` 提供 name↔index），再换算成偏移。

---

## 4. flags / sparse —— FabFilter 的"只存非默认值"容器

```
+0    u32 BE  0
+4    u32 BE  0
+8    u32 BE  count            ← 等于该插件的 AU 参数总数
+12   count × ( u32 BE index | float32 BE value )
```

- **大端**。
- 只存**非默认值**：没出现的参数就是默认值。
- 值用**插件自己的单位**（不像 Pmtr 那样归一化）。
- 例：Pro-MB `FPMb` → count = 151；Pro-DS `FPSr` → count = 20。

> 也就是说同一个 FabFilter 厂家有两种容器：多数插件用 `FFBS`（全量数组），Pro-DS / Pro-MB 用 `flags`（稀疏）。**不能按厂家假设，必须按 magic 判断。**

---

## 5. VC2! —— JUCE 系

```
+0    "VC2!"
+4    u32 LE  body_length
+8    body（**明文 XML**）
+8+body_length  一个 \0
```

总长 = `body_length + 9`。

**身份 = XML 根元素名**（多数 JUCE 插件没有 12 字节 FourCC 三元组）：

| 根元素 | 插件 |
|---|---|
| `ValhallaRoom` / `ValhallaSupermassive` / … | Valhalla 系列 |
| `state` | Streamliner、ADPTRSculptPreset |
| `ERASave` | iZotope ERA 6 |
| `SwivelAudioSpread_pluginstate` | Swivel Audio（库内 52 个） |

### 三种参数形态

```xml
<!-- A: 参数是根元素属性 -->
<ValhallaRoom decay="0.5" lateSize="0.3" .../>

<!-- B: 参数是子元素 -->
<state><param id="1" name="Output" value="0.75"/> …</state>

<!-- C: Swivel 的分组形态 -->
<SwivelAudioSpread_pluginstate><PluginAudioParameter Spc0="3" Spc1="0" value="…"/> …</…>
```

- **B 形态要按 `name` 寻址**，不能按 `id`——`<param id="1" name="Output">` 里 `id` 只是序号。
- **C 形态**用 `(Spc0, Spc1)` 二元组聚合成一个具名参数。

### 变长写入（唯一需要处理长度的地方）

XML 是**变长**的，改一个值可能改变 `body_length`。此时不能就地替换，必须：

```
1. 改 XML body
2. 同步改 +4 的 body_length
3. 重新拼整个 blob
4. 用"占位符填充"把所有字节塞回原来的 base64 槽（必要时缩短其它参数来腾出字节，
   或调整父级长度字段）
```

`logicfmt` 走的是**缩短策略**：`xml_shrink_to(raw, row, value, target_len)` 把新值塞进原长度；塞不下时 `_xml_absorb` 去邻近的空白/填充点吸收差额。

> ⚠️ 曾因为 pad 点插错位置（插在自闭合标签的 `>` 前面，得到 `… /   >`）产生了**非法 XML**，而解析器返回 0 个参数，错误表现为 `KeyError: no XML parameter 'lateSize' (have: )`——**看起来像"找不到参数"，其实是"整个 XML 坏了"**。改 XML 后必须重新 `parse` 一次确认根元素还在。

---

## 6. iZotope 私有容器

```
+0    u32   ← **含义未知**（可能是校验/类型码）。写法：原样保留
+4    u32   版本，实测 = 1
+8    u32   = len(data) − 12
+12   u32   解压后长度
+16   zlib 流（78 9c …）→ **JSON**
```

解压后是**自描述的 JSON 类型化树**：

```json
{"Context State": {"Type": "Dictionary", "Value": { … }}}
```

写入步骤：

```
1. 读 +0 原样保留
2. zlib 解压 → JSON 解析 → 改值 → 重新序列化 → zlib 压缩
3. 重算 +8 = len(压缩后) − 12
4. 重算 +12 = len(解压后)
5. 拼回去
```

实测：`NCTG`（Nectar 4 Gate）830 B → 9415 B JSON；`ZNU5` 1951 B → 31269 B。

> 这条路**未做 Logic 回读验证**。按 `CORE.md §4`，这类情况应先用 L2 或 L3 兜底。

---

## 7. 参数表从哪来（`param-maps/`）

`tools/au_dump.c` 用 AudioToolbox 直接问插件，生成 `<厂家>_<subtype>.txt`：

```
# FabFilter Pro-Q 4  FQ4p
0   0   Band 1 Used        0.000000  1.000000  0.000000
1   1   Band 1 Frequency   3.321930 14.872700  9.965780
...
```

列为 `index  au_id  name  min  max  default`。

```bash
clang -O2 -o tools/au_dump tools/au_dump.c -framework AudioToolbox -framework CoreFoundation
./au_dump aufx FQ4p FabF --values          # 列参数
./au_dump aufx FQ4p FabF --string 5 2      # 问插件：shape=2 显示成什么
```

### ★ 标定规则：**别用 MCU 的 LCD 当标准**

LCD 会四舍五入。曾经有一个参数插件说 `0.466667`（显示 `-1.00 dB`），LCD 显示 `-0.90`。差的就是那 0.1。
**要标定编码，问插件自己**（`--string`），不要问 Logic 的界面。

### AU 的 FourCC 从哪读

不要猜。读每个 bundle 的 `Contents/Info.plist` 里的 `AudioComponents`：

```
<key>AudioComponents</key><array><dict>
  <key>manufacturer</key><string>FabF</string>
  <key>subtype</key><string>FPMb</string>       ← Pro-MB 是 FPMb，不是 FMBp
  ...
```

`tools/dump_all_maps.py` 就是遍历所有 `.component` 干这件事的（1126 个文件，约 17 MiB；建议只提交 `INDEX.md`，其余用脚本重生成）。

---

## 8. L2 —— 让插件自己写状态（**已验证可用**）

这是整个 kit 最重要的一条兜底：**不需要知道任何容器格式**，让插件实例自己把状态吐出来。

```python
import classinfo
ci = classinfo.class_info("aufx", "SPTP", "SPL1", param=0, value=0.75)
ci["blob_key"], len(ci["blob"])      # ('data', 264)
classinfo.write_blob_into_cst(cst_bytes, insert_index, ci["blob"])
```

命令行等价物：

```bash
logicfmt/classinfo.py probe aufx SPTP SPL1            # 读它吐出的所有键
logicfmt/classinfo.py blob  aufx SPTP SPL1 -o s.bin   # 只取状态本体
logicfmt/classinfo.py set   aufx FQ4p FabF 2 8.45     # 实例化→设参数→重读
logicfmt/classinfo.py load  aufx RS1M ksWV 7          # 加载第 7 个出厂预设
logicfmt/classinfo.py selftest                        # 11 项自检
```

底层三步（`tools/au_classinfo.c`）：

```c
AudioComponentFindNext(NULL, &desc);
AudioComponentInstanceNew(comp, &inst);          // -3000 时改离进程实例化
AudioUnitInitialize(inst);
AudioUnitSetParameter(inst, paramID, kAudioUnitScope_Global, 0, value, 0);
AudioUnitGetProperty(inst, kAudioUnitProperty_ClassInfo,
                     kAudioUnitScope_Global, 0, &plist, &size);
```

### 8.1 实测覆盖（`classinfo.py selftest`：11 passed / 0 failed）

| 插件 | type/sub/man | ClassInfo 键 | 状态大小 | 家族 | 写入 |
|---|---|---|---|---|---|
| FabFilter Pro-Q 4 | `aumf FQ4p FabF` | `FabFilterPluginState` | 2738 | FFBS | ✅ |
| SPL Transient Designer | `aufx SPTP SPL1` | `data` | 264 | Pmtr | ✅ |
| ValhallaDelay | `aufx dLay oDin` | `jucePluginState` | 854 | VC2! | ✅ |
| accusonus ERA 6 Noise Remover | `aufx NrP6 Accu` | `jucePluginState` | 2417 | VC2! | ✅ 读 / ⚠️ 见 8.4 |
| iZotope Nectar 4 Gate | `aufx NCTG iZtp` | `data` | 830 | 头+zlib | ✅ |
| Swivel Audio Spread | `aufx SPRD SWAU` | `jucePluginState` | — | VC2! | ✅ |
| ADPTR Sculpt | `aufx SCPT Adpt` | `jucePluginState` | — | VC2! | ✅ |
| Knif Audio Soma / SPL TwinTube | `Soma` / `SPTT` | `data` | — | Pmtr | ✅ |
| Waves Abbey Road RS124 | `aufx RS1M ksWV` | `data` | 516 | — | ✅ `waves.py`：`[u32 id][f32 值]` 记录表 |

键名本身也有用：`ClassInfo` 字典里除状态外还有 `subtype` / `manufacturer` / `type` / `version` / `name` / `element-name`。

### 8.2 两条被 L2 纠正过来的旧结论

**① Plugin Alliance 的 L2 输出与 Logic 存储的是同一串字节。**
拿 `SPTP` 与库里真实 `.patch` 的 264 字节块逐字节比对：**0 处差异**。所以 L1（直接改容器）和 L2（让插件写）在 PA 系上是等价的两条路。

**② FFBS 的浮点数就是 AU 参数原值，不需要任何换算。**
实测 `--set 2 8.4512`（`Band 1 Frequency`）→ `float[2]` 从 `9.965784` 变成 `8.451200`。原因：Pro-Q 4 这个参数在 AU 里的量程**本身就是** log2(Hz)（3.32193–14.8727），所以库里存的 `9.96578` 就是 `log2(1000)`，不是我们算出来的。**旧文档里"频率要取 log2"那一层换算是多余的**——照做也不会错，但没必要。

### 8.3 JUCE 的参数 ID 是哈希值，不是序号

```
0	105897776	On/Off
1	873230368	Output Gain
```
这些是 JUCE 把参数名哈希成的 32 位 `AudioUnitParameterID`。**所以对 JUCE 系插件不能用"第 N 个参数"来定位，必须用 ID 或名字。**

### 8.4 已知例外：参数值变了，状态没变

accconus ERA 6（`NrP6`）上：

```
AudioUnitSetParameter(id=873230368, 0.9) → status=0, 回读 0.9   ✅ 值进去了
ClassInfo 里 jucePluginState            → 2417 字节逐字节不变   ❌ 状态没变
```

`On/Off`（`105897776`）更极端：**回读都不变**，是个只读参数。

两条教训，都要写进实现：

1. **不要相信 `status == noErr`。** 必须回读参数值 + 对比状态 blob，两步都过才算成功。
2. **参数可能只读。** `classinfo.py` 的自检因此会连试 4 个参数，而不是赌第一个。

### 8.5 Waves：真状态在 516 字节的 `data` 里，不在 XML 里

Waves 的 ClassInfo 带**两个**状态键，长得像同一件事，其实不是：

| 键 | 大小 | 真实作用 |
|---|---|---|
| `Waves_XPst` | 5–6 KB | 纯文本 `<PresetChunkXMLTree>`，`<Parameters Type="RealWorld">` 是 **429 个扁平 token**。**插件会丢弃手改过的副本** |
| **`data`** | **恒定 516 B** | **真状态**：12 字节头 + **63 条 8 字节记录 `[u32 BE 参数 id][f32 BE 值]`** |

判据（`logicfmt/waves.py` 的 `is_waves()`）：长度 516、`(len-12) % 8 == 0`、每条的 id 是互不重复的小序号、值有限。

**参数 id 就存在记录里**，所以不需要「参数名 → 第几个数字」的对照表——插件自己的序列化就写着它。

写入 = 等长 `pack_into` 那个 f32，不移动任何字节、不碰任何长度字段。

实测（`aufx RS1M ksWV`，Abbey Road RS124）：把 id 6 写成 250.0、id 5 写成 500.0、id 1 写成 3.0，把 blob 交回插件，插件自己的参数回读就是 **250 / 500 / 3**。

#### 三条走错的路（都是实测，留作教训）

1. **手改 XML token 无效**：410 个 token 逐个改一位再 load，**0 个参数变化**——插件直接忽略手改的 XML。
2. **驱动参数再读回也无效**：`AudioUnitSetParameter` 对 Waves 返回成功但值不变（参数对外只读），所以「设参数 → 存状态 → 看哪个 token 动」也走不通。
3. **跨预设做仿射拟合**能对出 5 个参数（Recovery=277、Super Fuse=279、Mix=281、Output Attenuator=412、Auto Makeup=428），但其余 57 个恒定参数无从定位——因为定位的对象本来就是副本。

能力边界（已更新）：读状态 ✅、按预设切换 ✅、**按名字改单个参数 ✅**（`logicfmt/waves.py`）。
