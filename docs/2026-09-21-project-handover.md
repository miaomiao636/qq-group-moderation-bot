# 项目交接文档 —— QQ 群多模态智能管理机器人（r132 评审轮）

> 面向接手的 AI/工程师。**读完这一份即可独立接手**：状态、资产、工作流、纪律、坑、待办、红线、协作约定。
> 生成日期：2026-09-21。所有数字均为**本机实核**（只读）或明确标注为"送审方声明"。

> **本轮新增授权（UI-PAGING-20260921）**：负责人要求群管理、成员白名单、报告待人工清单按页码管理，并显示群审核/动作配置数量。代码与验证进度见 [本轮记录](2026-09-21-admin-list-pagination.md)。账号巡检暂缓，等待负责人提供异常成员样本。本轮是否已加载到生产以该记录为准，不能从代码提交推断。

---

## 0. 一页速览

| 项 | 值 |
| --- | --- |
| 仓库 | `https://github.com/miaomiao636/qq-group-moderation-bot.git` |
| 分支 | `windows-deploy-2026-09-10`（长期工作分支，直接推送；**不要**推 main） |
| HEAD | **以 `git log -1` 的实际值为准**（本文件即在该提交中；状态快照以 `2e9e686` 为基线生成）；接手时先确认工作区**干净** |
| CI | 本次分页变更等待推送后 CI；旧成功记录不能证明本次变更，详见 [本轮验证](2026-09-21-admin-list-pagination.md) |
| 测试基线 | 本次执行 `759dc5c`：**1906 项 / 1891 passed / 0 failed / 0 error / 15 skipped**；命令与原因见 [本轮验证](2026-09-21-admin-list-pagination.md) |
| 主审探针 | 同次全量 JUnit 子集 `tests.test_r132_review_*`：**31 文件 / 262 项**全部通过；本轮未修改 |
| 静态门禁 | 本次执行 `759dc5c`：`ruff check` ✓ / `ruff format --check` ✓（**309 文件**）/ `mypy app` ✓（90 文件）；完整命令见本轮验证 |
| 生产运行态 | 67 群已开**且全部可路由**；阶段 `recall_only`（只撤回）；`IMAGE_HASH_MODE=shadow`；迁移已在生产执行到 `d4b7c1e9a502` |
| 唯一外部依赖 | **主审**（外部 reviewer）：每轮给我方一个"探针包"，我方复现→入库→整改→送审 |
| 当前卡点 | 主审已认可方案 B（本批实现并通过）；**方案 A 提案待主审裁定 6 个问题** |
| 绝对禁止 | 启用 `enforce`、执行生产迁移/回滚、改判定逻辑与阈值、扩群或改动作开关（除负责人明确授权） |

---

## 1. 项目是什么

QQ 群多模态智能管理机器人：**双通道**（OneBot/NapCat + QQ 官方）群管理服务。

- **app/**（运行时代码）：`adapters`（通道接入）、`moderation`（规则+AI 判定、图片感知哈希）、
  `actions`（撤回/禁言等动作与编排）、`runtime`（管线、shadow 决策落库）、`cases`、`reports`、
  `notifications`、`web`（后台）、`core`（路由、配置、契约）。
- **入口**：`app/main.py`、`app/__main__.py`；服务日志在仓库根 `runner.out.log` / `runner.err.log`
  （日常操作见 `docs/windows-operations.md`、`docs/deploy-runbook-d037-d038.md`）。

**关键开关（`.env`，`app/config.py` 定义，2026-09-21 实读值）**

| 变量 | 现值 | 语义 |
| --- | --- | --- |
| `IMAGE_HASH_MODE` | `shadow` | `off`（默认，零行为变化）/ `shadow`（只记录观察，**绝不改变判定**）/ `enforce`（命中即放行，**尚未实现、未授权**） |
| `ONEBOT_ACTION_STAGE` | `recall_only` | `recall_only`（只撤回）/ `full`（含禁言等，需授权） |
| `ONEBOT_SELF_ID` | `530297362` | 采集/执行绑定的账号（工具会校验，缺省即 fail-closed） |
| `DATABASE_URL` | 指向 `data/moderation.db` | SQLite |

> 权限/安全的硬约束：**色情/暴力与本地硬证据不被图片白名单豁免**；AI 判定阈值改动、`enforce`、
> 扩群、动作开关都属于**负责人**决策，AI 不得自行决定。

---

## 2. 状态快照（本轮与历史分开）

**本轮 UI-PAGING-20260921**：代码执行基线 `759dc5c827c2304720eb23ce05e6e704623c3e1a`，页码分页、群配置数量与相关回归已完成，本机全量和门禁通过，结果与命令见 [本轮验证](2026-09-21-admin-list-pagination.md)。推送后 CI 与生产加载分别记录；本轮没有重启生产、没有迁移或更改群开关。账号巡检等待样本。

**以下为接手时保留的历史快照（原基线 `2e9e686`）**，不代表本次 UI 变更已加载，也不把历史生产数量当作实时值：

| 维度 | 事实 | 来源 |
| --- | --- | --- |
| 代码 | HEAD `2e9e686`，工作区干净，与远端同步 | 本机 `git` |
| CI | 上面三次 run 全绿（Ubuntu / Windows / clean runtime-deps 三 job） | `gh run list/view` |
| 测试 | 1895 用例全量通过；262 项主审探针通过 | 本机 pytest |
| 生产迁移 | `alembic_version = d4b7c1e9a502`（= 代码 head，已对齐） | 只读查生产库 |
| 生产群动作 | `provider_group_settings`：onebot **enabled=1 → 67 行**、enabled=0 → 3 行；无跨 provider 同号冲突 | 只读查生产库 |
| 生产可路由 | `group_action_owners` 68 行 / `group_provider_routes` 67 行 → **67 群全部可路由**（不可路由 0） | 只读查生产库 |
| 图片白名单 | `image_allowlist` **68 行，全部 enabled=1**；另有 **1 条"撤回"只存在于拒绝快照**（库中无行） | 只读查生产库 |
| 拒绝快照 | `data/moderation.db.rejections.json`：**1 条**，该条目**无 `state` 键**（历史格式 → 按兼容默认 `rejected` 处理） | 只读查文件 |
| 身份证据 | `docs/evidence/image-review/identity-*.json` 共 **5 份**；最新一份（修复后）为 `identity-20260920T093858Z.json`，`allowed=68 (mismatch 0) rejected=1 (mismatch 0)` | 本机 |
| 生产部署 | 服务已重启加载新代码；提示词 `v15 → v16` | **送审方声明**（未经主审现场重验） |
| 未证明项 | 见 §8 | —— |

**重要口径纪律**：`docs/` 里写"生产已部署/已迁移"属于**送审方声明**；主审会区分"声明"与"现场证据"。
不要把它当成已验证事实对外宣称，也不要反过来臆测生产没做。

---

## 3. 核心工作流：与主审的循环（最重要，先读这一节）

### 3.1 六步循环

```
① 主审交付探针包（用户放到桌面）
     C:\Users\81596\Desktop\qqbot-review-pr45-<sha>\<sha>\
       ├─ REVIEW.md            ← 主审结论（含编号项、残余、关闭标准）
       ├─ VALIDATION.md        ← 复跑命令与证据口径
       ├─ README.md
       ├─ probes/              ← 本轮新探针（可能是 .py）
       ├─ previous*/           ← 上一轮"不可变"原件（用于回归）
       ├─ audit/               ← 可选：对账工具 + JUnit
       └─ *.junit.xml
② 我方**独立复现**：把 probes 原样跑一遍，核对失败数是否与 REVIEW 一致
③ 探针**原样入库**：`tests/test_r132_review_*.py`（只加文件头；必要的平台/路径/调度适配**逐处登记**）
④ 整改代码 → 跑该文件组 → 跑全部 262 项 → 跑全量 → 跑门禁
⑤ 送审材料 + 回评：`docs/<date>-r132-roundNN-review-submission.md` + `...-reply.txt`（纯文本、可直接转发）
⑥ 分三次提交（代码 / 测试 / 文档）→ 推送 → 等 CI（Windows job 约 10-13 分钟）→ 把 CI 的 run/jobId 回填进送审材料
```

### 3.2 五条纪律（都是被主审抓到过、代价很高的）

1. **"本机跑过" ≠ "已入库"，更 ≠ "被 CI 覆盖"**。
   （第十二批我把外部目录跑出的 31 passed 写成了"已入库 + 同 SHA CI 覆盖"，被主审 T01 判为不实；
   正确写法：仓库 196 + **外部 31**，并说明 CI 不覆盖外部包。）
2. **口径必须能复算**：任何"X passed / N 文件"都要标明**执行 SHA + 命令**。
   （"24 passed" 只成立于 `228f276`，在最终 HEAD 上应为"23 业务项 + 1 项历史 AST 审计不适用"。）
3. **主审探针一律原样入库**；任何适配只允许改**输入 seam / 平台路径 / 线程调度**，
   **不得**改业务断言；每处适配要写在**文件头注释 + 送审材料**里。
   （已登记适配见 §9。）
4. **提交三分**：`fix(rX)`（只动 `app/`、`scripts/`）/ `test(rX)`（只动 `tests/`）/ `docs(rX)`；
   送审材料里给出三个 SHA + 各自范围。
5. **不重开已关闭项、不夹带新功能、不为凑测试数删旧测试**；用户明确要求"什么被问到就改什么"。

### 3.3 决策权与协作习惯

- **用户（负责人）是唯一决策者**：扩群、`enforce`、阈值、迁移、回滚、口径、是否重开某项。
- 用户通常以**单字母**回答（`a` / `b`）：给选项时必须写清**代价、风险、推荐项与理由**。
- 主审只出**审查报告与复现测试**，不出补丁；主审明确说过的边界要照抄进回评（例：
  "允许登记输入调度适配，但不能为跑探针放松锁"）。

---

## 4. 资产地图

### 4.1 `scripts/`（22 个工具）

| 工具 | 作用 | 状态 |
| --- | --- | --- |
| `apply_review_decisions.py` | 落库负责人对图片审核清单的结论（失败即停；写白名单 + 拒绝快照） | **可用**（本轮整改对象） |
| `image_allowlist_seed.py` | 白名单种子导入 + 拒绝快照读写 + `decision_lock`/`_snapshot_lock` | **可用**（权威读写入口） |
| `verify_approved_identity.py` | 生效名单/拒绝快照 ↔ 负责人所审文件的身份关联（只读证据） | **可用**（只读） |
| `image_review_export.py` / `image_review_sheet.py` | 导出审核清单 / 写审核结论 | 可用 |
| `image_allowlist_replay.py` | 未迁移表的候选回放 | 可用 |
| `enable_group_actions.py` | 批量开群动作（provider 限定、写事务内复核路由、审计） | 可用（**批量写入，需负责人授权**） |
| `authorize_group_routes.py` | 群→动作路由授权（写锁内复核） | 可用（同上） |
| `group_size_survey.py` | 群人数普查（绑定 `ONEBOT_SELF_ID`、写结构化快照） | 可用（只读） |
| `shadow_report.py` | 图片哈希 shadow 观察报告 | 可用（只读） |
| `window_stats.py` | 时间窗口统计 | 可用（只读） |
| `rollback_preflight.py` / `purge_drill.py` | 回滚预检 / 清理演练 | 可用（谨慎） |
| `reviewer_pack_verify_submitted_evidence_bytes.py` | 主审取证脚本的本地副本 | 参考 |
| 其余（`capacity_loadtest` / `sample_*` / `w2_*`） | 抽样、评测、报告类 | 按需 |

### 4.2 `tests/`

- 本次执行 `759dc5c`：**167 个测试文件、1906 项**；文件枚举和 pytest 命令见本轮验证。15 skipped 实际为 13 项运行时锁、1 项缺真实样本、1 项符号链接能力限制。
- 其中 **31 个 `test_r132_review_*.py` = 262 项主审探针**，是"外部契约"的载体：
  **不要删、不要改业务断言**，只允许"登记式适配"。
- 命名约定：`test_r132_review_roundNN_*.py` 表示第 NN 轮主审包的入库版本。

### 4.3 `alembic/`

- 迁移链 head = **`d4b7c1e9a502`**（`add_image_allowlist`）。
- 启动门禁：`app.db.check_db_migrated()` 会比对"库 revision vs 代码 head"，**库落后即拒绝启动**
  （`tests/test_r132_review_unmigrated_startup_boundary.py` 已证明）→
  **任何新迁移都必须与部署同一窗口**。

### 4.4 `docs/`（评审轮文档序列，按时间读）

- 送审材料：`2026-09-18-r132-(remediation|round2..5)` → `2026-09-19-r132-round10-review-submission`
  → `2026-09-20-r132-round1[1-4]-review-submission`（**round14 是最近一批**）。
- 回评纯文本（可直接转发）：`2026-09-20-r132-round1[0-4]-review-reply.txt`。
- **方案 A 提案**：`2026-09-20-r132-image-decision-authority-proposal.md` + `...-ask.txt`（待主审裁定）。
- 运行手册：`windows-operations.md`、`deploy-runbook-d037-d038.md`、`windows-delivery-checklist.md`、
  `deploy-config-reference.md`、`group-rules.md`。

### 4.5 `docs/evidence/`（证据资产，**不可删**）

- `image-review/`：**9 个批次目录**（`batch-*/`，含 `IMAGE_REVIEW.md`、`DECISIONS.json`、原图）、
  `exclude_hashes.txt`、**5 份身份报告** `identity-*.{md,json}`。
- `allowlist-samples/`（负责人确认放行的样本图）、`stats/`（群人数普查快照 `groups-*.json/.md`）。
- 其他：容量/评测/保留期历史证据目录。

### 4.6 `data/`（生产数据，git 忽略，**只读**）

- `moderation.db`（SQLite）、`moderation.db.rejections.json`（拒绝快照）。
- 只读访问姿势：`sqlite3.connect("file:data/moderation.db?mode=ro", uri=True)`。
- **绝不打印** `.env` 里的令牌/密钥；读取配置只取需要的键名。

---

## 5. 数据契约与坑

### 5.1 表：`image_allowlist`

| 列 | 说明 |
| --- | --- |
| `id, phash(16), note(64), source(16), hit_count, enabled, created_at, created_by` | `phash` 唯一（64 位 dHash 十六进制） |

- **运行时只读 `enabled=1`**（`app/moderation/image_hash.py:167-169`）→ 加列/改工具不影响判定。
- 现状：68 行全 `enabled=1`；**被撤回的哈希在库里没有行**（只在拒绝快照里）——这是双存储问题的根源。

### 5.2 拒绝快照：`data/moderation.db.rejections.json`

```json
{ "<phash>": { "state": "rejected|approved", "source": "review:batch-...|exclude",
               "operator": "...", "at": "...", "first_rejected": "...",
               "history": [ {"state": "...", "at": "...", "source": "...", "operator": "..."} ] } }
```

**坑（必须记住）**：
- **历史条目可能没有 `state` 键** → 兼容默认 `rejected`（生产现存那 1 条就是这种；`verify_approved_identity._state_of` 已处理）。
- `state` 是**枚举**：`approved` / `rejected`；`null` / `true` / `7` / `""` 一律视为非法（fail-closed）。
- 文件**损坏 ≠ 缺失**：损坏必须显式报错，不能当空集、也不能覆盖原件。
- 写入用锁内读改写 + 原子替换（`_snapshot_lock`）。

### 5.3 批次目录：`docs/evidence/image-review/batch-*/`

`IMAGE_REVIEW.md`（表格：编号 | 状态 | 图片 | 来源 | 完整 SHA-256 | dHash | …）、
`DECISIONS.json`（`{"batch": <目录名>, "decisions": {"1": "放行|撤回"}}`）、原图文件。
**编号是正整数**（导出用 `:02d` 只是最小宽度，第 100 张是三位）。

### 5.4 双存储问题（C03 系列的根因，务必理解）

"决定状态"被拆在 `image_allowlist` 行 + 拒绝快照 JSON 两处，**没有共同提交边界** →
历史上出现"补偿覆盖后来的成功决定""最后一次读取到 commit 之间被穿过"等反例。
现状：用**跨进程 `decision_lock`** 覆盖"前态读取 → DB 变更 → 快照发布 → 补偿"全链（方案 B，已通过）。
**根治方案 = 方案 A**（见 §7 待办 1）：决定/来源/操作版本/生效位同库事务保存，JSON 仅作可重试导出。

---

## 6. 常用命令与 Windows 坑

```powershell
# 进入仓库
cd "d:\CodeBuddy工作空间\CB 项目\qq-group-moderation-bot"

# 全量测试（统计口径用 junit，避免被 -q 输出淹没）
uv run pytest -q --no-header --tb=no --junitxml=tmp_j.xml
uv run python -c "import xml.etree.ElementTree as ET; r=ET.parse('tmp_j.xml').getroot(); ts=list(r.iter('testcase')); print('TOTAL',len(ts),'FAIL',sum(1 for c in ts if c.find('failure') is not None),'ERR',sum(1 for c in ts if c.find('error') is not None),'SKIP',sum(1 for c in ts if c.find('skipped') is not None))"
Remove-Item tmp_j.xml          # 别把临时文件留在仓库

# 只跑主审探针（262 项）
uv run pytest tests -k "r132_review" -q --no-header -p no:randomly -p no:warnings

# 复跑主审给的外部探针包（注意：PYTHONPATH 与 -c pyproject.toml -o addopts=）
$env:PYTHONPATH=".;tests"
uv run python -m pytest -c pyproject.toml -p conftest "<包路径>\probes" -q -o addopts= --tb=no

# 门禁三条（与 CI 一致）
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app

# CI
gh run list --limit 3 --json headSha,status,conclusion,databaseId
gh run view <run-id> --json headSha,attempt,conclusion,jobs
Start-Sleep -Seconds 600        # Windows job 约 10-13 分钟；循环检查直到 completed
```

**Windows 环境的坑（都踩过）**

1. **pytest 通配符要用正斜杠**：`tests/test_r132_review_*.py` 有效，`tests\test_r132_review_*.py` 会报 "file or directory not found"。
2. **`Get-Content` 被安全策略拦**（读 `.env`、配置 JSON 时会报 SecurityException）→ 改用 Python 读，或只取所需键。
3. **PowerShell 引号地狱**：`python -c "…含 \" 的代码…"` 容易被吞引号 → 复杂片段用**单引号**包整段代码，或写**临时脚本**（用完 `Remove-Item` 删掉）。
4. pytest 清理临时目录时会打印 `safe-delete ... CONFIRM_REQUIRED` 噪声（不影响结果）。
5. 仓库里已有 `runner.out.log` / `runner.err.log`（服务日志），不要误提交改动。
6. 随机化插件：复跑主审包时用 `-p no:randomly` 固定顺序；官方全量用项目默认配置。

---

## 7. 未关闭项与待办（含前置条件）

| # | 事项 | 状态 / 前置 | Owner |
| --- | --- | --- | --- |
| 1 | **方案 A 提案**（同库权威保存 + JSON 可重试导出）：`docs/2026-09-20-r132-image-decision-authority-proposal.md` | **等主审裁定 6 个问题**（表形态、rejected 落行、导出失败语义变更确认、回填方式、迁移与部署同窗口、回滚口径） | 用户转发 → 主审 → 再实现 |
| 2 | Windows **回滚全链实机演练** | 需负责人给**维护窗口** + 备份；**不在生产演练**（先合成库） | 用户排期 |
| 3 | "1 次未复现失败"钉死 | 需复现条件；至今未复现 | 我方 |
| 4 | shadow 观察**第二份报告** | 等真实样本出现 `matched=true`（现在 0 命中，报告价值低） | 我方（持续跑） |
| 5 | 54→67 群补 owner/route | **已完成**（实核：67 群全部可路由） | 已关闭 |
| 6 | `enforce` 实现与阈值校准 | **未实现、未授权**；不得擅自开始 | 负责人 + 主审 |
| 7 | UI-PAGING-20260921：群/成员白名单/待人工清单分页与群状态数量 | 代码、本机全量和门禁通过；CI 与生产加载以 [本轮记录](2026-09-21-admin-list-pagination.md) 为准 | 我方；生产重启由负责人决定 |
| 8 | 异常账号巡检 | **负责人明确暂缓**；等待真实异常成员样本后评估共同特征 | 用户提供样本 → 再评估 |

---

## 8. 未证明项（`NOT_PROVEN`，**不能用本地测试顶替**）

A09 前 6 秒、历史授权快照、导出多查询一致性读事务、Windows 回滚全链实机演练、
一次未复现失败、`enforce` 最终批准与阈值校准、"B（补路由）"的实际动作效果。
审计里的 `stage_runtime_proven=false` 是**如实标注**，不构成"服务已加载 `recall_only`"的证据。

---

## 9. 已知"登记式适配"清单（改测试前先读）

| 文件 | 适配 | 原因 |
| --- | --- | --- |
| `tests/test_r132_review_group_enable_boundaries.py` | ①平台无关路径注入；②`setup` 改写入合法结构化快照；③`test_stale_survey_enabled_flag_...` 改走**真实 JSON seam** 并断言解析器读到 `True` | 生产移除 injected-loader 兼容分支；Windows 反斜杠；原用例未覆盖其命名前提 |
| `tests/test_r132_review_round11_c03_operation_identity.py` | 最后一个用例的**线程调度**改为"后一次审核被锁阻塞" | 方案 B 跨进程锁使原到达点不可达；**最终状态断言逐字保留** |
| `tests/test_r132_review_r9_write_residuals.py` | 并发用例调度同上（阻塞+顺序执行） | 同上 |
| `tests/test_r132_review_round12_c03_second_snapshot_commit.py` | 2 参数合并为 1 个"阻塞+顺序+最终一致"场景 | 同上 |
| `tests/test_r132_review_round12_all_candidate_evidence.py` | 无（verbatim） | —— |
| 入库时保留的其它 6 个文件 | 仅加文件头 + `ruff format` 风格（AST 逐节点一致） | 可复算：`ast.dump` 逐节点比对 |

登记纪律：**新适配必须同时写进文件头 + 当批送审材料**，并给出"AST 一致性/AST 差异函数"证据。

---

## 10. 红线（绝对不做）

1. 不启用 `enforce`、不改判定逻辑与阈值、不改安全例外口径（色情/暴力/硬证据）。
2. 不执行生产迁移、不回滚、不重启生产服务、不改动作开关或群授权（除负责人明确授权 + 有备份与回滚方案）。
3. 不删除/改写 `docs/evidence/` 下的证据、批次目录与主审探针；不为通过而放松断言或锁。
4. 不在文档里把"声明"写成"已验证"；不把"跑过"写成"入库"；不臆测生产状态。
5. 不打印/不提交任何令牌、密钥、`.env` 内容、生产库文件（`data/` 已被 gitignore）。
6. 不用 `git push --force`、不推 main、不跳过 hook；提交前必须 `git status` 干净。

---

## 11. 两个 AI 的协作约定（交接协议）

1. **单一写入者**：同一时刻**只有一个 AI** 修改仓库并推送。交接时用本文件 + `git log` 对齐，
   接手方先 `git pull` 并确认 HEAD。
2. **分工建议**（可按用户指派调整）：
   - A：**实现 + 回归**（`app/`、`scripts/`、`tests/`，跑全量/门禁/CI）；
   - B：**送审材料 + 回评 + 证据对账**（`docs/`，复跑主审包、登记适配、回填 CI jobId）。
   - 两人都要守 §3.2 的五条纪律；**改动前先读最新文件内容**（可能已被另一方改过）。
3. **交接检查单**（每次交接前逐项确认）：
   - [ ] `git status` 干净、HEAD 已推送、CI 最新 run 为 success
   - [ ] 本轮全量 1906 项 0 failed / 0 error；262 项主审探针通过（执行 SHA 与命令见本轮记录）
   - [ ] 门禁三条通过（ruff check / format --check / mypy）
   - [ ] 本文件 §2 的状态快照与 §7 的待办已更新（数字实核过）
   - [ ] 本轮新适配已登记进 §9 与送审材料
4. **冲突避免**：不要同时编辑同一文件；若必须，先各自 `git pull`，由后提交者合并并复跑门禁。
5. **交接点**：① 主审新探针包到达时；② 方案 A 裁定后；③ 迁移/回滚窗口前后。

---

## 12. 快速上手：三个最可能的入手任务

**任务 1（最常见）：主审又给了一个包**
```powershell
# 1) 读结论
Get-Content '<包>\REVIEW.md' | Select-Object -First 120     # 若被拦，用 python 读
# 2) 独立复现（核对失败数）
$env:PYTHONPATH=".;tests"; uv run python -m pytest -c pyproject.toml -p conftest "<包>\probes" -q -o addopts= --tb=no
# 3) 原样入库（只加文件头）到 tests/test_r132_review_*.py；复用 §9 的登记格式
# 4) 整改 → 跑该组 → 跑 tests -k r132_review → 跑全量 → 门禁 → 三分提交 → 推送 → 等 CI → 回填 jobId
```

**任务 2：主审裁定方案 A 后实施**
按提案 §3-§5 顺序：`app/models.py` 加列 → 迁移文件 → 回填脚本（`--dry-run` 先行）→
写入路径去补偿（导出改为事务外可重试）→ 身份工具改读库 → 按提案 §8 更新 4 处语义变更用例
（**必须先得到主审对语义变更的确认**）→ 合成库演练"备份→迁移→回填→导出→回滚→再启动+对账"。

**任务 3：出 shadow 第二份观察报告**
等 `data/moderation.db` 里出现 `matched=true` 的样本后：
```powershell
uv run python scripts/shadow_report.py --db data/moderation.db --since "<UTC>" --until "<UTC>"
```
（注意：窗口支持小数秒；报告要区分"窗口内观察"与"全库累计"，不要把不同范围相除。）

---

## 13. 一句话总结

本轮分页的本机验证通过，远端 CI 与生产是否加载分别以 [本轮记录](2026-09-21-admin-list-pagination.md) 为准；
**真正的工作面是"与主审的证据对话"**：复现 → 原样入库 → 登记式适配 → 小步整改 → 可复算的口径。
外部输入仍包括：**方案 A 的裁定**、**回滚演练的窗口**；本轮新增等待**分页加载的生产重启授权**与**异常成员样本**。
