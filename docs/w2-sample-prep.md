# W2 独立样本准备与延迟口径

> 适用：W2 验收（冻结模型/规则的离线复核）。运行环境为部署机本地，样本内容含群消息，
> **不得**提交仓库或外发。

## 1. 为什么必须用独立样本

W2 结论用留出集（held-out）评测，必须与规则挖掘集隔离。本机 `feedback_records`
已有 108 条 `confirmed_violation` / 32 条 `confirmed_normal`，但**这些反馈已被用于
反馈挖掘规则**（见 `rule_versions` 的"反馈挖掘全量-20260909"），因此**不得**直接
充当 W2 留出集。

W2 样本 = **W1 窗口及之后新产生的消息**，由人工盲标得到真值。

## 2. 延迟口径（必须先明确，禁止混用）

| 口径 | 字段/来源 | 含义 | 可用范围 |
|---|---|---|---|
| **主口径（端到端）** | `onebot_inbox.updated_at - created_at` | 事件入队 → 处理完成，含排队、媒体下载、AI 条件复核与本地判定 | W0（`d77ea60` live inbox）之后；DONE 行保留 `DECISION_RETENTION_DAYS`（默认 180 天） |
| **回退口径（仅 AI）** | `ai_usage_logs.latency_ms` | 单次 AI 调用耗时（含 base64 编码到响应解析），**不含**排队/下载/判定 | 全量历史 |
| 缺失 | — | 既无 AI 调用也无 inbox 行（例如纯规则 allow 的老数据） | — |

导出工具用 `latency_source` 字段标注每条的来源（`inbox` / `ai_fallback` / `missing`）。
**W2 报告只允许使用主口径**；回退口径仅作交叉参考，不得混入同一份报告。

### 实测延迟（2026-09-10 全量 `ai_usage_logs`）

| source | n | avg | min | max |
|---|---|---|---|---|
| text | 500 | 25 371 ms | 8 781 | 53 921 |
| vision | 267 | 32 173 ms | 6 407 | 213 875 |
| cache | 558 | 30 099 ms | 11 360 | 52 422 |

整体分位：p50 = 27 875 ms，p95 = 42 781 ms，p99 = 51 766 ms。端到端 ≈ AI 调用 + ~0.6 s。

### ⚠️ 与验收门槛的冲突（已知风险）

评测工具 `app.reports.evaluation` 的门槛为 `text p95 ≤ 3 s`、`image p95 ≤ 15 s`。
当前视觉模型 `mimo-v2.5` 单次调用即 25~43 s，**延迟门槛在当前模型下无法满足**。
需与主审确认处理方式（换更快的模型 / 异步化 AI / 重新定义门槛），不得静默掩盖。

## 3. 工具

| 工具 | 作用 |
|---|---|
| `scripts/w2_sample_export.py` | 从判定库导出脱敏候选：`-labeling.jsonl`（含内容、不含系统判定，供盲标）、`-system.jsonl`（系统判定/类别/延迟）、`-manifest.json`（覆盖统计） |
| `scripts/w2_sample_merge.py` | 合并人工标签 + 系统输出，校验枚举/去重/单一版本，产出评测工具所需严格 JSONL |
| `app.reports.evaluation` | 只读元数据 JSONL，输出聚合指标报告（不调用模型/QQ/生产库） |

```powershell
# 1) 导出 W1 窗口候选（起点用部署时间）
.venv\Scripts\python.exe scripts\w2_sample_export.py --since "2026-09-10 10:00:00" --out data/w2_w1

# 2) 人工在 data/w2_w1-labeling.jsonl 每行填 label
#    label ∈ confirmed_violation / confirmed_normal / false_positive
#    （labeling 文件不含系统判定，保证盲标）

# 3) 合并（--strict 遇未标注即报错）
.venv\Scripts\python.exe scripts\w2_sample_merge.py --labeling data/w2_w1-labeling.jsonl --system data/w2_w1-system.jsonl --out data/w2_samples.jsonl --strict

# 4) 出报告（输出文件不可覆盖已有，需新名）
.venv\Scripts\python.exe -m app.reports.evaluation --input data/w2_samples.jsonl --output data/w2_report.json
```

## 4. 覆盖要求（清单 P1-14）

- 每个交付类别至少 **20 个独立正例**、**30 个正常/难负例**。
- W1 未出现的消息类型在报告中标注 `NOT_TESTED`，不得用合成样本充数。
- 模型/规则版本必须单一；混版本需拆成多份报告。
- 重复模板按近重复族隔离，避免同族样本虚增计数。

## 5. 判定/类别枚举

- `verdict`：`allow` / `record_only` / `violation_high`
- `category`：`ad` / `fraud` / `porn` / `violence` / `flood` / `other`（空值回落 `other`）
- `kind`：`text` / `image` / `gif` / `video` / `audio` / `file` / `share_card` / `mixed` / `unknown`
  （库内 `forward_record` 等未知值统一回落 `unknown`）

## 6. 注意事项

- 图片内容需人工查看 `data/media/<name>`（`-labeling.jsonl` 的 `media` 字段给出本机路径）。
- 导出工具读取本机生产库，**只能在部署机本地运行**。
- `docs/evidence/2026-09-10-W1/` 存放 W1 窗口证据；W2 样本与报告同样**只留本机**，
  仅提交脱敏后的元数据摘要与结论。
