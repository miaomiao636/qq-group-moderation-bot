# DAILY-BACKUP-20260922：每日运行数据备份（不加密、排除服务凭据）

负责人已批准定时备份，并在完整运行数据方案基础上明确选择“不加密，排除密码和 Token；换机时重新填写”。以下是当前方案，替代本轮先前的加密计划。旧加密试验快照和密钥原位保留，不删除。

## 范围与边界

每份新快照包含在线一致的 SQLite 数据库、data/media/、data/sample_pool/、config/ai_prompt_rules.txt、公开配置恢复模板 .env.recovery-template，以及执行 SHA 对应的源码 ZIP（含依赖锁和安装脚本）。数据库保留审核决定、群设置、案件、动作幂等、inbox 等完整业务表；A2 数据库仍是审核决定权威。

原始 .env 整份排除；模板只收录显式白名单中的非凭据配置，密码、API Key、OneBot Token、邮件授权码和心跳能力 URL 不进入模板。未知配置默认排除；含用户名、密码、查询或片段的服务 URL 也排除。源码 ZIP 拒绝已跟踪的真实 .env 和未审核 config 文件。换机必须从 .env.example 补填服务凭据并核对开关，模板不能直接当作完整运行配置。

不打包旧备份、服务日志、历史演练输出、下载临时文件（.part/.partial/.lock）、临时视频帧 _frames 或外部 QQ/NapCat 登录会话。已提交的通用附件 .bin 正常纳入。业务消息和原始图片按证据保留，不承诺清除用户内容中的所有敏感字符串。外部桥接需重新配置并人工登录；这不是整机镜像。

数据库快照先完成，再枚举媒体。manifest 记录采集时间与媒体引用对账，历史缺失原件、无效引用单独计数。成功表示采集范围已读回验证，不表示历史缺失已恢复，也不是数据库与媒体跨文件系统的原子快照。

## 保存与调度

新目录 E:/qqbot-backups/daily-plain-v1，状态配置 C:/ProgramData/QQBotBackupPlain/config.json；新模式不生成或读取恢复密钥。Windows 任务 QQBotDailyBackup 计划每日当地时间 04:30，以 SYSTEM 运行，错过时间补跑、重复实例忽略，硬上限 40 分钟；程序流式操作期限 30 分钟。若 QQBotAutoCleanup 正在运行或无法确认其状态，本次失败留证，不终止清理或机器人服务。

默认备份总上限 100 GiB、输出盘保留空闲 5 GiB，可调整私有配置 max_bytes、min_free_bytes；不是预分配空间。达到限制后停止本次，保留最后成功备份；不自动删除旧快照。保留期清理仍须 N03 准确清单和负责人授权。

对象以内容 SHA256 去重，文件为明文 .blob，清单为 JSON；通过 restore 命令还原原始目录和文件名，不能只复制清单当作完整备份。对象、manifest 与成功收据进行摘要和大小核验，能发现损坏，不能声称抵御同时改写清单和收据的攻击者。目录仍限定负责人、SYSTEM、Administrators。仅有 snapshots 文件不代表成功，必须有对应成功 attempts 收据。

## 初始化、状态与恢复

仅在全新目录初始化一次；从已安装且验证的仓库根运行。实际执行必须记录 SHA 和命令。

~~~powershell
.venv\Scripts\python.exe -m app.reports.scheduled_backup init --repo . --destination E:\qqbot-backups\daily-plain-v1 --state C:\ProgramData\QQBotBackupPlain --mode plain
# 管理员 PowerShell；仅创建新任务，已有同名任务拒绝覆盖
.\scripts\register_backup_task.ps1 -ProjectRoot (Get-Location).Path -ConfigPath C:\ProgramData\QQBotBackupPlain\config.json
.venv\Scripts\python.exe -m app.reports.scheduled_backup status --config C:\ProgramData\QQBotBackupPlain\config.json
~~~

手工验收触发 Start-ScheduledTask -TaskName QQBotDailyBackup，核对 Get-ScheduledTaskInfo 的 LastTaskResult、state/last_attempt.json 与 E 盘 last_success.json。不能用注册成功替代实际运行。本轮按负责人要求保留通知渠道但不配置或发送 QQ/邮件通知。

恢复必须使用全新、私有且与生产/备份/state 不重叠的目录：

~~~powershell
.venv\Scripts\python.exe -m app.reports.scheduled_backup restore --config C:\ProgramData\QQBotBackupPlain\config.json --snapshot <成功收据中的ID> --to C:\QQBotRestore-新目录
~~~

每日备份自动流式读回全部对象，只把数据库落地到私有临时目录，检查 integrity_check、foreign_key_check、精确 Alembic revision 和 A2 决定。显式 restore 才还原完整文件；仅 RESTORE_VERIFIED.json 表示完整成功。对象读取/还原失败写 RESTORE_FAILED.json；更早的前置检查失败可能只留下空目录，均不能投入运行。

恢复不运行 ZIP、服务或迁移，不覆盖生产库，保留 UNKNOWN 动作和 PROCESSING inbox 原值。正式切换恢复库需要维护窗口、未知动作人工核对和消息幂等检查。换机需设置路径、负责人 SID、服务凭据和 QQ 登录；新备份不需要恢复密钥。后续迁移必须同步审核 expected_revision，否则备份按设计拒绝不匹配版本。

## 验证记录与当前状态

新 plain 模式的测试、CI、真实 SYSTEM 调度及完整隔离恢复正在验收；尚不声称定时备份已启用。冻结源码和实际收据验收后更新本节。

先前加密试验（历史，不作为新 plain 模式验收）：
- 执行源码 aaa4159514bc087f1f98ca956a2d07e0aef87f7b；uv run --locked pytest --junitxml=<私有证据>/full.xml：2510 passed、6 skipped；ruff check、format --check app tests alembic 和 mypy app 通过。私有记录 full-command.json、gates.json。
- [源码 CI 35733313210](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35733313210)：PR head aaa4159，实际合并 checkout 6636cf0ee5e33591a9e6afeefc9056b17439a034；uv run pytest，Ubuntu 2510 passed/6 skipped、Windows 2512 passed/4 skipped，runtime-deps 通过。
- 同源码私有 candidate_backup.py 调用 run_backup（只读采集生产9709e1d），快照 20260922T132659Z-4021a650635d42678f1a81e1e7b1ea1d，11236 文件，缺失引用 0；使用 restore --config C:/ProgramData/QQBotBackup/config.json --snapshot <上述ID> --to C:/ProgramData/QQBotRestoreCheck-20260922 完整还原验证成功。
- 旧试验位于 E:/qqbot-backups/daily-v1；仅这份旧加密试验仍需 C:/ProgramData/QQBotBackup/recovery.key。旧快照、密钥和隔离恢复材料保留，不要求为新备份保存密钥。异机恢复与整机故障演练未验收。

本轮不重启 Web/Runtime、不迁移库、不改急停、动作、群授权、成员白名单或通知接收目标。实施前主服务加载审核源码 1c17ba0，实时急停 false；此前 true 是历史部署快照，本轮保持实值。Windows 整机演练、N03 删除处置及校园墙自然配对完整验收分别仍待办。
