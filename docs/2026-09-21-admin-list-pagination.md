# 后台列表分页与群状态数量（UI-PAGING-20260921）

负责人 2026-09-21 授权直接实施页码分页；账号巡检暂不实施，待负责人提供异常成员样本后再评估。

## 实现与边界

- 群管理、成员白名单、报告待人工清单：服务端分页，默认每页 20 条，可选 50 条；总数、当前/总页数、页码链接、上一页/下一页与跳页控件。越界页归到有效页，每页数量仅接受上述选项。
- 案件首页待人工清单也分页；默认仍折叠，翻页后保持展开。`pending_page/pending_page_size` 与下方案件页码、筛选独立保存。
- 群数量使用原有“消息中已见身份 ∪ 已登记配置”的范围，按 `(provider, external_group_id)` 区分，忽略空身份；不是机器人当前加入的全部群。摘要含隐藏群，列表按正常/隐藏视图分别分页。
- 群状态：后台已记录、审核开启、仅审核、审核＋真实动作已配置、审核关闭、已隐藏。未登记配置沿用默认审核开启/动作关闭；审核关闭时不计入审核＋动作。配置数量不代表实际动作已生效。
- 成员白名单总数/启用/停用统计覆盖完整名单；仅页面查询分页，原有整表导入、预览确认、全量启用成员导出保持原契约。
- 待人工清单仍只按 `PENDING_REVIEW` 筛选，包含历史归档行；日报/周报总数不按页面截断。案件链接仍进入原详情与审批流程。
- 未修改判定逻辑、阈值、图片模式、群开关/授权、锁、迁移或外部主审探针；没有新增依赖。

## 分离提交

| 范围 | SHA | 文件 |
| --- | --- | --- |
| 代码 | `2cd1a898bc136a3ec5948c4a64fcb948fb41cc93` | `app/web/routes.py`、`app/web/pagination.py` |
| 测试 | `8c4aefde4394532855bafa6a8a4ab19a15641c18` | `tests/test_admin_list_pagination.py` |
| 兼容修复 | `759dc5c827c2304720eb23ce05e6e704623c3e1a` | `app/web/pagination.py`：表单 action 不含片段标识；页码链接仍保留锚点 |
| 文档 | 以本文件的 `git log -1 -- docs/2026-09-21-admin-list-pagination.md` 为准 | 本说明、交接入口与任务记录 |

## 验证记录

执行 SHA：`759dc5c827c2304720eb23ce05e6e704623c3e1a`。运行时仅任务文档有未提交编辑，`app/` 与 `tests/` 与该提交一致；最终文档提交须用 `git diff <执行SHA>..HEAD -- app tests alembic scripts pyproject.toml uv.lock` 复核无差异。

| 命令 | 结果 |
| --- | --- |
| `uv run ruff check app tests alembic scripts` | 通过，退出码 0 |
| `uv run ruff format --check app tests alembic scripts` | 309 files already formatted，退出码 0 |
| `uv run mypy app` | 90 source files，无问题，退出码 0 |
| `uv run pytest -q --no-header --tb=no --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-ui-paging-800c6e47/full-final.junit.xml` | 1906 项：1891 passed / 0 failed / 0 error / 15 skipped，退出码 0 |

上列全量 JUnit 中筛选 `classname` 以 `tests.test_r132_review_` 开头的 testcase：31 文件、262 项，全部 passed；本轮新增分页测试 11 项已入库且全部 passed。这里是仓库全量执行的子集统计，不是外部目录“跑过”的结果。文件数复算命令：`rg --files tests -g 'test_*.py'`（167 文件）；主审探针 `rg --files tests -g 'test_r132_review_*.py'`（31 文件）。

15 skipped 的实际原因：13 项因生产服务持有运行时锁，1 项因本地真实样本缺失，1 项因当前环境无法创建文件符号链接；不能概括成全部属于 Windows 平台差异。本次没有为了消除跳过而停止生产服务或放松断言。

前次执行 SHA `8c4aefde4394532855bafa6a8a4ab19a15641c18`，同上 pytest 命令但 JUnit 文件为 `full.junit.xml`：1906 项，1890 passed / 1 failed / 0 error / 15 skipped。唯一失败为 `tests/test_r109_case_ui.py::test_archived_filter_submission_stays_in_archive`：原 HTTP 探针把查询串追加在新分页表单 action 的片段标识之后，未传递筛选参数。不能据此断言真实浏览器标准 GET 提交也会丢参数。兼容修复移除表单 action 的片段标识，原测试和主审探针均未修改；“应用”回页面顶部，页码链接仍定位列表。

隔离页面预览使用临时 SQLite 与合成数据，验证群/白名单/报告的上一页、下一页和每页数量控件；没有连接生产后台。执行 SHA 同上，命令为 `uv run python <临时目录>/final-preview/render-pages.py`（`PYTHONPATH=.`），再用 bundled Node 执行 `<临时目录>/final-preview/verify-pages.cjs`；结果为 `paginationLinks=passed, pageSizeControls=passed, javascriptErrors=[]`。临时目录：`C:/Users/81596/AppData/Local/Temp/qqbot-ui-paging-800c6e47/`。这不是生产页面已生效的证据。

## CI 与生产加载

- 本次代码、测试和首份文档已推送为 `229d85638301c35b37c6a93289b69a7a0b9d1555`，对应 [CI run 35573589364](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35573589364)（attempt 1）。本节保存固定来源，最终结论以该 run 原始记录为准；不把排队/执行中写成通过。
- 该 run 的 PR 合并检出 SHA 为 `a9a14874e3254c3049fdf9547ff037d578092615`（runtime 与 Ubuntu checkout 日志已核对），不能将源分支 head 和实际执行 SHA 混称。复核命令：`gh run view 35573589364 --json headSha,status,conclusion,attempt,jobs`；单 job 原始日志使用 `gh api --allow-escape-sequences repos/miaomiao636/qq-group-moderation-bot/actions/jobs/<jobId>/logs`。

| 检查 | jobId | 固定证据入口 |
| --- | --- | --- |
| Ubuntu 质量与全量测试 | `106250323836` | [执行结果与日志](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35573589364/job/106250323836) |
| Windows 质量与全量测试 | `106250323834` | [执行结果与日志](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35573589364/job/106250323834) |
| 干净运行时依赖 | `106250323620` | [执行结果与日志](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35573589364/job/106250323620) |

CI 的命令是 `uv run ruff check app tests alembic`、`uv run ruff format --check app tests alembic`、`uv run mypy app`、`uv run pytest`；CI 静态检查不含 `scripts`，本机额外覆盖该目录。源代码执行基线到文档提交的 `git diff 759dc5c..HEAD -- app tests alembic scripts pyproject.toml uv.lock` 应为空。**文档回填后的最终 HEAD 仍须单独核验其最新 CI**：`gh run list --commit <git rev-parse HEAD 的完整值> --limit 3 --json databaseId,headSha,status,conclusion,url`，不能用前一 run 代替；最终交接回复提供这一新 run 的链接和实得结论，避免文档自引用导致无限补提交。

- 生产尚未重启加载。本次无数据库迁移；是否立即重启 `QQBotWeb` 仍受交接入口 §10 的生产操作授权约束。
- 当前 `QQBotWeb` 同时承担后台与 OneBot 接入，不能把重启描述为绝不影响消息接入。未授权前不执行重启、急停或生产回滚。
- 生效方案：获得明确窗口/授权后，核对在途任务与健康状态，使用现有 `app.reports.backup.backup_sqlite` 做一致性在线备份并确认 `quick_check`；仅正常重启 `QQBotWeb`，检查新进程、健康/OneBot 就绪与持续心跳，再重新登录验收分页。无需重启 `QQBotRuntime`、QQ 或 NapCat。断线期间未入库消息不保证补齐，遗留执行中动作可能变为 UNKNOWN 待核对。
- 若新页面异常，代码回退方案是撤销本次 Web/测试提交后重新加载；数据库不回滚、不恢复。该代码回退与再次生产重启也须纳入负责人授权。本轮仅备好方案，未执行生产备份、重启或回退。
