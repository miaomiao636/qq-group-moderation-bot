# CASE-BATCH-20260923：案件批量管理与撤回回执超时

负责人选择 A：批量导出和批量结案，只改案件状态，不处罚成员。本轮沿用既有 KEEP → CLOSED 语义，违规记录和累计次数保留；误判撤销、人工已踢出仍逐案处理。没有恢复批量硬删除，没有迁移、生产清理、历史动作重放或群授权变更。根代理是唯一仓库写入者，辅助代理只读审查。

## 使用与边界

案件列表的“操作范围”可选跨页勾选或当前筛选全部。筛选包括状态、群身份/群名、起止日期和归档状态；上方待人工清单使用独立分页，不属于当前案件筛选。CASE-SELECT 补正后的勾选在同一标签页、登录会话和筛选条件下跨页保留；更换筛选或登录后清空，后退/前进重新同步，成功结案后清空已处理选择。补正部署状态见文末，首版不具备跨页保存。

- CSV 每次最多 5000 案，包含群名、群号、QQ 号、来源、群/成员身份、案件状态、创建/结案时间、违规记录数、有效违规类别及证据编号。每案一行，同一成员多个案件不擅自去重。官方 OpenID 单列为身份，不伪装为 QQ 号。只导出摘要，不复制原文、附件和凭据。
- 批量保留结案候选上限已于 CASE-BATCH-5000-20260926 提高到 5000 案（旧版为 200 案，生产加载状态见文末），要求未归档且待审，先列出具体案件供人工核对。CASE-SELECT 补正后 UI 不要求填写原因，默认审计记录为“人工处理”。已关闭重复项在预览时明确跳过；其他不可处理案件整批拒绝。已有保留期清理不会因结案预览获得新删除授权。
- 预览绑定登录会话、明确案件 ID 和证据指纹，有效 5 分钟；确认不重新按筛选挑选新案件。并发确认只执行一次；案件、身份、群名或证据变化要求重新预览。
- 同一事务提交 KEEP/CLOSED、逐案审计及批次完成状态。审计或任何存储失败整批回滚，不出现半批结案。证据检查同时覆盖显式 ID 和 `ViolationRecord.case_id` 反向关联，防止遗漏预览后新增的违规。
- CSV 使用 UTF-8 BOM、标准转义与公式防护；长数字身份按文本处理。返回 `Cache-Control: no-store`，不生成服务器常驻导出副本。导出后的本地文件由下载者保管。

## 后台接口

三个 POST 均要求真实管理 Cookie 和 CSRF，Agent Bearer 不能作为批准权限：

| 路径 | 输入与结果 |
| --- | --- |
| `/admin/cases/batch-export` | `scope=selected/filtered`、多值 `case_ids` 或当前筛选，返回 CSV |
| `/admin/cases/batch-preview` | 同上，`reason` 可省略或留空（默认为“人工处理”，兼容旧调用方的自定义原因），返回冻结计划与完整案件预览 |
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

## 授权部署（2026-09-23）

负责人明确选择“A：现在上线（推荐）”。部署执行提交 `22e60d19874841ad1e7f32e1c575852a46afbd6c`；其应用代码与全量测试源码 `d3e2d3c` 一致。部署前 [CI run 35859367956](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35859367956) 三 job 全部 success，命令 `gh run view 35859367956 --json headSha,status,conclusion,jobs,url` 与日志已核验；实际 checkout 为 `d79f68dc498c9fbdf491abe0b2a66355f9665026`，tree 与部署提交完全相同。此次上线记录提交后的最终 HEAD 再按 §11 查询，另存 `ci-deployment-doc-head.json`，不将上述 run 冒充更新后的文档 CI。

私有收据目录：`D:/QQBotAudits/case-batch-20260923/deploy-01`。本批只改代码、无迁移和配置变更，变更前数据库副本保存在该 D 盘目录；没有制作 C 盘完整恢复副本。

| 执行命令（仓库根，执行 SHA 均为 `22e60d1`） | 已验证结果 |
| --- | --- |
| `powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:/QQBotAudits/case-batch-20260923/deploy-01/deploy-services.ps1`（UAC 正常授权） | 排空观察后正常停 Runtime/Web；数据库备份完整性、外键与 revision `e1c7d4b8a902` 通过；验证后依次启动 Web/Runtime |
| 同上，`service-state.json` | Runtime NSSM PID `12596 → 31528`，Web `33776 → 17736`；运行目录和 Python 均为当前仓库。状态为 `services-running-await-health`，独立健康验收见下一行 |
| `.venv/Scripts/python.exe -B D:/QQBotAudits/case-batch-20260923/deploy-01/deploy.py verify` | UTC `12:41:18` 登录页/健康接口成功，OneBot ready/online、队列为 0；备份后自然 inbox 4 条 DONE、shadow 判定 4 条，动作意图 0。`.env`、规则、群授权、路由、动作/白名单/系统设置指纹均未变；源码文件与固定提交逐字节一致 |
| `.venv/Scripts/python.exe -B D:/QQBotAudits/case-batch-20260923/deploy-01/probe_routes.py` | 新批量接口 OPTIONS 均为 405 / Allow POST，案件首页未登录正确跳登录；确认已加载新入口，不执行结案或下载生产成员数据 |

数据库副本 `moderation-20260923T124034Z-9cujjgur.db`，SHA256 `6201b77631b3b6972b5701693a4eef6e251c37d8f01b9086c6cdde54557be902`；完整路径与校验结果保存在 `backup.json`。授权、脚本哈希和服务轨迹分别在 `authorization.json`、`plan.json`、`service-state.json`；`verified.json` 与 `route-probe.json` 保存实际验收结果。

部署前只读审查发现失败恢复可能在备份失败时启动新版，已在私有执行脚本补 `verified_backup()`，检查收据、固定目录、SHA256、完整性和 revision 后才允许启动。缺备份的 `resume-check` 实测拒绝，服务未受影响；补正收据为 `backup-guard-amendment.json`。没有执行数据库恢复或历史动作重放。第一次健康检查处于 QQ 启动连接阶段未通过，连接 ready 后按原条件复核成功；首次 GET 路由探针命中了既有整型 case ID 路由而返回 422，改用无副作用 OPTIONS 核验实际 POST 入口，未改产品代码或断言口径。

**当前已部署，保持急停 false、recall_only、image_hash shadow 及群开关原值。** 没有代负责人批量结案，没有真人生产 CSV 下载验收，也未观察到新的自然 NapCat 回执超时样本；这些不能由合成回归或自然普通消息替代。应用源码未因本次记录更新改变。

## 仍然独立待办的事项

N03 尚未闭环：现有清理有 mtime/处理时间路径；来源期限未成为覆盖全部副本的持久权威索引。只读审查确认可以先投影已存 `sent_at` 并给未来 manifest 增加非权威观察字段，但不能由此自动删除。共享对象须核对所有引用，未知来源继续保留；混合内容数据库不能按单个文件期限整体删除。持久来源索引、各复制入口继承期限、合规替代备份、历史未知项处置及具体清单批准仍需单独完成。本轮没有改备份格式，也没有生产迁移或删除证据。

整机/异机故障恢复仍待负责人安排维护窗口；主动通知渠道按负责人要求保留但不启用。独立空间巡检不属于本轮交付。


## CASE-SELECT-20260923：跨页选择与人工处理补正（待上线）

负责人反馈不能连续选择多页、结案不希望再填原因。实现提交 `4855a4a8f2f3d6a72004fff0419f324203adfb5b`、回归提交 `07ec9ba3195baadd1be9749e49c8a7f34303bd23`。根代理单独实施，原主审探针未改。

- 前端 `app/web/case_selection.py` 在 sessionStorage 中仅保存案件 ID 与筛选，键使用服务端登录会话 SHA256 摘要，不保存登录令牌。提供全选本页、清空本页、清空全部勾选；浏览器禁用存储会显式提示只可保留当前页。后退/前进使用 pageshow 同步，避免浏览器缓存旧选择。
- UI 去除原因输入，按钮为“批量标记已人工处理”；后台缺省/空白原因统一为“人工处理”，旧 API 自定义原因兼容。预览绑定、CSRF、权限、案件证据漂移、200/5000 上限、事务及幂等均保留。成功确认跳回列表后清空选择，移除一次性复位参数。
- 状态仍沿用 KEEP → CLOSED；不表示系统踢人或撤销违规，不更改违规累计、群动作、保留期、数据库结构。
- 私有回执在 `D:/QQBotAudits/case-batch-paging-20260923`。TDD 首先复现未填原因返回 422；补正后通过。入库回归覆盖未填原因的页面/预览/确认、跨非相邻页面的选择导出与精确结案，验证未选中的 99 个合成案件保持待审。
- 隔离浏览器 HTTP 夹具启动被自动审批返回 `blocked by policy`，未启动服务，也未改用其他工具重试启动。改用 Node VM 无网络模拟验证真实前端脚本；这是本地脚本验证，不能称为真实浏览器点击验收，也未纳入 CI。
- 本地完整验证已完成；最新 HEAD CI 按下述方式单独核验。生产尚未重启加载本补正；上一批 `22e60d1` 的上线授权及 CI 不替代本批。


### 补正验证

执行 SHA `07ec9ba3195baadd1be9749e49c8a7f34303bd23`，仓库根运行；测试 TEMP/TMP 固定在上述 D 盘私有目录的 `test-temp`，`PYTHONUTF8=1`。

| 命令 | 实际结果 |
| --- | --- |
| `uv run --locked pytest --junitxml=D:/QQBotAudits/case-batch-paging-20260923/full-07ec9ba.xml` | 2655 项：2636 passed、19 skipped，0 failed/error；274.33 秒 |
| 同次 JUnit 提取 `tests.test_r132_review_*` | 31 文件、262 项通过；原探针未改 |
| `uv run --locked ruff check app tests alembic` | 通过 |
| `uv run --locked ruff format --check app tests alembic` | 361 文件通过 |
| `uv run --locked mypy app` | 125 源文件通过 |
| `node D:/QQBotAudits/case-batch-paging-20260923/check_selection.js` | 真实前端脚本模拟跨页累计、跨页提交、清空本页、筛选/登录隔离、禁用存储提示、bfcache 恢复、成功后仅复位一次全部通过 |
| `node --check D:/QQBotAudits/case-batch-paging-20260923/case-selection.generated.js` | 通过 |

19 项跳过分别为本机生产 runtime 锁保护 13 项、符号链接权限 4 项、私有真实图片及可选巡检运行时各 1 项，详见 `validation-07ec9ba.json`。工作树阶段 `uv run --locked pytest tests/test_case_batch.py tests/test_r109_case_ui.py -q --tb=short --junitxml=D:/QQBotAudits/case-batch-paging-20260923/focused.xml` 为 67 passed；最终源 SHA 的完整验证才作为当前源码基准。

文档提交不改应用/测试；推送后用 `git rev-parse HEAD`、`gh run list --commit <HEAD>` 与 `gh run view <run-id> --json headSha,status,conclusion,jobs,url` 核验最终 HEAD 三 job，并核对 PR 合成 checkout 的 tree。精确回执保存在同目录 `ci-final-head.json` 和 `ci-final-verification.json`；结果未生成或不是 success 时不可宣称通过。真实浏览器点击与生产加载仍不在本地测试/CI 的证明范围内。

## CASE-EXPORT-FIELDS-20260924：跨页导出字段上限修复

负责人在逐页全选后导出 CSV，浏览器收到 `Too many fields. Maximum number of fields is 1000.`。原脚本为每个跨页所选案件增加一个 `case_ids` 字段，产品允许每次导出 5000 案，表单却在 FastAPI 路由执行前被解析器拒绝；CSV 本身没有生成或损坏。

浏览器现在将所选案件 ID 合并为一个 `case_ids_compact` JSON 字段，提交时禁用本页重复的逐案字段；返回页面时恢复复选框状态。后台限定字段大小、整数类型和最多 5000 个 ID，再沿用既有案件存在性、身份、证据和导出校验；旧版逐案字段请求继续兼容，混用两种格式会拒绝。批量结案仍限 200 案并须预览确认。本轮无数据库迁移，不调整群审核、真实动作或生产案件状态。

回归先失败再修复：1001 个合成案件通过 multipart 表单导出 CSV，条数和成员身份准确、所有案件仍待审；格式异常、混合字段及超限输入被拒。`node D:/QQBotAudits/case-batch-paging-20260923/check_compact_selection.js` 用真实前端脚本验证 1200 个跨页 ID 只提交一个字段、后退恢复和筛选范围行为。本地全量 `uv run pytest -q`、`uv run mypy app`、`uv run ruff check app tests alembic` 通过。源码 CI 与上线验收记录见下节；真人生产跨页导出仍需用户刷新页面后重试。

### 授权部署（2026-09-24）

修复源码提交 `47eac0c622fc5e859a309552f78c8559af74bad1` 已推送；[GitHub CI run 35967964544](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35967964544) 的 Linux、Windows 全量与干净运行依赖三 job 均 success。`uv run ruff format --check app tests alembic` 为 366 文件通过，目标测试 53 项通过；本地全量 `uv run pytest -q` 退出码 0。脚本模拟收据为 `D:/QQBotAudits/case-batch-paging-20260923/check_compact_selection.js` 的输出。

负责人确认部署可满足跨页导出要求后，本轮仅对 Web 服务做短暂重启。先在 `D:/QQBotAudits/case-export-fields-20260924` 使用项目 `backup_sqlite` 建立 162,004,992 字节一致性数据库快照，`integrity_check=ok`、外键错误 0、revision `f3c8a9d12064`，SHA256 `814be641f5671761d8da02a122eb77feccf67b7b00d15cba8e53782472db0873`；收据为 `backup-receipt.json`。普通权限重启被 Windows 拒绝后，通过本机 UAC 管理员授权执行 `restart-web.ps1`，`restart-receipt.json` 记录 Web 从 Running 恢复 Running，Runtime 始终 Running。新 Python 子进程于北京时间 15:31:38 启动；`/healthz` 为 200，`/admin/` 未登录正确跳转登录页（303）。无数据库迁移、配置或群动作变更，没有触发生产案件导出/结案。旧版已打开的浏览器页须刷新后再点击导出；真人生产跨页导出尚待用户验收，不能用合成结果替代。

### 浏览器字符串 ID 热修复（2026-09-24）

首次上线后，真人跨页导出返回“案件编号无效或数量过多”。根因是前端 `box.value` 为字符串，单字段 JSON 实际是 `["1","2"]`；首轮回归构造为整数数组 `[1,2]`，后端严格类型检查误拒绝浏览器请求。新测试先用 1001 个字符串 ID 的 multipart 请求重现 422，再让后端对 ASCII 十进制字符串做长度、正数及 SQLite 64 位范围校验后转整数；旧整数请求仍兼容，混用字段、无效格式和超限仍拒绝。预览操作也用字符串请求回归，5000 个最大合法 ID 测试覆盖字段长度边界。前端脚本模拟额外核对了 1200 个 ID 的元素类型。

源码 `edf986bfdd1d698211f854e57c9abf30cb95aeac` 已推送；本地目标、全量 pytest 退出码 0，ruff check/format、mypy 通过；[GitHub CI run 35973756828](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35973756828) 的 Windows、Linux 全量及干净运行依赖三 job 均 success。热修复前在 D 盘生成 `moderation-20260924T081057Z-6gdokjra.db`，162,955,264 字节，SHA256 `33396b792e89c2925e3a9b745d3743b2adcee3e63bbf430edb40e5489684c571`，`integrity_check=ok`、外键错误 0、revision `f3c8a9d12064`；收据 `backup-receipt-hotfix.json`。本机 UAC 授权执行 `restart-web-hotfix.ps1`，`restart-receipt-hotfix.json` 显示 Web 从 Running 恢复 Running，Runtime 始终 Running；新 Web 进程于北京时间 16:26:41 启动。`/healthz` 为 200、后台未登录 303；OneBot 短暂重连后 `ready`、connected、login online、队列 0。无迁移、群配置或案件状态写入。真人刷新页面后的再次导出仍待用户反馈，不能将合成成功写为生产名单已导出。

## CASE-ACCEPTANCE-20260926：真实页面验收与证据错连导出补正

在已登录的生产后台用 Computer Use 逐页全选，第一页 50 件、第二页 50 件，跨页勾选保持为 100 件；浏览器实际下载 CSV，100 行、案件编号 100 个唯一值。未对生产案件执行批量结案。

在 D 盘一致性数据库副本运行隔离的 SAFE/SHADOW Web，使用真实页面混选 1 件已关闭和 1 件待审案件：预览明确跳过已关闭案，仅列出 1 件待审案；确认后只在副本中转为 KEEP → CLOSED，生产库及审计计数保持原样。

隔离副本中“当前筛选全部”原本在历史证据错连处返回 409。只读盘点发现 1550 件未归档案件中有 21 件涉及 80 条身份不符的反向关联证据。本次补正让 CSV 只计入身份相符的证据，在末尾新增“关联异常证据数”一列；错连记录不进入违规类别、证据编号或数量。详情页隐藏错连证据并提示核对，异常案件的单案处理和批量结案仍严格拒绝，未自动改写历史关联。修复后在同一隔离页面实际导出 1550 行、1550 个唯一案件编号、15 列；21 件异常案的异常计数合计 80。当前只是源码和隔离副本验收，生产 Web 尚未加载此补正。

最终工作树验证：`python -m pytest -q --tb=short` 退出码 0；`python -m pytest tests/test_case_batch.py tests/test_longterm_web.py tests/test_admin_web.py -q` 退出码 0；`python -m ruff check` 与 `ruff format --check` 对改动文件通过；`python -m mypy app` 对 130 个源文件通过。隔离验收服务已关闭，正式 `QQBotWeb`、`QQBotRuntime` 均保持 Running。

## CASE-BATCH-5000-20260926：批量人工处理扩大到 5000 案

负责人要求人工处理每次最多 5000 案，并反馈实际后台全页选中后 CSV 已成功导出；另亲眼确认所核对群消息已撤回。这两项记录为用户现场观察，未据此回写自动通知确认，也不推断全量消息的撤回率。

`CLOSE_LIMIT` 提高至 5000，页面提示沿用同一常量。预览和确认按每 200 案读取一次证据摘要，每块继续保留 20000 条证据保护；全批可超过 20000 条，异常大块仍要求缩小范围。所有分块在同一事务快照下完成身份、状态与指纹校验后才统一结案，不分段提交。每案仍记录 PENDING_REVIEW → KEEP → CLOSED 两次审计，仅最终状态集中刷新；逐案管理审计批量写入，批次完成回执与所有案件共用一个事务。旧调用方的转换刷新行为不变。CSV 的既有导出路径和容量保护未改。

回归先用浏览器同型 `case_ids_compact` 字符串 ID 复现“最多 200 案”失败，再验证 5000 案、25000 条证据成功；混选 4999 待审＋1 已关闭也通过，重复确认不重复审计，原证据全部保留、动作意图数为 0。5001 案在勾选/筛选两种入口均拒绝且不创建计划。末尾完成回执插入失败时，已经写入的案件及逐案审计全部回滚，原计划可重试；后续证据块超限时整批不处理。既有并发、过期、越权、证据变化回归保留。

工作树阶段 `python -m pytest tests/test_case_batch.py -q --tb=short --durations=5` 的 79 项通过；5000 案测试含建数、预览、两次确认和保存结果断言约 2.69 秒，混合关闭项约 2.47 秒（本机合成库，不是生产时延保证）。Ruff 检查、375 文件格式检查和 130 源文件 mypy 通过。完整回归与精确 HEAD CI 仍待结果；私有收据目录为 `D:/QQBotAudits/case-batch-5000-20260926`。本轮尚未重启服务，无生产案件状态、配置、群动作或数据库迁移变更。
