# 办证误撤事故与口径 A 整改证据（2026-09-16）

## 1. 事故事实（负责人复验发现）

- "办证/学历提升…"文案被**真实撤回 2 次**：intent `#1061`（群 6638171，09-16 00:08）与
  `#1123`（群 6638171，09-16 11:17），均 `SUCCEEDED`；更早 4 次发生在开关关闭期（`SKIPPED`）。
- 判定：`violation_high / fraud`，reason"动态规则达到高置信阈值"——**非 AI 路径**。
- 已撤回消息**不可恢复**（QQ 撤回不可逆）。

## 2. 根因链（三段叠加）

| 环节 | 事实 |
| --- | --- |
| ① 反馈源头（09-08/09-09） | 反馈 #22/#23/#47/#86/#90/#96 把"办证"文案标为 `confirmed_violation + fraud`；其中 #86 是 **RULETEST 测试消息**（`RULETEST_8160bb85`）混入生产反馈 |
| ② 自动挖掘（09-09） | `local-miner` 生成候选 #1→#12→#47→#281（`keyword="办证", category=fraud, 0.95`），随"反馈挖掘全量-20260909"**全量发布**为 DR_167/168/186（版本 #152） |
| ③ 豁免缺口 | 09-14 起"办证例外"仅覆盖本地 ad 路径（且旧实现仅降至 record_only）；`_merge_dynamic_decision` 的"DR 高置信覆盖本地"路径把办证文案升级为 fraud；媒体/AI 层同理 |

## 3. 止血（2026-09-16，服务层带审计，未直改表）

- 停用全部办证类 DR 项：`#167 办证`、`#294 学历提升(学信网可查)`、`#344 学历提升低价`——
  逐条生成草稿并发布（**v53 / v54 / v55**，`rule_audits` 可查；operator=`ops:owner-certificate-allow-2026-09-16`）。
- 候选治理：`#281`（PROPOSED"办证=fraud"）→ **DISMISSED**（防再发布）。
- 复查：ACTIVE 版本中办证类项 `enabled=0`；PROPOSED 无办证类候选。

## 4. 代码修复（口径 A：完全放行，不处罚不转人工）

| 位置 | 修复 |
| --- | --- |
| `app/moderation/rules.py` | 豁免块升级为 `allow`（category∈{None,ad}；**即使零其他命中也打豁免标记**）；移除失效的 DR 前置条件；新增 `_is_certificate_dr_hit`（按 pattern 识别"办证自身"DR） |
| `rules._merge_dynamic_decision` | 办证豁免（allow）时，**办证类 DR 只记录不升级**；非办证类 DR 照常生效 |
| `app/moderation/ai.py` | `merge_ai_evidence` 短路：本地办证豁免（allow）时 **AI 结果（含独立二审确认/严重疑似）不得升级或转人工** |
| `app/runtime/pipeline.py` | 媒体层"媒体部分转人工"不降级办证豁免（媒体真违规仍按 B-2 处理） |
| `app/moderation/dynamic_rules.py` | DR 命中证据携带 pattern（`动态规则命中:{type}:{pattern}`）——提升可诊断性并为豁免识别提供依据 |

**保留例外（不放宽）**：本地文本含严重类别词（porn/violence/fraud，B-2）；非办证类显式 DR。

## 5. 回归与验证

- 事故复现回归：`tests/test_r113_certificate_dr_protection.py`（办证自身 DR 不升级 / 非办证 DR 仍拦 / AI 严重确认不升级）。
- 口径更新回归：`test_r07_certificate_scope.py`、`test_v13_severe_policy.py`、`test_certificate_policy_pipeline.py`（allow 断言 + 保留 B-2 严重词场景）。
- 门禁：本机全量 **1145 收集 / 1130 passed / 15 跳过 / 0 失败**；ruff/mypy 通过。

## 6. 遗留与后续

- **反馈数据卫生**：RULETEST 测试消息不得进入反馈挖掘输入（待整改；本次为 09-09 期历史数据）。
- **挖掘泛化控制**：单关键词从混合样本学"严重类别"需更严门槛（转主审/设计讨论）。
- 决策记录：DECISIONS **D-031**。
