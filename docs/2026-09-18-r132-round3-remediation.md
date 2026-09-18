# r132 三轮复验整改与复评请求（2026-09-18）

- **受审基线**：`7ec55534f1d63bd4854ab2ea953a1e1a9d7b86cf`（主审三轮复验结论：一轮 42 项 + 二轮 40 项全过，
  新增独立探针 **4 failed / 19 passed**）。
- **整改提交**：`1cd6afb`（分支 `windows-deploy-2026-09-10`，PR #45）。
- **整改后本机复现**：主审三轮探针目录（未改动任何断言、未删断言、未加 xfail）**23 passed**；
  整改前同一目录 **4 failed / 19 passed**（失败项名称与主审报告逐字一致）。
- **本机全量**：`uv run pytest` **1509 passed / 15 skipped / 0 failed**（junit 收集 **1524**）；
  `ruff check` + `ruff format --check` 通过；`mypy app` 通过（88 源文件）。
- **同 head 远程 CI**：[run 35343211214](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35343211214)
  （head `70c4422a99afe98d09584f84d369406c12707233`）三个 job **全部 success**——
  `Lint, Type-check & Test (ubuntu-latest)`、`Lint, Type-check & Test (windows-latest)`、
  `Runtime deps regression (clean install)`；job 日志 URL 分别为
  [.../job/105593676906](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35343211214/job/105593676906)、
  [.../job/105593677070](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35343211214/job/105593677070)、
  [.../job/105593676741](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35343211214/job/105593676741)。
- **未部署**：生产仍是 2026-09-18 17:18 加载的版本（`t204-v15` 提示词 + 当时的 `wall_pair`），
  本次整改**未重启、未加载**；是否部署由负责人决定。

## 1. 逐项整改映射（主审编号 → 代码 / 回归）

| 编号 | 整改实现 | 回归（入库） |
| --- | --- | --- |
| **F02-R-2**（P1） | `app/moderation/ai.py`：`_miniprogram_qr_allow` 增加 `primary_direct_threshold` / `secondary_review_low` / `secondary_review_high` 三个关键字参数，并原样透传给 `_attachment_reviews_unresolved`；`merge_ai_evidence` 调用处传入本服务实际配置值——**QR 入口与主路径共用同一套阈值**，不再存在第二套隐式 `0.90/0.60/0.90` | `tests/test_r132_qr_configured_thresholds.py`（6 项，含 `configured-strict` 非默认阈值分支） |
| **N01-R**（P1） | `app/moderation/wall_pair.py`：新增 `_confirmed_sources(detail)`，返回**同一条消息内全部**合格来源；每条来源的标记与 `has_miniprogram_code` 仍取自**同一条**视觉结果（不跨结果拼接）；配对循环由"取首条"改为 `extend` 全部来源；`_confirmed_source` 保留为"取第一条"的兼容视图 | `tests/test_r132_source_message_order.py`（8 项，含 `campus-first` / `qr-first` 双向与"前缀与布尔不得跨结果"） |
| **N-F05-1**（P2） | `app/moderation/allowlist.py`：`apply_member_import` 用 `staged` 聚合同一成员本次批准的全部字段，**合成一次**带原版本条件的 `UPDATE`（不再"第一次推进 `updated_at`、第二次按旧版本匹配 0 行"） | `tests/test_r132_f05_round2_edges.py::test_single_member_approved_change_applies[enable-and-note]` |
| **N-F05-2**（P2） | `app/moderation/allowlist.py` + `app/web/routes.py`：`apply_member_import` 新增 `sync_file_text`（被批准的文件原文），在**同一事务/写锁内**核对全量集合——启用中且既不在文件、也不在本次停用名单的成员 → 抛 `ConcurrentMemberSetChangeError`（`ConcurrentMemberChangeError` 子类，沿用同一用户提示）整体回滚并要求重新预览；**不擅自停用**本次未批准的成员。路由在执行事务内传入文件原文 | `tests/test_r132_f05_round2_edges.py::test_unrelated_concurrent_member_addition_invalidates_whole_sync` |
| **F06-R**（P1，部署/恢复门禁） | 新增 `scripts/rollback_preflight.py`（`services` 停服轮询 / `backup-export` 一致性备份 + 可再导入名单导出 + 回读校验 / `check-version` 数据库与代码版本一致性；**全部以退出码表达结论**）与 `scripts/rollback_d037_d038.ps1`（失败即停链：`Status` 属性核对服务存在 → 精确 `STOPPED` → 备份/导出 → 降级 → 切码 → 版本核对 → 启动，每步 `Assert-ExitCode`）；`docs/deploy-runbook-d037-d038.md` 改指向脚本并写清内部顺序与失败边界 | `tests/test_r132_rollback_preflight.py`（25 项，含 `.ps1` 静态门禁） |

`F06-R` 的具体修正点（对应主审三条）：

1. 服务状态改用 **`Status`** 属性（并用 `sc.exe query` 的退出码判定"服务不存在"），
   `@(Get-Service …).State` 这种取不到值的写法已从手册中移除；
2. 第 2 步不再是注释：真实调用项目入口 `app.reports.backup.backup_sqlite`
   （SQLite `Connection.backup` + `quick_check`，WAL 下不漏已提交数据），并校验文件存在非空；
3. 第 3 步不再是 `SELECT count(*)`：**从该备份**导出 `allowlist-*.txt`，回读校验"能再解析导入、
   与库内启用集合一致、条数非 0"；关键命令全部检查退出码，版本不一致不会启动服务，
   失败不会到达 `downgrade` / `start`。

## 2. 请主审重点复核

1. **阈值只有一套**（F02-R-2）：除 `_miniprogram_qr_allow` → `_attachment_reviews_unresolved` 外，
   是否还有其它"绕过服务配置、回落到函数默认值"的入口（建议用非默认 `direct`/`low`/`high` 三组
   值交叉验证，而不只是 `high`）。
2. **来源收集的完备性与不变量**（N01-R）：`_confirmed_sources` 是否在"跨消息行 × 单条消息内"
   两个维度都收集完备；R09 结构化否决（任一 vision 结果 category 非空 / 需人工 / 降级 → 整条消息
   不作来源）与"前缀与布尔同源"是否仍不可绕过；是否存在"合法来源被新逻辑误纳"的路径。
3. **组合字段与集合语义**（N-F05-1 / N-F05-2）：合并为一次 `UPDATE` 后，要求"外部撤权、删除、ABA
   一律拒绝"的既有保证是否仍然成立（`tests/test_r132_f05_plan_integrity.py` 与
   `tests/test_r132_f05_round2_edges.py::test_postclaim_mutation_is_actually_executed_and_rejected`
   保留计数断言：claim / 第二会话提交 / apply 都必须真实到达）；`sync_file_text`
   是否会与"整表同步 = 文件即完整列表"的既有契约产生冲突（尤其"停用而非删除"的语义）。
4. **回滚手册可执行性**（F06-R）：`.ps1` + 预检查脚本在 Windows 上的实跑（含 `StopPending` 超时、
   服务缺失、备份/导出失败、版本不匹配四条失败路径）。
5. **证据表述**：主审指出的"Ubuntu 按原样跑主审版本"已在
   `docs/2026-09-18-r132-round2-remediation.md` 更正为"**断言相同、使用同一 loopback 兼容守卫**"；
   `PROGRESS.md` / `HANDOFF.md` 的旧急停文字已统一（急停 14:49 已由人工解除）。

## 3. 如实登记的缺口（未完成，不作为已关闭）

- **Windows 回滚链未实机演练**：`scripts/rollback_d037_d038.ps1` 只做过静态门禁 + 前置检查脚本的
  隔离库逻辑测试（25 项），**没有**在真实服务/生产库上跑过整链，也没有 Windows 端的完整演练日志。
  生产回滚或重启须负责人另行授权并在维护窗口执行。
- **本次整改未部署**：生产运行代码早于 `1cd6afb`，因此生产现场不体现上述修复。
- **固定 UTC 半开窗口的统计原件**（账号、授权群集合、部署 SHA/提示词版本、导出时刻、完整动作分母、
  失败/超时/跳过、越界核查）仍待补；`code 1200` 只支持"调用超时"，不能直接等同撤回失败或成功。

## 4. 未改动（保留）的边界

D-037 白名单成员全类别放行、D-038 合并转发/群名片一律撤回、D-039 小程序码放行及其"色情/暴力"
两条例外、口径 C 窗口豁免（文字与图片、不豁免 porn/violence/flood）、R09 前缀语义、同秒不豁免、
未完成图"不处罚也不豁免"、账号/群/成员作用域隔离、`recall_only` 阶段与授权群边界、永不产生踢人动作
——以上均**未改动**。
