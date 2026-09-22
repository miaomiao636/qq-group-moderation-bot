# DAILY-BACKUP-20260922：每日完整运行数据备份

负责人已批准继续收尾，并选择“完整运行数据备份”。本轮仅增加独立备份程序、Windows 调度与操作说明；不重启 Web/Runtime，不迁移生产库，不更改群授权、急停、动作或通知渠道，不删除原件和旧备份。

## 范围与边界

每份快照包含在线一致的 SQLite 数据库、.env、config/、data/media/、data/sample_pool/，以及执行 SHA 对应的源码 ZIP（含依赖锁和安装脚本）。数据库包含审核决定、群设置、案件、动作幂等、inbox 等完整表。JSON 审核决定导出可从 A2 数据库重建，不替代数据库权威。

不打包旧备份、服务日志、历史演练输出、下载临时文件和临时视频帧，也不打包外部 QQ/NapCat 安装、设备或登录会话。换机必须按原安装说明重新配置外部桥接并人工登录；本功能不是整机镜像。旧历史材料原位保留，未删除。

数据库快照先完成，再枚举媒体。快照里的媒体引用与采集文件对账：缺失原件、无法解释的引用分别计数，详情仅在加密 manifest。成功备份表示采集范围内的数据已加密并读回验证，不表示历史缺失原件已恢复。数据库和媒体不是跨文件系统的同一时刻原子快照；manifest 保存采集起止时间。

## 保存与调度

计划使用 E:/qqbot-backups/daily-v1，独立于现有 data/backups 的自动清理。Windows 任务 QQBotDailyBackup 每日当地时间 04:30，以 SYSTEM 后台运行、错过时间补跑、重复实例忽略，硬执行上限 40 分钟。Python 流式读写有 30 分钟期限检查；部分数据库/系统调用还各有独立限时。任务运行前若 QQBotAutoCleanup 正在运行或无法查询其状态，则失败留证；不会为了备份终止清理或机器人服务。

默认备份存储上限 100 GiB、各输出盘保留空闲 5 GiB，可在私有 config.json 中调整 max_bytes、min_free_bytes。不是预分配磁盘。达到上限或空间不足后，本次停止，最近成功备份保持；本轮不自动清除旧快照。日后保留期清理须遵守 N03 的准确清单和删除授权，不能把备份能力写成保留期已收口。

每个对象使用 AES-256-GCM 加密，相同内容在同一密钥下复用既有对象；已有对象也要认证校验。对象名为带密钥的摘要，manifest 同样加密。参考 [cryptography 的 GCM 文档](https://cryptography.io/en/stable/hazmat/primitives/symmetric-encryption/)。失败不会替换 last_success.json；仅凭 snapshots 下存在文件不能判定成功，还必须有同 ID 的成功 attempts 收据。

目录与密钥仅允许安装负责人、SYSTEM、Administrators。安装时固定负责人 SID，避免 SYSTEM 后台创建内容后负责人无法恢复。恢复密钥与 E 盘密文分开放在 C:/ProgramData/QQBotBackup/recovery.key，严禁发到聊天、Git 或日志。

**负责人还需将 recovery.key 单独复制到安全的离线介质或受控保管处**。同机 C 盘密钥并非异机保管；若密钥和系统盘同时损坏，E 盘密文无法恢复。不得把本轮能力称为整机灾难恢复验收通过。

## 初始化、状态与恢复

仅在全新私有目录初始化一次。以下命令在已安装且已验证的源码仓库根运行；实际执行记录与源码 SHA 必须另行留证。

~~~powershell
.venv\Scripts\python.exe -m app.reports.scheduled_backup init --repo . --destination E:\qqbot-backups\daily-v1 --state C:\ProgramData\QQBotBackup
# 管理员 PowerShell：只创建新的 QQBotDailyBackup；已有同名任务将拒绝覆盖
.\scripts\register_backup_task.ps1 -ProjectRoot (Get-Location).Path -ConfigPath C:\ProgramData\QQBotBackup\config.json
.venv\Scripts\python.exe -m app.reports.scheduled_backup status --config C:\ProgramData\QQBotBackup\config.json
~~~

手工触发同一计划任务可用 Start-ScheduledTask -TaskName QQBotDailyBackup；需核对 Get-ScheduledTaskInfo 的 LastTaskResult、state/last_attempt.json 与 E 盘 last_success.json，不能用“注册成功”替代实际运行证据。本轮不启用 QQ/邮件通知；失败应通过任务与状态收据检查。

恢复只允许全新、私有、与生产/备份/state 不重叠的目录：

~~~powershell
.venv\Scripts\python.exe -m app.reports.scheduled_backup restore --config C:\ProgramData\QQBotBackup\config.json --snapshot <成功收据中的ID> --to D:\QQBotRestore-新目录
~~~

每次日常备份自动流式解密并核对全部对象，只把数据库落地到私有临时目录，检查 integrity_check、foreign_key_check、精确 Alembic revision 和 A2 决定。显式 restore 才会落地完整文件，先检查目标空间，成功写 RESTORE_VERIFIED.json；失败材料会标 RESTORE_FAILED.json，不能投入运行。

恢复不运行 ZIP、Web、Runtime 或迁移，不覆盖生产库，保留 UNKNOWN 动作和 PROCESSING inbox 原值。实际切换恢复库必须另行安排维护窗口，核对新增案件/消息、未知动作人工复核、幂等和 QQ 登录，不能直接覆盖后重启或重放。换机时需恢复密钥、重新设置私有目录与负责人 SID，并更新 config.json 中路径。后续数据库迁移时同步审核 expected_revision，否则备份按设计拒绝不匹配版本。

## 本轮状态与验收证据

实现及合成回归进行中；尚未注册生产计划任务，未声称已生成真实完整加密备份。本段将以冻结源码 SHA、命令、实际结果和部署收据更新。真实整机故障/断电恢复、异机密钥保管、N03 删除处置、校园墙自然配对完整验收仍分别待办。

实施前只读观察：Web/Runtime 仍为原进程，已加载审核源码 1c17ba0；急停当前为 false，与此前部署时 true 的历史快照不同。本轮保持实值，没有执行解除或启用急停。状态应以每次实时核验为准。

