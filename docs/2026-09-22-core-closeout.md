# 主项目收尾 CORE-CLOSEOUT-20260922

> **后续状态：已生产上线。** 负责人另行明确授权“方案 A 上线”，本次维护及恢复证据见 [上线记录](2026-09-22-authority-a2-deployment.md)。下文“候选/未部署/待窗口”描述此前状态；新的生产事实以上线记录为准。

范围排除独立手机/空间巡检。负责人要求无须决策的剩余事项直接执行。先等待巡检任务提交并推送 `afdb5a38e5f0b7c644b7423767354ca936066132`，确认工作区干净后接手单一写入。本轮不修改生产开关、群授权、判定阈值，不执行生产迁移/清理/回滚/停服。

## 负责人决定

- 通知：曾选择 QQ 管理群＋邮件后备，随后明确“先不用，保留这个通知渠道就好”；最终口径是保留现有能力、暂不开启，不发送测试通知。
- N03：先准备准确到期清单和隔离恢复验证，具体原件处置清单确认后才处理；不是授权立即删除原件、备份或审核证据。
- 方案 A：负责人授权本任务兼任审查与实施，按推荐方向、小范围改造；不再等外部主审裁定。需先修正提案中的兼容/历史保留问题，在隔离副本实现；不把这一授权解释为可直接迁移生产。
- 整机故障与恢复：继续待排期，主服务保持运行。

## 报告一致性修复

`window_stats` 原本在多个独立 SELECT 中查询授权、判定、动作；`shadow_report` 原本分别读取明细和分母。WAL 写入并发发生时，同一报告可混入不同数据库状态。新增显式只读事务；所有查询共用同一快照，成功和异常均关闭连接。窗口工具也修正无时区输入按 UTC 解释、拒绝空/倒置区间；两种报告采用独占创建，存在同名证据时明确失败，不覆盖旧证据。

源码提交 `de90679`；内部回归提交 `cda90d7206385f8d494b88b20519ce394d4d0203`，新文件 `tests/test_report_snapshot.py`。不是外部主审探针，未更改 `tests/test_r132_review_*`。

初始脚本源码与 `d2a4f6b` 一致，修复前回归在 TEMP 运行，失败原件 `C:/Users/81596/AppData/Local/Temp/qqbot-core-closeout-20260922/report-red.log`。命令 `uv run pytest -q -o addopts= C:/Users/81596/AppData/Local/Temp/qqbot-core-closeout-20260922/test_report_snapshot.py --tb=short`；当时源码尚未入库，不把 TEMP 运行算作已入库。

## 固定窗口 shadow 第二份报告

执行 SHA `cda90d7206385f8d494b88b20519ce394d4d0203`，命令：

```powershell
uv run python C:/Users/81596/AppData/Local/Temp/qqbot-core-closeout-20260922/export-core-evidence.py
```

脚本仅调用已提交只读报告入口；原始报告留本机 `committed-observation/`，入库只保留脱敏聚合。UTC 半开窗口 `2026-09-21 12:00:00` 至 `2026-09-21 15:30:00`，在 MEDIA-QUOTA 正常重启后；生产加载基线仍 `d7ceaf3`。

- image_hash 观察 348 条，全部 shadow；附件检查 84 条；matched 18 条，would_allow 0。命中原判已 allow，不把它解释为新放行效果或 enforce 已实现。
- 判定：violation_high 149、allow 146、record_only 53。
- 全局动作意图（与本账号判定分列）：SUCCEEDED 136、SKIPPED 10、FAILED 3；动作日志成功撤回 136、code 1200 超时 3。超时只表示调用未获成功确认，不证明 QQ 最终未撤回；没有重放或补罚。
- 尝试外发目标与导出时授权集合的差集为空；历史动作发生时的授权快照仍 NOT_PROVEN。两个报告各自拥有一致快照，不声称它们共享同一跨报告事务。

本轮报告可以关闭“第二份固定窗口观察报告未整理”和本工具多查询快照缺口，不能关闭 enforce、历史授权、动作最终效果或整体实机验收。

## 修复后容量 B/C Mock 复测

执行 SHA `94427140777b76e245f6f400a193de1c35830cef`；参与运行的主程序/压测脚本首尾哈希一致。隔离包装器只在全新 TEMP 写入和连接数据库，禁止读取 `.env`、非回环网络；AI 只到本地 Mock。

令 `<T>` 为 `C:/Users/81596/AppData/Local/Temp/qqbot-capacity-loadtest-20260921-235218-cf807eaa`，实际命令：

```powershell
& .venv/Scripts/python.exe -B <T>/run_isolated.py --repo "D:/CodeBuddy工作空间/CB 项目/qq-group-moderation-bot" --out <T>/B-current --rate 2 --label B-current
& .venv/Scripts/python.exe -B <T>/run_isolated.py --repo "D:/CodeBuddy工作空间/CB 项目/qq-group-moderation-bot" --out <T>/C-current --rate 4 --label C-current
```

当前参数为 10 worker、600 AI/min、50000/day；每轮 150 秒。B 实际注入/处理 301/301，C 为 600/600；唯一文本、唯一 AI 缓存键、Mock 次数均分别等于处理数，零缓存、零限流，排空后零积压，动作和通知零发送，quick_check=ok。每 15 秒采样的积压峰为 7/14，p95 为 6.3/6.1 秒，排空约 5 秒。不能把采样峰值写成连续监测最大值，不能把 B 的实际 301 写成理论 300。

旧 B 是不同 worker/限流参数，本次不追认旧报告。此证据关闭当前参数下“唯一文本修复后 B/C 未重跑”；不证明真实供应商、多媒体、违规案件、多实例或真实百群容量。包装器只在 TEMP，不是仓库维护工具或主审探针。脱敏原件摘要见本记录附带 JSON。

## 合成库恢复演练

执行固定源码 `d2a4f6b22b7b1629a4a08b64f7b355d604e5a06d`（git archive 到 TEMP）。命令：

```powershell
& .venv/Scripts/python.exe -B C:/Users/81596/AppData/Local/Temp/qqbot-isolated-recovery-b3b56e57fe34/drill.py
```

真实迁移链创建合成库，revision `d4b7c1e9a502`。WAL 中已提交行在“只复制主文件”对照中缺失，而项目 online backup 保留；恢复到新目录后文件/逻辑内容一致，integrity_check=ok，foreign_key_check 无异常。真实 Uvicorn 在独立回环端口启动，/healthz HTTP 200；读取合成成员白名单、群设置、案件、幂等及动作状态。过期 PROCESSING 事件变 FAILED，已有 SUCCEEDED/UNKNOWN 动作保留不重放。

AI、OneBot、动作、通知关闭，没有启动官方 runtime 或计划任务；没有读取生产 `.env`、生产库或媒体。最终证据轮使用 Windows Selector 事件循环以记录本地 socket 审计。这只能补充合成库恢复/启动证据，真实备份、媒体/凭据恢复、异机备份、断网断电和生产回滚均未关闭。

## N03 与验证进度

### 2026-09-23 处置清单补查（只读，未批准删除）

负责人同意优先补齐处置清单和校园墙线上核验。本次执行 SHA `6ecb6d28e38b64b597c95edfb1c312f4cd33a049`；实际 `.env` 仅核对 `RAW_RETENTION_DAYS` / `DECISION_RETENTION_DAYS` 数字项，均为 15，与 D-024/D-026 一致，不能用模板 30/180 默认值替代。文件值本身不证明当前进程已加载值，本轮未改配置。

仓库根执行 `.venv/Scripts/python.exe -B scripts/retention_audit.py --db data/moderation.db --data-root data --days 15 --evidence-root docs/evidence/image-review --evidence-root docs/evidence/allowlist-samples --as-of <收据内固定UTC> --out D:/QQBotAudits/n03-campus-20260922/retention-inventory.json`；随后 `.venv/Scripts/python.exe -B D:/QQBotAudits/n03-campus-20260922/build_retention_review.py`。完整实参、脚本及报告摘要见 [脱敏收据](evidence/core-closeout-20260922/retention-review-6ecb6d2.json)，逐文件私有路径/摘要/时间依据和审核说明位于该 D 盘目录。私有脚本不是入库测试；其分类与冲突保护自检通过，不计入主仓库用例数。

本轮声明根内盘点 11872 项：媒体 11257、data/backups 21、sample_pool 59、审核证据 527、演练副本 8。媒体中 8819 项的直接/精确关联来源时间仍在窗口内；1847 项缺可靠源时间，591 项无直接来源引用，合计 2438 项继续保留待审核。没有发现具备完整到期依据的当前媒体，所有项仍为 `safe_to_delete=false`。没有用 mtime 或最早引用过期代替所有引用期限；混合到期、冲突、非法时间和不完整身份不能升级为可删。

已流式核验新 E 盘 plain 成功快照 2 份、不同对象 2589 个，其中 2576 个被两个快照共用；2438 项未知/无直接引用媒体均有同字节备份。样本池 52 个文件与当前媒体同字节，这只能证明副本关系，不能声明已脱敏。旧 `data/backups`（含嵌套已知 DB）、E 盘顶层 DB 和 plain 快照数据库共 15 个检查对象，14 个完成来源元数据投影，1 个因旧 schema/读取边界未完成；这些旧库没有为当前未知来源新增可靠时间。未检查所有部署副本、旧加密存储及每个容器内的原文字段，不能把本轮称为全副本期限已核清。

**工程缺口仍在**：现有媒体/副本清理主要使用 mtime，备份 manifest 没有固定源时间/到期字段。对象跨快照复用，不能单删 `.blob`；应先建立原消息期限记录、明确历史未知项处置口径，生成并验证合规替代快照，再审核旧快照与无引用对象清单。备份可恢复不等于保留期限合规，本轮清单不是可执行删除计划。

审计初稿未被采信：修正了历史时间覆盖冲突/不完整身份、凭据备份摘要范围，以及重复展开共享对象导致输出膨胀的问题。中断输出已在 D 盘无损压缩保留，原大文件已移除；最终脚本拒绝读取 `.env*`/旧密钥，只统计此类文件元数据。初稿可能已计算旧凭据副本摘要，但未输出凭据内容。SQLite 只读查询可能更新 SHM 锁记账，报告只承诺无逻辑 SQL 写入，不声称文件系统每个字节未变。未运行清理、未发送消息、未改生产开关或服务；没有创建完整恢复副本。

以下为旧轮次历史观察，不覆盖本节的新清单。

已新增独立 `scripts/retention_audit.py` 与 `tests/test_retention_audit.py`，没有被维护任务/下载器/备份入口导入。只有显式只读 CLI，无删除参数。只投影源时间和媒体引用、不读取文件内容；固定目录、链接边界、查询和扫描上限、独占发布均有内部回归。所有文件均 `safe_to_delete=false`，整个报告 `n03_closed=false`。

执行 SHA `e41368d0bac5469497ad187354683beda0b5d429`，实际命令及报告哈希见 [retention-summary.json](evidence/core-closeout-20260922/retention-summary.json)。观测 UTC `2026-09-21T16:07:44.788898Z`：源记录 29449，其中时间已知 24512、缺失 4937；文件元数据 9614 项，其中媒体 9005、备份 15、sample_pool 59、审核证据 527、隔离演练副本 8。媒体中 6483 项来源时间已知且在窗口内、1931 项存在未知来源时间、591 项无来源引用。不可关联文件引用 4353 包括空文件名等，不据此声称数据库损坏。

这次没有可直接授权删除的清单；未知源时间不能用 mtime 补造，备份内部原文也未检查。下一步需建立后续副本固定源期限、历史未知项处置口径及合规恢复副本，再交负责人审核真实处置清单。文件系统是运行中的有界观察，非原子快照；普通 SQLite 只读事务也不代表 SHM 锁记账字节完全不动。

同一执行 SHA 的 `uv run ruff check app tests alembic scripts`、`uv run ruff format --check app tests alembic scripts`、`uv run mypy app` 已通过（格式 348 文件、类型 111 文件）。全量命令 `uv run pytest --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-core-closeout-20260922/full.xml`：2214 项，2197 passed、17 skipped、0 failed/error；主审子集 31 文件/262 项全通过。汇总 [verification-summary.json](evidence/core-closeout-20260922/verification-summary.json)，日志保存在同一 TEMP 目录。后续文档提交不改变执行源码；最终 CI 仍需按推送 HEAD 核验。

## 最终补正与交付边界

执行源码 `59d15eceff32df54520d214b04bd0dc9647c7f63`：N03 预检把空文件引用与非法引用分列，排除观察时间之后的处理记录并单独计数。六项新增回归已入库。全量命令 `uv run pytest --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-core-closeout-20260922/final-main.xml` 得到 **2221 项：2204 passed、17 skipped、0 failed/error**；其中原契约主审探针 262 项全通过。跳过分别为运行中生产锁保护 13 项、符号链接不可用 3 项、本地真实图片样本未挂载 1 项，不能统称 Windows 差异。同 SHA 的 `uv run ruff check app tests alembic scripts`、`uv run ruff format --check app tests alembic scripts`（348 文件）、`uv run mypy app`（111 文件）通过。精确日志哈希/跳过节点见 [最终本机验证](evidence/core-closeout-20260922/verification-59d15ec.json)。测试集合也包含此前独立巡检任务已提交的边界回归；不把它计作本轮主项目功能。

该 SHA 的只读预检命令及聚合原件见 [最终保留期观察](evidence/core-closeout-20260922/retention-summary-59d15ec.json)：UTC 2026-09-21T16:44:22.331094Z，源记录 29487，时间缺失 4937；空引用 4353、非法引用 0。文件元数据 9619 项，其中媒体 9010、备份 15、样本池 59、审核证据 527、隔离副本 8。媒体中来源时间已知且窗口内 6488、未知 1931、无引用 591。仍全部禁止直接删除。

追加只读关联核查执行 `adef6ee558be3ba9d53de7111c6eecfd21a74ef9`，完整命令及结果见 [关联观察](evidence/core-closeout-20260922/retention-reconciliation-adef6ee.json)。原清单的 2522 项文件元数据未变化，其中 84 项可从同 provider/group/member/external-message 的记录补证源时间，且仍在保留窗口内；剩余 2438 项不能确认源期限。该关联未读取文件内容，不能证明文件字节身份，也未回填生产库。历史入口中部分原始 payload 已清除，不能用 mtime 代替原始时间。备份内部内容与可替代恢复副本尚未完成核验，**N03 未关闭、没有获批的删除清单**。

此前 `adef6ee` 的 CI [35624097841](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35624097841) 已核对成功：实际 PR 合并检出 `6e6a01a0059ecea061170b5fcd478ecffdc3c0a2`，Ubuntu/Windows 的 `uv run pytest` 均 2211 passed、3 skipped，干净运行依赖任务通过。原始 jobId/命令/日志哈希见 [CI 摘要](evidence/core-closeout-20260922/ci-35624097841-summary.json)。这不替代后续 HEAD 的 CI，交接时应检查对应 PR 最新 Checks。

方案 A 已按负责人裁定实现为独立候选 `codex/r132-authority-20260922`，源码冻结 `7456d40b61f969c41662665a971dee8c71bdfb40`；验证记录在该分支 `docs/2026-09-22-authority-a2-validation.md`。生产工作分支保持旧 schema，不能提前合入新 migration 后让现有服务自动重启。候选只允许评审与 CI，维护窗口之前不合并、不生产迁移、不部署。

仍未关闭：Windows 整机故障/恢复与真实备份恢复（负责人决定待排期）；历史单项失败原始 nodeid/trace/命令/执行 SHA 缺失（未证明原因或修复）；真实群卡片事件原文已不在可用 payload 中（历史命中不等于真实样本验收）；N03 全副本源期限与最终原件处置。通知能力保留但不启用。运行中的生产主程序仍沿用 MEDIA-QUOTA 的加载基线，不因本轮离线工具改动重启；enforce、判断阈值、群授权与动作开关均未调整。
