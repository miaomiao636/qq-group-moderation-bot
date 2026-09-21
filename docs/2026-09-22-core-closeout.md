# 主项目收尾 CORE-CLOSEOUT-20260922

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

已新增独立 `scripts/retention_audit.py` 与 `tests/test_retention_audit.py`，没有被维护任务/下载器/备份入口导入。只有显式只读 CLI，无删除参数。只投影源时间和媒体引用、不读取文件内容；固定目录、链接边界、查询和扫描上限、独占发布均有内部回归。所有文件均 `safe_to_delete=false`，整个报告 `n03_closed=false`。

执行 SHA `e41368d0bac5469497ad187354683beda0b5d429`，实际命令及报告哈希见 [retention-summary.json](evidence/core-closeout-20260922/retention-summary.json)。观测 UTC `2026-09-21T16:07:44.788898Z`：源记录 29449，其中时间已知 24512、缺失 4937；文件元数据 9614 项，其中媒体 9005、备份 15、sample_pool 59、审核证据 527、隔离演练副本 8。媒体中 6483 项来源时间已知且在窗口内、1931 项存在未知来源时间、591 项无来源引用。不可关联文件引用 4353 包括空文件名等，不据此声称数据库损坏。

这次没有可直接授权删除的清单；未知源时间不能用 mtime 补造，备份内部原文也未检查。下一步需建立后续副本固定源期限、历史未知项处置口径及合规恢复副本，再交负责人审核真实处置清单。文件系统是运行中的有界观察，非原子快照；普通 SQLite 只读事务也不代表 SHM 锁记账字节完全不动。

同一执行 SHA 的 `uv run ruff check app tests alembic scripts`、`uv run ruff format --check app tests alembic scripts`、`uv run mypy app` 已通过（格式 348 文件、类型 111 文件）。全量命令 `uv run pytest --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-core-closeout-20260922/full.xml`：2214 项，2197 passed、17 skipped、0 failed/error；主审子集 31 文件/262 项全通过。汇总 [verification-summary.json](evidence/core-closeout-20260922/verification-summary.json)，日志保存在同一 TEMP 目录。后续文档提交不改变执行源码；最终 CI 仍需按推送 HEAD 核验。
