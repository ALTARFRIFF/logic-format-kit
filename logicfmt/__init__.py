# -*- coding: utf-8 -*-
"""logic-format-kit 参考实现：Logic Pro `.patch` / `.cst` / `.logicx` 读写。

模块
----
au_state     AU 插件状态（FFBS / Pmtr / VC2! / sparse / iZotope）读写
au_batch     声明式批量写入（spec.json）
classinfo    L2：让插件自己序列化状态（kAudioUnitProperty_ClassInfo）
mkpatch      从模板 patch 组装新的通道条 patch
chord        和弦轨区域（497 字节记录、音程位掩码）
meterkey     拍号 / 调号（含任意小节变化）
tempo        速度 BPM（含任意小节变化）
project      ProjectData 块流（读、改、真插入、通道条、Stack、新建音频轨）
mixer        推子 / 声像 / 静音 / 独奏 / 输出 / Send
"""

__all__ = ["au_state", "au_batch", "classinfo", "mkpatch", "chord",
           "meterkey", "tempo", "project", "mixer"]
