# HANDOFF · 交接说明（给下一个人，或下一次的自己）

> 这份文件的目的只有一个：**让任何人（包括未来的你）在 20 分钟内接上进度，不用重做已经做过的实验。**
> 最后更新：2026-10-22，HEAD 见 `git log -1`。

## 30 秒速览

`logicfmt` 是一个**纯离线**读写 Logic Pro `.logicx` 的 Python 库：不启动 Logic、不点鼠标，
直接改 `ProjectData` 的块流，然后用 Logic 打开验收。已有能力见 `CORE.md` 的矩阵；
还没走通的三项、以及为什么走不通，见 `docs/08-roadmap.md`。

当前状态：**可发布**。8 项能力经 Logic 实测通过；3 项是记录在案的边界；
`python3 selftest_all.py` → `all modules ok`。

## 先跑这三条命令（确认你拿到的是完整状态）

```bash
cd logic-format-kit
python3 selftest_all.py                 # 期望最后一行：all modules ok
python3 -m logicfmt.project             # 各模块自检明细（project 60 条）
git log --oneline -5                     # 看最近的提交
```

## 目录地图

| 路径 | 是什么 |
|---|---|
| `logicfmt/` | 库本体。`project.py`（块流/轨道/工程）、`midi.py`（区域）、`mixer.py`（推子/发送）、`markers.py`、`tempo.py`、`automation.py`、`classinfo.py`（容器识别）、`au_state.py`（插件参数） |
| `docs/PROJECTDATA-FINDINGS.md` | **最重要的一份**：所有真值实验的字节级结论（§43–§64） |
| `docs/08-roadmap.md` | 三项未完成：卡点、证据、解开条件、不要重做的实验 |
| `docs/07-track-creation.md` | 从零建工程（骨架 5 文件 / 改名 / 定速 / 加轨） |
| `docs/05-verification.md` | 验证纪律 + 取真值的 `pairdiff` 流水线 |
| `tools/pairdiff.py` | 两份 `ProjectData` 的结构差异/字节差异/载荷 diff，取真值必备 |
| `templates/` | donor 字节与骨架工程；`templates/donors/marker_delivery.bin` 是 Marker 三件套+两 qSxT 的原始字节（1,131 B） |
| `_templates/`、`_probes/` | **不发布**：真值工程与探针工程（`.gitignore` 已排除） |

## 三条铁律

1. **真值阶梯**：先拿“Logic 亲手改的”真值 → 离线实现 → 用 Logic 打开验收。三步缺一，
   能力矩阵里就不能标 ✅。
2. **同源真值**：真值对必须是**同一工程、同一 Logic 会话**的两次保存，中间只改一处。
   换工程的对照会被 id 重编号与 UUID 抖动淹没（§50 的教训，§64 是正确做法）。
3. **每个可验证进展单独 commit**，WIP 用 `wip:` 前缀并写清差在哪一处。

## 剩下三项：现状 / 卡点 / 下一步（照着做就行）

### A. Marker 新增（最接近打通的一项）

**已有**（§64，真值对 `PA` 130,551 B/444 块 → `PB` 131,842 B/449 块）：

* 交付物 = 3 块对象（`qSvE:64` + `qeSM:305` + `karT:0`，插在第 406 块处）
  \+ 2 块 `qSxT`（99 B 索引 + 483 B RTF，追加末尾）+ `gnoS` **+160 B**。
* 五块原始字节在 `templates/donors/marker_delivery.bin`。
* 更正：`qSvE:80` **不是** Marker 列表（0 Marker 时它就在，且逐字节相同）。
* 记录 = 48 B，位置在 `+5`；移动/改名/删除已在真实工程验证。

**卡点**：`gnoS` 登记行的字段语义。已知行宽 **24 B**，形如
`[u32 a][u32 b][16B uuid]`，`a` 在一段内递增；新增三件套带来的是
`(a=0x16, b=0x08, uuid=0)`、`(a=0x16, b=0x0c, uuid=…)` 这类成对行，另有
`(0x20,0)`、`(0x20,0x04)` 两种。`b` 疑似“子对象类型”，`a` 疑似“所有者对象 id”，
但和块头里的 id（`qeSM`/`karT` 在 `hdr[2]>>16`，`qSvE` 在 `hdr[3]>>16`）对不上。

**下一步实验（约 3 次 Logic 往返）**：

1. 用 `pairdiff.py` 造第二对真值：同一工程加**第 2 个** Marker，看 `gnoS` 是 +160 还是 +48，
   以及 `qSxT` 是再追加 483 还是也追加 99。这一步能立刻区分“一次性”与“每个”。
2. 把 `gnoS` 第 1 段那 48 B 里的 `a`/`b` 各改一个值（单变量），看 Logic 是否还读得出 1 个 Marker
   —— 这会直接指出哪个字段是对象 id。
3. 解开后：`markers.add_marker(data, bar)` = 三件套 + 2×qSxT + 按新 id 写 `gnoS` 行；验收用
   “同一工程加一个 Marker 后与真值逐块比对”。

### B. 新建 Send

**已有**（§63）：完整增量 —— 通道条 `OCuA:237→241`（`+026`/`+028` 各 +1、`+136` 插
`01 00 00 00`，**工程里所有** `payload[4]==0x40` 的条都要升）、Send 本体 = 一条 `UCuA:76`
（载荷末尾两个 16 B UUID：拥有者条 `+205`、目的地 Aux）、Aux 侧**不新增条块**只加
`ivnE:469`+`karT:58`+`karT:0`+`qeSM:309`+`qSvE:16` 五块对象 + `qSvE` +80 + `gnoS` +80。

**卡点**：与 A 同一层（对象 id 与登记表）。**先做 A**，A 通了 B 就有模板。

### C. `PvWr` / `FrAr` 插件容器

**现状**：`classinfo.describe_blob()` 报 magic + `family="unknown"` 并**拒写**，自检用合成样本
钉住这个行为。**卡点**：本机没有样本（`~/Library/Audio/Presets/Polyverse Music/`、
`Slate Digital/` 都是空目录；Logic 的插件浏览器也不列出 Polyverse）。
**解开条件**：一个含这两个插件状态的 `.logicx` 或 `.aupreset`。

## 环境与陷阱（都是踩过的）

* **存盘**：`logic_save_project`（走 MIDI 键命令 Save）**实测不触发保存**。可靠做法：
  `osascript -e 'tell application "Logic Pro" to activate' -e 'delay 0.5' -e 'tell app "System Events" to keystroke "s" using command down'`，
  再用 `stat -f '%m %z'` 确认写盘。
* **探针工程一律放工作区 `_probes/`**，不要放 `/tmp`：`/tmp` 是 ` /private/tmp` 的软链，
  `logic_open_project` 会因路径规范化报 `verification_failed`（其实已经打开了，用
  `logic_list_windows` 确认，不要重试）。
* **重开同一路径不会回滚未保存改动**，干净状态要用 `logic_reset_to`。
* **录音会并进重叠的已有区域**：造“新增区域”的真值必须录在空白小节。
* macOS + Logic 12.4；`Logic Pro 12.3.1` 起 Marker 名字在 Marker 列表里是只读的。

## 想发布到 GitHub

这个仓库还没有远端。三步：

```bash
# 1) 在 GitHub 建一个空仓库（不要勾选 README），然后
git remote add origin git@github.com:<你的账号>/logic-format-kit.git
git push -u origin main
```

推送前确认 `_templates/`（158 MB 真值工程）与 `_probes/` 没有被带上去：

```bash
git status --ignored --porcelain | head
cat .gitignore
```

## 从哪里开始读

1. `CORE.md` 的能力矩阵 —— 现在能做什么、不能做什么（一屏）
2. `docs/08-roadmap.md` —— 三项边界与下一步
3. `docs/PROJECTDATA-FINDINGS.md` §64 → §63 —— 最近两轮的真值结论
4. `docs/05-verification.md` —— 怎么自己取真值（`tools/pairdiff.py`）
