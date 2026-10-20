# 模板清单（随本套资料一起分发）

> 全部由 **Logic Pro 12.4 / build 6707** 生成。数字由 `logicfmt` 实测，不是手记。
> **你不需要自己造模板** —— 除非 §3 列的那两种情况。

## 1. 工程模板（`templates/logicx/`）

| 模板 | ProjectData | md5 前 8 | 包体积 | 和弦槽位 | 拍号槽位 | 调号槽位 |
|---|---|---|---|---|---|---|
| `_BASE` | 117,148 | `a261dc9f` | 118 KB | 0 | 0 | 0 |
| `_CHORD` | 127,501 | `13314eff` | 128 KB | 15 | 0 | 0 |
| `_METERKEY` | 118,534 | `13106442` | 120 KB | 0 | 4 | 3 |
| `_SKELETON` | 135,886 | `9730fcbf` | 137 KB | 0 | 0 | 0 |

**槽位** = 你可以直接改写的事件条数。第 1 条永远是"默认值"（默认拍号 4/4、默认调号 C major），它才是全局默认，不要当槽位用。

| 模板 | 里面已经有什么 | 拿来做什么 |
|---|---|---|
| `_BASE.logicx` | 1 条音频轨，4/4，C major，无任何事件 | 一切"从零开始"的底稿 |
| `_METERKEY.logicx` | 4 条拍号变化（3/4、5/4、6/8、7/8）+ 3 条调号变化（D major、F# minor、Bb major + Disable ♭♯） | 改拍号/调号，就地覆盖这 7 个槽位 |
| `_CHORD.logicx` | 15 个和弦（覆盖 Maj / Min / Sus / dim / aug / 各种扩展音 / 多扩展音叠加） | 改和弦，就地覆盖这 15 个槽位 |
| `_SKELETON.logicx` | 5 条音频轨（A1–A5）+ Summing Stack（SUM1）+ Folder Stack（FLD1）+ Aux（AUX2）+ BPM/拍号/调号变化 + 自动化 + 2 个 Marker | 轨道/路由/Stack 的运行时底稿 |

> `_METERKEY.logicx` 与 `_T-MK` 的 ProjectData **md5 相同**（`3ab530c2` 之外的另一份）——它是同一份工程的"干净版"。
> `_SKELETON.logicx` 的对象图**我们没有解**，所以它只用于"让装了 Logic 的人改"，不是离线可写的模板。

## 2. 通道条模板（`templates/patch/`）

| 模板 | 体积 | 内含插件（各一次，默认参数） |
|---|---|---|
| `_TEMPLATE-A.patch` | 36 KB | ERA NR / ERA MdC / SPL De-Verb / Nectar Gate / Gain / Pro-Q 4 / Transient / Pro-C 3 / Pro-MB / Pro-DS / bx_meter |
| `_TEMPLATE-B.patch` | 32 KB | Bass Mint / bx_clipper / HG-2 / TwinTube / karactter / bx_refinement / bx_saturator / bx_console / Maag / museq / Soma |

用法：`mkpatch.py` 从这两个里挑插件块，**整块搬过来并重写槽位字段**（`docs/01 §3` 的两个字段都要写）。插件身份看 `subtype`，所以模板里的顺序无所谓。

## 3. 什么时候你才需要自己造模板

只有两种：

| 情况 | 为什么 | 怎么办 |
|---|---|---|
| **Logic 版本不同** | 记录布局、块头版本字节都可能变 | 按 `docs/04 §2` 的规格造一份，然后跑一遍 `logicfmt` 的自检与槽位计数 |
| **槽位不够** | 本项目未实现"真插入"，事件数受模板槽位数限制 | 在 Logic 里往 `_METERKEY` / `_CHORD` 上再加几条事件，存成新文件即可 |

除此之外，**直接用这里的分发模板**。这也是这套资料的设计意图：一个人造一次，所有人共用。

## 4. 关于 `WindowImage.jpg`

原始工程包里有 `Alternatives/000/WindowImage.jpg`（136–198 KB，是工程窗口截图）。**分发版已删掉它**：

- 它只是工程浏览器里的缩略图，删掉不影响打开、不影响任何格式字段（Logic 会自己重画）
- 它含个人工程截图，没必要带走
- 删掉后每个模板从 272–424 KB 降到 125–144 KB

**除 `WindowImage.jpg` 之外，包内成员一个不少**：`Alternatives/000/` 下的 `ProjectData`、`DisplayState.plist`、`DisplayStateArchive`、`MetaData.plist`，以及 `Resources/ProjectInformation.plist`。

## 5. 复现这张表

```bash
python3 - <<'EOF'
import os, sys, hashlib
sys.path.insert(0, "logicfmt"); import chord, meterkey
D = "templates/logicx"
for n in sorted(os.listdir(D)):
    p = os.path.join(D, n, "Alternatives", "000", "ProjectData")
    if not os.path.isfile(p): continue
    d = open(p, "rb").read()
    try:
        blk, pay, ln = meterkey.find_qsve0(d); pl = d[pay:pay+ln]
        m, k = len(meterkey.meter_blocks(pl))-1, len(meterkey.key_records(pl))-1
    except Exception:
        m = k = 0
    print("%-20s %8d %s chord=%-2d meter=%d key=%d" % (
        n, len(d), hashlib.md5(d).hexdigest()[:8],
        len(chord.find_records(d)), m, k))
EOF
```

## 6. 出处

这些工程由本项目的一位参与者用 Logic Pro 手工建立（不是从别人的工程改的），
用于差分实验；分发的是**实验结束后仍然干净的那几份**。
所有差分实验的中间文件（`_P0/_P1/_P2`、`_K1–_K3`、`_M1/_M2`、`_SIG-A/B/C`、`_CHORD-A/B/C/E`、`_MIDI-A`、`_SKELETON2`、`_T-CHORD`、`_T-MK`）**没有**放进分发版——它们是逆向过程的脚手架，对使用者没有价值。
