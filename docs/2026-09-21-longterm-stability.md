# 长期运行整改（STABILITY-20260921）

负责人在后台分页之后追加授权：检查整个项目可能影响长期使用的问题，找到并修复。本轮围绕接入/媒体/AI、人工案件、数据增长和备份保留开展代码审查与隔离复现；这不等于真实环境连续运行验收或外部主审通过。

## 已落实的修复

| 问题与影响 | 修复及验证边界 |
| --- | --- |
| 同步图片、视频、文件分析占用事件循环，其他消息、动作响应与心跳无法及时处理 | 移到线程计算，每个事件循环共享单容量门闩，保留原串行计算资源上限；排队取消不启动线程，运行中重复取消须等真实线程退出才释放许可。数据库会话仍留在事件循环。 |
| 官方通道只有接收超时才发心跳，持续来消息或慢处理会饿死心跳 | 独立心跳任务随连接管理，使用最新序号；失败传播到现有重连流程，退出取消并等待子任务。 |
| AI 只限制读取空闲间隔，持续慢速传输可长期占住 worker | 沿用现有配置值增加整个请求的总时限，超时仍按 `provider_timeout` 降级；客户端所有权及外部取消保持。没有调整判定阈值或提示词。 |
| 图片分析缓存随不同内容持续增长 | 使用有容量上限的 LRU；逐出后重算得到原有判定。容量是资源边界，不是内容判断阈值。 |
| 人工结案多次独立提交，误判操作可能“案件已关闭但违规仍有效”；相反结论竞争 | 案件状态、违规撤销和操作审计统一事务；读原状态前保留 SQLite 写锁。非法状态仍拒绝，原状态链未改。旧确认码在事务期间占用、成功消费、失败按原代际及有效期恢复；不恢复已删除的旧生成界面。 |
| 案件结束日期漏掉当天最后一秒及小数秒 | 改为结束日期次日零点的排他上界，仍采用既有 UTC 日期口径；非法/反向日期返回 422。 |
| 候选规则固定只显示最新记录，旧候选不可达 | 沿用后台页码组件，对所有未清除候选分页；复制到草稿和人工批准发布的权限链保持。最近反馈区仍是原有“最近记录”范围。 |
| 备份保底把新 `.partial`、空文件/目录算作可保留项，可能挤掉唯一完整副本 | 只有非空、非 `.partial` 文件参与保底候选及新旧排序；保留选中集合的伴随文件。原链接边界和删除前检查保持；不声称对所有历史备份做了数据库完整性验证。 |
| 统计把全量反馈消息号展开为 SQL 参数，积累后超过 SQLite 绑定数上限 | 使用 SQL 子查询，同身份/最新反馈的统计口径保持。 |
| 通知清理一次展开全部通知号，超过绑定数上限后阻止清理推进 | 分批选取和删除，保留同一事务和写锁；未确认、在途发送、近期通知及水位仍受原保护。 |
| 图片像素读取接口已弃用，后续依赖升级存在兼容风险 | 在当前最低/锁定 Pillow 已支持的 API 上等价替换像素读取；灰度转换、缩放、像素比较和阈值均未改。旧迁移测试同时显式序列化日期，避免依赖弃用的 SQLite 默认适配器。 |

没有新增依赖、数据库迁移或政策变更；没有修改 `enforce`、群授权/动作开关、外部主审探针或历史证据。生产库、服务、备份、清理与回滚均未执行写操作。

## 提交与证据来源

- 接手本批基线：`4571e0a2b52049795e45861ea04fb75549bc1557`。
- 代码提交：`4d79c3803ac0817b80bb8dae3277699845c4e9a3`。
- 回归入库提交：`bfc027f2c203e75300966841a8a59299a1c73c10`，新增 `test_ai_request_deadline.py`、`test_longterm_runtime.py`、`test_longterm_web.py`、`test_longterm_data.py`。这些是内部回归，不冒充外部主审探针。
- 等价兼容代码：`001d25562a5cfb115c1fc67b67760ae88de84e9e`；内部测试日期种子兼容：`6806210320d5c8c7810472255d0f665aa026f91b`，原断言保持。
- 文档提交：使用 `git log -1 -- docs/2026-09-21-longterm-stability.md` 核对。后续若仅改文档，用 `git diff 6806210..HEAD -- app tests alembic scripts pyproject.toml uv.lock` 核验受测代码一致。

基线复现使用合成文件/消息及临时 SQLite，未调用外部 QQ/AI，未修改生产。以下目录均位于 `C:/Users/81596/AppData/Local/Temp/`；临时脚本运行不等于入库，正式覆盖以测试提交为准。

| 执行 SHA / 命令 | 当时观察 |
| --- | --- |
| `4571e0a`；`uv run python .../qqbot-runtime-audit-20260921/probe_official_heartbeat.py` | 合成持续来帧场景没有发送协议心跳。 |
| `4571e0a`；`uv run python .../qqbot-runtime-audit-20260921/probe_media_eventloop.py` | 实际管线中替身媒体计算阻塞其他事件循环回调。 |
| `4571e0a`；`uv run python .../qqbot-runtime-audit-20260921/probe_image_cache.py` | 不同合成内容增加时缓存持续增长。 |
| `4571e0a`；`uv run python .../qqbot-web-longrun-071a3688/probe-boundary.py` | 注入违规撤销写失败后案件 CLOSED、违规未撤销、重试拒绝；日期末秒漏项，较旧候选无分页入口。原始结果在同目录 `result-boundary.json`。 |
| `4571e0a`；`uv run python .../qqbot-longterm-data-4571e0a/probe.py`、`probe_notifications.py` | 新残留临时文件挤掉完成备份；统计与通知清理达到实际 SQLite 参数边界后抛错。 |
| `4571e0a`；`uv run pytest tests/test_ai_request_deadline.py -q --no-header --tb=short`（当时该内部测试仅在工作区，未入库） | 文本与视觉的慢速持续响应不能按总时限退出，成功响应对照通过；日志 `qqbot-stability-20260921/ai-before.log`。 |

内部草稿接入过程中调整了调度/故障注入：心跳测试等首次业务序号后观察心跳，允许启动阶段序号为零；取消操作的审计故障使用合法 `MANUAL_PENDING` 前置状态；提交失败在 `AsyncSession.commit` 调用边界注入，避免 SQLAlchemy 提交事件监听器异常改变测试连接事务状态。核心业务断言保留：失败整体回滚、成功一次性、竞争只允许一个合法结论。这些不是外部探针适配。

像素兼容对照：HEAD `bfc027f`、兼容编辑阶段，执行 `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-runtime-audit-20260921/probe_pillow_pixel_equivalence.py`；本地 Pillow 12.3.0 的旧/新 API 均从同一底层像素序列读取。42 个合成输入（不同模式、尺寸、文件格式、动图首帧和坏数据）逐像素与最终哈希相同；25 个有效哈希、17 个无哈希，结果在同名 JSON。这是临时等价验证，不计入正式用例；正式回归以最终全量为准。日期适配器源码实际返回 `val.isoformat(" ")`，本次测试显式使用同样格式，没有改变数据边界或生产 ORM。

## 最终本机验证

执行 SHA：`6806210320d5c8c7810472255d0f665aa026f91b`。运行时仅任务文档有未提交编辑，代码与测试与该提交一致。

证据目录：`C:/Users/81596/AppData/Local/Temp/qqbot-stability-20260921/`。

| 命令 | 实际结果 |
| --- | --- |
| `uv run ruff check app tests alembic scripts` | 通过，退出码 0 |
| `uv run ruff format --check app tests alembic scripts` | 314 文件，通过，退出码 0 |
| `uv run mypy app` | 91 源文件，通过，退出码 0 |
| `uv run pytest -q --no-header --tb=short --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-stability-20260921/full-final.junit.xml` | 1955 项：1939 passed / 0 failed / 0 error / 16 skipped，退出码 0；未再出现上述弃用警告 |

最终全量 JUnit 子集：主审探针 31 文件/262 项全部 passed；新增内部回归 4 文件/49 项，48 passed、1 skipped，已经入库。16 skipped 的实际原因：13 项生产运行时锁、1 项缺本地真实样本、2 项当前环境无法创建相应符号链接；没有为了清零跳过而停止生产或放松测试。复算命令为 `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-stability-20260921/summarize_junit.py C:/Users/81596/AppData/Local/Temp/qqbot-stability-20260921/full-final.junit.xml 6806210320d5c8c7810472255d0f665aa026f91b`，结果 `full-final.junit.summary.json`；仓库测试文件枚举 `rg --files tests -g 'test_*.py'` 为 171 文件。门禁原始输出同目录 `ruff-check.log`、`ruff-format.log`、`mypy.log`。

上一轮本机执行 SHA `bfc027f2c203e75300966841a8a59299a1c73c10`，同上 pytest 命令但输出 `full.junit.xml`：1955 项、1939 passed、0 failed、0 error、16 skipped。主审子集 31 文件/262 passed；新增内部回归 4 文件/49 项，48 passed、1 skipped。该版发现上述兼容弃用提示，修复后按最终 SHA 重新全量。复算命令 `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-stability-20260921/summarize_junit.py C:/Users/81596/AppData/Local/Temp/qqbot-stability-20260921/full.junit.xml bfc027f2c203e75300966841a8a59299a1c73c10`，原始结果 `full.junit.summary.json`。

辅助审查为只读代码复核；不计作独立测试通过，也不替代上述执行证据。外部探针是否不变可用 `git diff 4571e0a..HEAD -- 'tests/test_r132_review_*.py' docs/evidence` 复算。

## CI 与生产加载

本批首推 source head `bfc027f2c203e75300966841a8a59299a1c73c10`，对应 [CI 35577877038](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35577877038)，attempt 1；已完成 job 的实际 PR 合并检出 SHA `c65f41c18bdd49e6da1de4756b94de46d3d443d4`。核验命令 `gh run view 35577877038 --json headSha,status,conclusion,attempt,jobs`；日志 `gh api --allow-escape-sequences repos/miaomiao636/qq-group-moderation-bot/actions/jobs/<jobId>/logs`。

| 首推检查 | jobId / 固定证据 | 实核状态（UTC 2026-09-21 08:30:30） |
| --- | --- | --- |
| Ubuntu | [106263711800](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35577877038/job/106263711800) | success；`uv run pytest`：1952 passed / 3 skipped；format 292 文件；mypy 91 源文件 |
| 干净运行依赖 | [106263711853](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35577877038/job/106263711853) | success；`uv sync --locked --no-dev` 后应用可导入且 pytest 不可导入 |
| Windows | [106263711529](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35577877038/job/106263711529) | 当时仍在运行；完成结果以该 job 原始记录为准，不提前声明通过 |

CI 命令沿用工作流：`uv run ruff check app tests alembic`、`uv run ruff format --check app tests alembic`、`uv run mypy app`、`uv run pytest`，另有仅运行时依赖的干净安装检查；本机静态检查额外覆盖 `scripts`。原始日志与 `ci-35577877038-verification.md` 在本轮 TEMP 证据目录。

**首推 CI 不包含随后兼容补丁，不能证明最终 HEAD。** 文档与兼容补丁推送后的最终 HEAD 必须单独核对：`gh run list --commit <git rev-parse HEAD 的完整值> --limit 3 --json databaseId,headSha,status,conclusion,url`，再用 `gh run view <runId> --json headSha,status,conclusion,attempt,jobs`。最终交接回复提供这一 run 的固定链接、完整 head 与实得结果；不再为把文档自身提交号写回文档而无限触发新 CI。

分页最终文档提交 `4571e0a` 的 [CI 35574880076](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35574880076) 已三个 job 成功，核对命令 `gh run view 35574880076 --json headSha,status,conclusion,attempt,jobs`。它证明分页那一版，不是本批稳定性修改的证据。

**未执行本轮生产重启和生效验收，不能据 Git 更新宣称已加载。** 只读检查执行 SHA `6806210320d5c8c7810472255d0f665aa026f91b`，本地时间 `2026-09-21T16:31:06+08:00`：`Get-CimInstance Win32_Service -Filter "Name='QQBotWeb' OR Name='QQBotRuntime'"` 返回服务 Running；`Invoke-RestMethod -Uri http://127.0.0.1:8001/healthz -TimeoutSec 10` 返回 `status=ok, onebot.state=ready, connected=true, queue_backlog=0`。这只是当时服务可用，不证明加载版本或实际动作效果。受唯一交接入口 §10 第 2 条约束，重启需负责人明确授权及备份/回退方案。可选：

- A（推荐）：约定维护窗口，核对在途任务/健康状态，使用现有 `backup_sqlite` 做一致性在线备份并确认 `quick_check`；正常重启 `QQBotWeb` 与 `QQBotRuntime`，再核验新进程、后台分页、OneBot 就绪和官方通道心跳。无迁移，不变更 QQ/NapCat、群开关或动作模式。Web 断开会要求重新登录，短暂接入间断的未入库消息不保证补齐，在途动作可能转 UNKNOWN 待核对。
- B：先保留当前生产进程，待负责人提供窗口再加载。代价是页面和上述修复暂不生效，原风险继续存在。

本批同时涉及官方运行器，若合并加载分页与稳定性修复，不能再沿用“只需重启 Web”的分页单批方案。出现异常时可按已验证代码版本撤销本批相关提交并重新加载；不回滚/恢复数据库。代码回退与再次重启须纳入负责人授权，本轮没有执行。

仍需外部输入：方案 A 的主审裁定、Windows 回滚全链演练维护窗口、真实消息样本。异常成员巡检按负责人要求暂缓，等待样本后再判断共同特征。本批不替这些项目宣告验收完成。
