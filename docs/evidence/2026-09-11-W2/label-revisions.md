# W2 标签修订归档（2026-09-11）

批准人：负责人（2026-09-11 对话确认）。执行：Windows Agent。
修订依据：`docs/evidence/2026-09-11-W2/w2-failure-review.md`（逐张目验记录）。
样本 ID 均为匿名代号；原文仅存本机受控路径 `data/media/`，不进入本归档。

## 修订批次与理由分类

### 批次 1（8 条，首轮复核）

| 样本（匿名） | 旧标签 | 新标签 | 理由 |
|---|---|---|---|
| s12135fa67092f6a | violation | normal | 目验=小说章节文字截图，无广告/联系方式 |
| sc52a261ba75741b | violation | normal | 目验=学院官方迎新海报 |
| s1a42e1053dbafca | violation | normal | 与上条同图 |
| s9466108eed241a0 | violation | normal | 目验=个人聊天列表截图，无违规内容 |
| se7d59a9cdcf2195 | violation | normal | 目验="冷"雨衣小狗表情包 |
| s8247cfd12ef251d | violation | normal | 目验=音乐/论坛分享卡片 |
| s201dfca0ea07f75 | normal | violation | 目验=#兼职赚钱# 地推招募（i茅台代抢/游戏推广） |
| s50f2f64ee560da6 | normal | violation | 与上条同图 |

### 批次 2（17 条，t204-v7 严格口径重放后）

全部为逐张目验确认的 **#兼职赚钱# 系列兼职招募/推广图**（刷单、抖音评论兼职、地推拉人、
百度拉新、代发推广等），模型判 `ad` 正确；旧标签系"带校园墙卡片栏=放行"旧口径产物。
样本匿名代号（17）：`sbf2c52d…` `scc8cddb…` `sf987da1…` `s88a35f5…` `sc949277…`
`sb45bc96…` `s6b4bd31…` `s44c9a51…` `s387c356…` `s2b54e0e…` `s104c54f…` `sa7c7fb1…`
`s2f7f861…` `sbc01d19…` `saadae17…` `sbf7ddfe…` `s87ca808…`
（完整映射可由 pre-scope 备份与当前文件 diff 重算，见下方摘要）

### 口径决定 1 条

`sbaebc027fd28f7e`（个人小程序自荐）：负责人 2026-09-11 确认严格口径
**"群内推广任何外部产品都算引流"** → 维持 `confirmed_violation`，规则文件第 4 条例外承载。

## 标签文件与摘要

| 文件 | SHA-256（前16位） | 说明 |
|---|---|---|
| data/w2_labels_all.pre-review-2026-09-11.jsonl | 3CF87D38423A28D7 | 批次1之前（原始盲标） |
| data/w2_labels_all.pre-scope.jsonl | F3343EF34B2DB90F | 批次2之前（含批次1） |
| data/w2_labels_all.jsonl（最终） | 35450F4A8D96A051 | 评测输入标签 |
| data/w2_samples_v7.jsonl（最终评测输入） | 5771582F4F72E228 | 与标签同步后的最终版 |
| data/w2_debug_v7.jsonl（逐条结果） | 00D2F264EFB12D0D | 回放明细（模型曾用旧标签快照） |

## 声明

1. 修订只依据内容客观属性（逐张目验），不参照模型输出。
2. 修订后指标为 **开发回归集**口径；同批样本参与过调参，**不能单独证明长期 100%**。
   正式复测须使用未参与调整的新样本（见主审 S03 整改要求）。
3. 原始标签、中间标签、最终标签三级备份齐全，任何一步可重算。
