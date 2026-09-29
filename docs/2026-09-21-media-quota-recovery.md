# 媒体配额耗尽与恢复（MEDIA-QUOTA-20260921）

负责人反馈正在运行的机器人图片全部转人工，随后明确同意扩容至 20 GiB、增加容量预警和具体失败原因。授权用于保留证据的容量修复与服务加载；不改变判定、群授权、动作开关、保留期或执行数据库迁移。

## 原因与范围

诊断执行 SHA `7990a9184dbd8de6acf2466e2ee7c2724b201283`，命令：

```powershell
uv run python C:/Users/81596/AppData/Local/Temp/qqbot-media-quota-20260921/diagnose.py
```

UTC `2026-09-21T11:21:32.992770+00:00`：媒体目录顶层 8892 文件、2147483567 字节，硬编码配额 2147483648 字节，仅剩 81 字节；D 盘可用 747213746176 字节。现有下载器读取实际目录容量、使用合成 1024 字节响应，返回“磁盘配额不足，剩余81字节”。未请求真实 CDN 或写入生产媒体。

截图对应 UTC `11:18:28.137771` 的记录媒体文件名为空、AI 结果为空。最新图片查询窗口 UTC `10:14:01.701719` 至 `11:20:42.082007` 的 30 条均为媒体缺失。逐消息历史记录未保存下载细分错误，不把当前容量复现当成每条历史下载原因的完整证明。

当前文件配置原始保留期为 15 天；最早媒体修改时间为 `2026-09-08T12:02:33.308393+00:00`，尚未到期。问题是容量预算与实际积累不匹配，不能通过立即执行过期清理释放这些尚未到期的证据。诊断原件见上述 TEMP 目录 `diagnosis.json`、`诊断结论.md`。

服务正常退出后日志缓冲刷出，按实际 Windows 日志编码复核到截图同一时刻 UTC `11:18:28.104987` 的下载日志：“附件下载失败：磁盘配额不足，剩余81字节”。脱敏摘录 `screenshot-download-log.json`。这补充了最初只读快照时尚未取得的直接原因；没有改写旧诊断快照。

## 修复

- 新增 `MEDIA_QUOTA_BYTES`，默认保持 2 GiB，必须为正整数；本机按负责人授权配置 20 GiB。下载器默认从已加载配置取值，显式传入的隔离测试配额仍有效。单文件上限、URL/DNS 防护与配额锁不变。
- 后台案件页和影子判定列表显示已用容量、配置上限和磁盘可用空间；达到配额的 80% 显示容量预警。`/healthz.media_storage` 提供 `state/used_bytes/quota_bytes/remaining_bytes/disk_free_bytes`；目录不可读返回 `unknown`，不伪报容量正常。容量检测在独立线程执行，不等待媒体计算串行门闩。
- 两个下载入口保存经过白名单规范化的下载错误类别；判定详情显示容量不足、安全检查拒绝、超时、连接失败、单文件超限或写入失败。未知原始内容/URL 不作为诊断文本落库或展示。此字段只用于运维诊断，不参与判定或动作。
- 统计目录容量时容忍并发清理/重命名导致的单文件消失。没有提前删除媒体、缩短保留期、清除历史判定或自动重罚旧消息。

代码提交 `8e7e7d9`；内部回归提交与本机执行 SHA `d7ceaf320a22599595ff0aca0200fb7b8f2afc77`。新增测试是内部回归，不是主审探针，未修改 `tests/test_r132_review_*` 或历史证据。

## 本机验证

证据目录 `C:/Users/81596/AppData/Local/Temp/qqbot-media-quota-20260921/`。执行 SHA `d7ceaf320a22599595ff0aca0200fb7b8f2afc77`，源代码与测试无未提交修改，待提交文档不影响检查：

```powershell
uv run ruff check app tests alembic scripts
uv run ruff format --check app tests alembic scripts
uv run mypy app
uv run pytest -q --no-header --tb=short --junitxml=C:/Users/81596/AppData/Local/Temp/qqbot-media-quota-20260921/full-final.xml
```

门禁通过：格式 329 文件、类型 101 文件。输出分别保存 `ruff-check.log`、`ruff-format.log`、`mypy.log`。全量输出重定向 `full-final.log`：1997 项，1981 passed、16 skipped、0 failed、0 error，退出码 0；同份 JUnit 的主审子集 31 文件/262 项全部通过，复算汇总 `full-summary.json`。初次失败复现覆盖“配置字段不存在、低配额仍可下载、容量状态不存在、下载原因丢失”；OneBot 测试首次误读 fixture 包装，修正为读取 `event` 后复现 `_media_download_errors` 缺失，未降低业务断言。

## 生产操作与回退

执行 SHA `d7ceaf320a22599595ff0aca0200fb7b8f2afc77`，命令 `uv run python C:/Users/81596/AppData/Local/Temp/qqbot-media-quota-20260921/backup-before-deploy.py`：UTC `11:27:59.070249` 完成 SQLite 在线一致性备份，`data/backups/moderation-20260921T112758Z-ydswqo00.db`，96874496 字节，`quick_check=ok`。仅备份，没有恢复或迁移数据库。

执行 SHA 同上，UTC `11:30:49.390421`，只追加 `MEDIA_QUOTA_BYTES=21474836480`，验证 `.env` 原有全部字节保留；无服务环境的同名覆盖，`config-before.json/config-after.json` 记录非敏感设置及摘要。未复制或记录其他凭据。

随后通过 Windows 系统管理员授权执行维护脚本 `.../qqbot-media-quota-20260921/reload-services.ps1`，仅正常停止/启动 `QQBotWeb` 与 `QQBotRuntime`，不控制 QQ/NapCat、群授权或动作模式。UTC `11:30:57.3246346` 完成，NSSM 服务 PID 分别由 `3988/3972` 变为 `23924/25336`，均 Running，详见 `service-reload.json`。服务重新加载同时加载此前已验证的分页和稳定性代码，手机巡检模块没有被服务入口导入。

已从运行中 `/healthz.media_storage` 读取 `quota_bytes=21474836480`、`state=ok`，证明新容量配置实际加载。重连与新消息验证另行记录，不能只据服务 Running 宣告业务恢复。重启短暂断开接入；未入库消息不保证补齐，不主动补罚历史失败图片。异常时优先保留证据并检查服务启动原因；需要代码回退时仅撤销本批代码并正常重启，不恢复数据库或删除新证据。旧容量会再次阻断下载，不能把回到旧版本当作业务恢复。

## 实际恢复验证

验证脚本执行 SHA `3b10021c06dcef24e3efb480357793126edb12e1`，生产服务加载基线仍为 `d7ceaf3`（二者仅独立巡检 GUI 平台守卫不同，主服务代码一致）。命令：

```powershell
uv run python C:/Users/81596/AppData/Local/Temp/qqbot-media-quota-20260921/verify-live.py
uv run python C:/Users/81596/AppData/Local/Temp/qqbot-media-quota-20260921/check-live-ui.py
```

`verify-live.py` 于 UTC `11:36:29.251686` 核实 OneBot `ready/connected=true`、媒体容量 20 GiB、群设置与动作所有者及政策配置摘要和维护前一致。自然到达的新图片记录 `26600`，UTC `11:35:37.498893`：媒体原件存在、视觉结果 1 条、下载错误为空、判定 `violation_high`；对应撤回意图 `5682` 为 `SUCCEEDED`。这是系统记录的实际撤回成功，不冒充人工在 QQ 客户端逐条核对；仅证明这个新消息样本，不宣称所有旧失败消息已经补处理。

UI 检查实际先在执行 SHA `d7ceaf3`、UTC `11:32:33.066800` 进行，登录凭据只在内存中使用、结束注销：案件/影子判定显示新容量；群管理、成员白名单、报告和首页待审清单存在分页组件，HTTP 均 200。保存的是布尔验证结果 `live-ui.json`，不是含成员数据、会话令牌的整页 HTML。新消息证明保存在 `live-recovery.json`。

首推 [CI 35594755643](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35594755643) source head `d7ceaf3`：干净运行依赖 job `106316921567` 成功；Ubuntu job `106316921859` 在独立巡检 GUI 的 `os.startfile` 类型检查失败，尚未运行 pytest。未放松门禁，已用 `sys.platform == 'win32'` 明确 Windows API 作用域（`3b10021`），执行 `uv run mypy --platform linux app` 与 `uv run mypy app` 均通过。此补丁不被生产服务导入，不需要再次重启。本批最终 CI 仍需按最终 HEAD 单独核对。

CI 与最终交接使用 `gh run list --commit <完整HEAD>`、`gh run view <runId> --json headSha,status,conclusion,attempt,jobs` 核实；最终固定 run 链接在交接回复提供，不为把文档自己的 SHA 写回文档无限触发 CI。手机巡检仍处于有界实机验收阶段，不因本次全量测试通过宣称全群巡检完成。
