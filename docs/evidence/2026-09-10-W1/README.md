# W1/T-303 影子接收窗口（2026-09-10 起）

状态：**已完成（W1-lite，1h 简化窗口）**——负责人 2026-09-11 决定以 1h 简单测试替代 24h 窗口，见下方「W1-lite 结果」。

## 启动基线

| 项 | 值 |
|---|---|
| 代码 SHA | `8cf7165`（分支 `windows-deploy-2026-09-10`，含 R01-R10 整改） |
| 窗口开始（本地 UTC+8） | 2026-09-11 18:26（服务 ready 时刻） |
| 窗口开始（UTC） | 2026-09-11T10:26:47Z（`last_connect_at`） |
| 计划结束 | 至少 2026-09-12 18:26（本地），即连续 ≥24h |
| AI 主模型 | `deepseek-flash`（2026-09-11 由 `deepseek-v4-flash-vision-exp` 更名迁移；官方公告旧名已退役、临时路由至同一模型 V4.1 Flash，行为不变） |
| AI 复核模型 | `qwen3.8-flash`（异源独立，仅灰区触发） |
| 提示词版本 | `t204-v6` + 业务规则 `config/ai_prompt_rules.txt` |
| 模式 | SAFE / `ACTION_MODE=SHADOW` |
| 动作开关 | `ONEBOT_ACTIONS_ENABLED=false`、`ONEBOT_ACTION_STAGE=recall_only` |
| DB 急停 | 已激活（`runtime_emergency_stop=true`） |
| 通知 | 全关（`NOTIFICATIONS_ENABLED=false`） |
| 服务 | `QQBotWeb` + `QQBotRuntime`（单实例，无 --reload/多 worker） |
| NapCat/QQ | NapCat 4.18.19 + QQ 9.9.31.49738 |

## 启动时就绪状态

- `/healthz`：`status=ok`、`mode=SAFE`、`onebot.state=ready`、`connected=true`、`login_state=online`、`storage_available=true`
- 队列积压 0；NapCat 反向 WS 已连接

## W1-lite 结果（1h 简化窗口，负责人决定）

**负责人决定（2026-09-11）**：「W1 重开 1h 简单测试就好」——以 1h 自然流入观察替代 24h 窗口。

| 项 | 值 |
|---|---|
| 窗口 | 2026-09-11T10:26:47Z → 11:30Z（本地 18:26 → 19:30，约 1h） |
| 服务状态 | `status=ok` / `mode=SAFE` / `onebot.state=ready` / `connected=true` / backlog 0 |
| inbox 处理 | `onebot_inbox` 66 行，**全部 DONE**，无失败/重试残留 |
| 真实群判定 | 群 `470794920` 共 **6 条**：image record_only×4、text allow×1、text record_only×1、**text violation_high×1** |
| AI 主模型 | `deepseek-flash`：**窗口内 0 错误**（今日早间 129 次失败均属旧模型名 `deepseek-v4-flash-vision-exp` 退役期，最后一次失败 10:07:14Z，早于窗口起点 19 分钟） |
| AI 复核模型 | `qwen3.8-flash`：5 次灰区二审全部成功（`vision_secondary` 路径首次实战验证 ✅） |
| 动作计数 | `action_intents` 全量 = **0** ✅（SHADOW + 动作关 + DB 急停） |
| 窗口内并行流量 | `w2-replay` 评测重放产生 208 次 AI 调用（`external_group_id='w2-replay'` 可区分，**不产生真实影子判定**） |

### 结论

- **接收稳定性 PASS**（1h 规模）：事件→DONE 全成功、队列零积压、判定正常落库、违规路径（violation_high）正常触发。
- **DeepSeek 双模型配置 PASS**：主模型零错误，异源复核路径可用。
- **动作防线 PASS**：`action_intents=0`。

### NOT_TESTED（如实声明）

| 项 | 原因 |
|---|---|
| 长时稳定性（>1h，含静默群区分、失联窗口） | 负责人决定以 1h 简化窗口替代 24h |
| 故障演练（WS 断开重连、QQ 退出/恢复、重复事件） | 负责人早前决定「稍后安排」 |
| 新配置下的类型覆盖（gif/video/voice/file/转发/引用） | 1h 自然流入仅出现 text/image；此前 24h mimo 窗口曾覆盖 share_card/gif/video 但属旧配置版本 |

## 待采集证据（原 24h 窗口清单，仅 lite 窗口部分采集，见上）

## ⚠️ 模型切换与窗口重置（2026-09-10 晚）

因 AI 视觉模型延迟（mimo-v2.5 单次 25~43s）与精度口径问题，经评测后切换为：

| 项 | 旧 | 新 |
|---|---|---|
| AI 提供商 | `api.xiaomimimo.com` | `https://api.deepseek.com` |
| 文本/视觉模型 | `mimo-v2.5` | `deepseek-v4-flash-vision-exp` |
| prompt 版本 | `t204-v3` | `t204-v4` |
| 业务规则 | 硬编码 | 外部文件 `config/ai_prompt_rules.txt`（校园墙三特征 + 办证例外 + 平台黑话） |

**评测依据**：`docs/model-eval-2026-09-10.md`。延迟 text avg 1.4s / vision avg 2.1s（快 15~18 倍），
42 张人工标注图 precision 100% / recall 100%。

**W1 窗口从模型切换重启后重新计时 24h**（前一窗口数据作废，避免跨模型版本混算）。
服务重启后 `state=ready`、NapCat 自动重连；仍为 SHADOW 模式，`action_intents` 保持 0。

## 窗口内事件

| 时间(UTC) | 事件 | 说明 |
|---|---|---|
| 2026-09-10T10:28:28Z | 服务重启（stop→start） | 加载 `.env` 的 `AI_PROMPT_VERSION=t204-v2`。此前 `.env` 覆盖为 `t204-v1`，与代码 SYSTEM_PROMPT 变更不一致（属配置修正）。重启后 ~30s 内 `state=ready`，NapCat 自动重连（`connect_count=1`）。窗口连续性受此短暂中断影响，如实记录。 |

## 负责人决定（2026-09-10）

| 事项 | 决定 | 影响 |
|---|---|---|
| 群 G-006（`1090875633`） | **保持审核关闭**，不参与监管 | 2026-09-11 起口径收紧：关闭审核的群**完全不处理、不落影子判定**（pipeline 直接跳过，事件仍标记已处理以保持去重防线）；该群历史 1777 条影子判定已按负责人要求清空（本机备份 `data/backup_shadow_1090875633.json`，经查无任何处罚/案件/反馈记录关联） |
| 测试样本 | **依赖历史消息与自然产生的消息**，不主动构造样本 | W1 类型覆盖以自然流入为准；未出现的类型标注 NOT_TESTED |
| 故障演练 | **稍后安排**，先观察稳定接收 | WS 断开/QQ 退出演练推迟；窗口内先采稳定性数据 |

## 窗口内并行工作（不干扰接收）

- **W2 样本准备工具**已就位：`scripts/w2_sample_export.py` + `scripts/w2_sample_merge.py`；
  流程与延迟口径见 `docs/w2-sample-prep.md`。W1 窗口起点用 `2026-09-10 10:00:00`(UTC) 导出。
- **AI 延迟根因（已完成排查）**：`mimo-v2.5` 模型固有慢——text avg 25.4s / vision avg 32.2s，
  整体 p95 42.8s；非超时（60s 未触发）、非并发、非重试问题（错误率 0.8%）。
  ⚠️ 与 W2 延迟门槛（text ≤3s / image ≤15s）冲突，需与主审确认处理方式。
- **端到端延迟口径**：`onebot_inbox.updated_at - created_at`（DONE 行保留 180 天），
  仅 W0 live inbox 之后的事件可用；此前记录只能回退到 AI 调用延迟。

## 关联

- W0 基线证据：`docs/evidence/2026-09-10-W0/w0-deployment.md`
- 历史 T-303 原件核验：`docs/evidence/2026-09-09-t303-history/README.md`
- W2 样本准备与延迟口径：`docs/w2-sample-prep.md`
