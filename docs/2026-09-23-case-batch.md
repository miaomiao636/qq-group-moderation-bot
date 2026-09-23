# CASE-BATCH-20260923：案件批量管理与撤回回执超时

负责人选择 A：批量导出和批量结案，只改案件状态，不处罚成员。本轮沿用既有 KEEP → CLOSED 语义，违规记录和累计次数保留；误判撤销、人工已踢出仍逐案处理。没有恢复批量硬删除，没有迁移、生产清理、历史动作重放或群授权变更。根代理是唯一仓库写入者，辅助代理只读审查。

## 使用与边界

案件列表的“操作范围”可选本页勾选或当前筛选全部。筛选包括状态、群身份/群名、起止日期和归档状态；上方待人工清单使用独立分页，不属于当前案件筛选。勾选不跨页保存。

- CSV 每次最多 5000 案，包含群名、群号、QQ 号、来源、群/成员身份、案件状态、创建/结案时间、违规记录数、有效违规类别及证据编号。每案一行，同一成员多个案件不擅自去重。官方 OpenID 单列为身份，不伪装为 QQ 号。只导出摘要，不复制原文、附件和凭据。
- 批量保留结案每次最多 200 案，要求未归档且待审、填写原因，先列出具体案件供人工核对。混入不可处理案件时整批拒绝，不悄悄部分成功。已有保留期清理不会因结案预览获得新删除授权。
- 预览绑定登录会话、明确案件 ID 和证据指纹，有效 5 分钟；确认不重新按筛选挑选新案件。并发确认只执行一次；案件、身份、群名或证据变化要求重新预览。
- 同一事务提交 KEEP/CLOSED、逐案审计及批次完成状态。审计或任何存储失败整批回滚，不出现半批结案。证据检查同时覆盖显式 ID 和 `ViolationRecord.case_id` 反向关联，防止遗漏预览后新增的违规。
- CSV 使用 UTF-8 BOM、标准转义与公式防护；长数字身份按文本处理。返回 `Cache-Control: no-store`，不生成服务器常驻导出副本。导出后的本地文件由下载者保管。

## 后台接口

三个 POST 均要求真实管理 Cookie 和 CSRF，Agent Bearer 不能作为批准权限：

| 路径 | 输入与结果 |
| --- | --- |
| `/admin/cases/batch-export` | `scope=selected/filtered`、多值 `case_ids` 或当前筛选，返回 CSV |
| `/admin/cases/batch-preview` | 同上并带 `reason`，返回冻结计划与完整案件预览 |
| `/admin/cases/batch-confirm` | 仅使用服务端 `plan_id`；成功跳回列表，重复同一完成计划不重复结案 |

超限、空选择及非法日期返回 422；失效/越界计划、状态或证据冲突返回 409。旧 `/cases/batch-delete` 继续停用。复用现有 `AdminChangePlan` 表，无新增依赖或数据库迁移。

## 撤回超时修复

NapCat `delete_msg` 的 `failed + 整数 retcode=1200`，且 message/wording 有完整 `recallMsg / onMsgInfoListUpdate` 超时签名时，撤回结果归为 UNKNOWN。`EventRet.result=0` 也不证明已撤回。未知结果停止动作链且不可自动重放，存储固定脱敏原因，不复制上游原文。其他响应、错误和动作保持原分类；不回写历史 FAILED，不将旧超时改成成功。

## 验证记录

私有执行收据在 `D:/QQBotAudits/case-batch-20260923`。测试只用合成数据与临时库，不向群发消息，不调用真实审核模型。原主审探针未修改。

- 基线 `0fb7ad639fde2c366de7c731b805f0c91786a0fc` 加新超时回归，执行 `uv run --locked pytest tests/test_napcat_recall_timeout.py -q --tb=short`：6 failed、7 passed，证实分类缺陷；修复后相关回归通过。
- 同基线加本轮工作树与首批案件测试，执行 `uv run --locked pytest tests/test_case_batch.py -q --tb=short`：7 failed（接口尚不存在）；实现后同命令通过，随后补入并发、身份、越权、筛选及 CSV 回归。
- 第一版源码 `2d56fc66c534a5918cfe18787f3c299e27ad467b` 的全量发现历史空外部身份归档筛选兼容问题。原样执行 `uv run --locked pytest tests/test_r109_case_ui.py -q --tb=short` 复现 `test_archived_filter_submission_stays_in_archive` 失败；不改原测试，修正查询兼容后以最终源码重新验证。
- 源码 `2d56fc6` 的浏览器静态合成夹具：`.venv/Scripts/python.exe -X utf8 -B D:/QQBotAudits/case-batch-20260923/ui_fixture.py`；本机回环 HTTP 8129，原生浏览器验收本页全选、清空计数与预览身份/原因/确认按钮。接口执行由隔离 HTTP 测试覆盖；静态页面检查不算生产验收。

最终源码 `d3e2d3ce3cab14e15bd3e672542cb9e4473b0ae1`，以下命令均在仓库根执行，`PYTHONUTF8=1`，TEMP/TMP 指向审计目录下的 `test-temp`：

| 命令 | 实际结果 |
| --- | --- |
| `uv run --locked pytest --junitxml=D:/QQBotAudits/case-batch-20260923/full-final.xml` | 2653 项：2634 passed、19 skipped、0 failed/error |
| `.venv/Scripts/python.exe -X utf8 -B D:/QQBotAudits/case-batch-20260923/collect_validation.py` | 从同次 JUnit 提取原主审子集：31 文件、262 passed；本轮新入库回归 53 passed |
| `uv run --locked ruff check app tests alembic` | 通过 |
| `uv run --locked ruff format --check app tests alembic` | 360 文件通过 |
| `uv run --locked mypy app` | 124 源文件通过 |

`validation-d3e2d3c.json` 保存全部命令、源 SHA、tree 和跳过原因。19 项跳过不能全称平台差异：13 项为本机生产 runtime 锁保护，4 项为符号链接权限，另有真实图片样本与可选巡检运行时各 1 项。第一版 `2d56fc6` 的失败结果保留 `full-tests.xml/log`：2632 passed、19 skipped、1 failed；最终重跑没有覆盖它。额外全目录 `ruff check . / ruff format --check .` 检出了历史证据脚本/Markdown 代码块问题，它们不在项目 CI 门禁范围，本轮未修改历史证据。

源码 [CI run 35858634765](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35858634765) 与最终文档 HEAD 的 CI 分别核验。检查命令：`gh run view <run-id> --json headSha,status,conclusion,jobs,url`；再读取 job 日志，核对实际 PR 合成 checkout 及 tree，不把分支 head 直接写作 CI checkout。源码回执保存 `ci-source-*`；最终文档 HEAD 按 `git rev-parse HEAD` 和 `gh run list --commit <HEAD>` 查询，结果保存审计目录 `ci-final-head.json`。运行未完成或收据缺失时不能宣称 CI 通过；后续文档提交不借用源码 run。

**部署状态：未部署本轮源码。** 生产最近已验证的加载源码仍为 `23d0a1f`。上线需按交接文档 §10 第 2 条取得本轮生产重启授权，先在 D 盘备份，保持现有急停、recall_only、群开关及图片政策；无迁移、无回滚、无历史重放。本轮没有执行线上批量结案。

## 仍然独立待办的事项

N03 尚未闭环：现有清理有 mtime/处理时间路径；来源期限未成为覆盖全部副本的持久权威索引。只读审查确认可以先投影已存 `sent_at` 并给未来 manifest 增加非权威观察字段，但不能由此自动删除。共享对象须核对所有引用，未知来源继续保留；混合内容数据库不能按单个文件期限整体删除。持久来源索引、各复制入口继承期限、合规替代备份、历史未知项处置及具体清单批准仍需单独完成。本轮没有改备份格式，也没有生产迁移或删除证据。

整机/异机故障恢复仍待负责人安排维护窗口；主动通知渠道按负责人要求保留但不启用。独立空间巡检不属于本轮交付。
