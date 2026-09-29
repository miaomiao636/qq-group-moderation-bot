# 部署与回滚手册：D-037 成员白名单 + D-038 合并转发/群名片撤回

日期：2026-09-18（**2026-09-18 晚修订：已提交/已部署版本的回滚流程**）
适用环境：本机 Windows + NSSM 服务方式（`QQBotWeb` / `QQBotRuntime`）
目标版本：**`84a8b47`（已提交、已部署）**；数据库迁移 `c9a1f4d27e30`（已应用）
状态：**已部署**。本手册是操作步骤与回滚细则，不构成任何验收结论。

> **主审 F06 修订说明**：本手册初版写于"改动仍未提交"时，其回滚章节使用的
> `git stash push -u` / `git checkout -- <paths>` **对已提交内容无效**（stash 不会撤销已提交的
> 代码，`checkout --` 不带旧 ref 只会恢复当前版本），会留下"代码 head ≠ 数据库版本"。
> §3 已按**已提交/已部署版本**重写；下文若仍提到"未提交/未跟踪文件"字样，均属**历史记录**，
> 不再作为操作依据。

---

## 0. 部署前基线（2026-09-18 只读核对；当前值见括号）

| 项目 | 实测值 | 说明 |
| --- | --- | --- |
| Git 版本 | `84a8b47`（已提交） | 部署前为 `756e4d2` + 未提交改动；现全部入库 |
| 服务 `QQBotWeb` | `STATE : 4 RUNNING` | NSSM 注册；`python -m app`（后台 + OneBot 反向 WS + 通知） |
| 服务 `QQBotRuntime` | `STATE : 4 RUNNING` | `python -m app.runtime`（常驻影子 runner） |
| 服务的 AppDirectory | 本仓库目录 | **服务直接跑在工作树上**，切版本/重启即生效 |
| 数据库 | `data/moderation.db` | 部署前 `alembic current` = `b8d4f2a05e31`；**现为 `c9a1f4d27e30`（已迁移）** |
| 代码迁移 head | `c9a1f4d27e30` | 部署前比数据库新一个版本；**现两者相等** |
| `ACTION_MODE` | `OFFICIAL` | 全局真实动作门禁名称（D-022） |
| `ONEBOT_ACTIONS_ENABLED` | `true` | NapCat 独立开关**已开启** |
| `EMERGENCY_STOP` | `false` | 环境级急停未开启 |
| 每日清理任务 | `QQBotAutoCleanup`，每天 04:00 | `python -m app.reports.maintenance cleanup` |
| 备份入口 | 后台「设置」页 →「备份数据库」 | `POST /admin/settings/backup` → `data/backups/` |

---

## 1. 现在就存在的风险（开工前必读）

因为新代码**已经落在服务的工作目录**里，而数据库还没迁移：

1. **重启 `QQBotWeb` 会启动失败。** `app/main.py` 启动时即校验"数据库版本 == 代码 head"（`check_db_migrated()`），现在两者不等 → 服务起不来 → **后台、OneBot 反向 WS、通知同时中断**（NapCat 推不进来，审核链路整体停摆）。
   - `QQBotRuntime` **不校验**迁移，能正常启动；成员白名单在新表不存在时按空集处理（fail-closed），不会崩。
2. **今晚 04:00 的 `QQBotAutoCleanup` 会失败。** 已在临时库上实测：新代码 + 旧迁移版本运行时输出
   `{"event":"maintenance_cleanup","status":"failed","error":"startup_or_metadata_failed"}`，退出码 1，**不执行清理、不删除任何数据**（失败是"跳过"，不是"误删"）。
3. **结论（二选一，不要停在中间态）：**
   - **A：尽快完成第 2 节部署**（推荐，改动已在盘上，窗口很短）；
   - **B：先把改动移出工作目录**：`git stash push -u -m "d037-d038-pending"`，
     部署时再 `git stash pop`。**必须带 `-u`**——新增的迁移脚本是"未跟踪文件"，
     不带 `-u` 的话它仍留在盘上，"代码 head"依然是新版本，重启照样起不来。

> **一致性铁律**：数据库版本必须等于"代码 head"，而 head 由 `alembic/versions/`
> 目录里的文件决定，**包含未跟踪的新迁移脚本**（本机实测：当前 `get_head_revision()`
> = `c9a1f4d27e30`，正是这个未跟踪文件造成的）。任何回滚后都要核对两者相等：

```powershell
uv run alembic current                                              # 数据库版本
uv run python -c "from app.db import get_head_revision; print(get_head_revision())"   # 代码 head
```

> 也就是说：现在**不要**因为任何原因重启 `QQBotWeb`（含崩溃自愈、Windows 更新、手动重启）。

---

## 2. 部署步骤（维护窗口，预计 20–30 分钟）

前置：选低峰期；提前通知管理员；NapCat/QQ 客户端**保持不动**。

### 第 0 步 前置核对（1 分钟）

```powershell
cd "D:\CodeBuddy工作空间\CB 项目\qq-group-moderation-bot"
git status --short                              # 期望只有 D-037/D-038 相关改动
uv run pytest --no-header                       # 期望 1350 passed / 15 skipped / 0 failed
sc.exe query QQBotWeb     | Select-String STATE
sc.exe query QQBotRuntime | Select-String STATE
uv run alembic current                          # 期望 b8d4f2a05e31
```

### 第 1 步 打开急停（暂停新的自动处罚）

后台 →「群管理」页 →「**立即停止全部外部动作**」（`POST /admin/emergency-stop`）。

- 急停是**数据库级、跨进程立即生效**，**不需要重启**；开启后新产生的动作意图只记录不执行。
- 核对：`action_intents` 新记录状态为 `SKIPPED`，原因含急停。

### 第 2 步 备份数据库

后台 →「设置」页 →「**备份数据库**」。结果落在 `data/backups/moderation-<UTC时间>-*.db`
（在线备份：含 WAL 中已提交数据，并做 integrity check；不是简单复制主文件）。

- 记录**文件名 / 大小 / 时间**，并**复制一份到另一块物理磁盘**（运维要求：备份不得与唯一数据库同盘）。
- 命令行等价方式（可选）：

```powershell
uv run python -c "from app.config import get_settings; from app.reports.backup import backup_sqlite; print(backup_sqlite(get_settings().database_url))"
```

### 第 3 步 停止服务

```powershell
sc.exe stop QQBotWeb
sc.exe stop QQBotRuntime
```

（等价：`C:\nssm\nssm.exe stop QQBotWeb`。）等两个服务都变 `STATE : 1 STOPPED` 再继续。
Web 停止后 OneBot 连接断开属预期，恢复后 NapCat 会重连。

### 第 4 步 迁移数据库

```powershell
uv run alembic upgrade head
uv run alembic current     # 期望：c9a1f4d27e30 (head)
uv run alembic check       # 期望：No new upgrade operations detected
```

本次迁移**只新增一张表** `allowlist_members`，不改动任何既有表；表结构已实测为
`id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT` + `UNIQUE (provider, external_user_id)`，可完整回滚。

### 第 5 步 启动服务

```powershell
sc.exe start QQBotWeb
Start-Sleep -Seconds 5
sc.exe start QQBotRuntime
```

### 第 6 步 验证（生效证据）

```powershell
Invoke-WebRequest http://127.0.0.1:8001/healthz -UseBasicParsing | Select-Object -ExpandProperty Content
```

期望：`status=ok`、`onebot=ready`、`connected=true`、`queue_backlog=0`。另外核对：

- 后台「白名单」页出现**成员白名单**区块（单条添加 + 文件批量设置 + 导出链接）
- `data\service_QQBotWeb.log` / `data\service_QQBotRuntime.log` 无异常堆栈
- 后台「影子判定」页出现新的实时记录（说明 NapCat → 审核链路已恢复）

### 第 7 步 导入成员白名单

后台 →「白名单」页 →「用文件批量设置」→ 选择负责人维护的文本文件 →「**解析并预览**」→ 核对
"新增 / 停用 / 非法行" →「确认执行」。

- 首次导入预期：**全部是"新增"、停用 0**；非法行为 0（文件是 9–10 位纯数字）
- 导入后验证：名单内成员的下一条消息判定应为 `allow`，规则命中含"成员白名单命中"
- 若预览里出现"停用"，**先取消**，说明文件不完整（全量同步语义：文件里没有的会停用）

### 第 8 步 观察与释放

- 观察期**保持急停**：新规则先只看影子判定，不产生真实动作
- 确认影子判定符合预期后，由负责人决定是否解除急停（后台「群管理」→「预览解除急停」）
- D-038 的撤回在 OneBot 保持 `recall_only` 阶段时**只撤回、不自动禁言**

### 第 9 步 取证

把第 0/4/6/7 步的**原始输出**记入 `docs/evidence/2026-09-18-deploy-d037-d038.md`：
时间、命令、输出、迁移版本、白名单导入前后条数、healthz 结果。

---

## 3. 回滚方案（按情形选择；**已按"已提交/已部署版本"重写**）

**通用铁律**：无论哪种回滚，结束后必须核对
`alembic current` == `get_head_revision()`（否则 `app/main.py` 启动校验直接失败）。
切版本一律用 **`git switch --detach <已验收 SHA>`**，不用 `stash` / `checkout --`。

### A. 数据库未迁移前的中止（零数据影响）

数据库仍是 `b8d4f2a05e31`，只需把**代码**切回上一个已验收版本并重启：

```powershell
git rev-parse HEAD                                   # 记录当前 SHA，便于再前进
sc.exe stop QQBotWeb ; sc.exe stop QQBotRuntime
git switch --detach <上一个已验收 SHA>                 # 例：本轮之前 main 的 68a94b9
uv run python -c "from app.db import get_head_revision; print(get_head_revision())"   # 期望 b8d4f2a05e31
uv run alembic current                                                                 # 必须与之相等
sc.exe start QQBotWeb ; sc.exe start QQBotRuntime
```

### B. 迁移成功后的回退（数据库已是 `c9a1f4d27e30`）

**先备份名单**（决定"能否重新导入"，无论走哪个方案都先做）：

1. 后台「白名单设置 → 导出当前成员白名单」（`GET /admin/allowlist/members/export`），或
2. 从**降级前的一致性备份**（`data/backups/…`，见下方第 2 步）导出 `allowlist_members` 表内容。
   ⚠️ **不要用升级前的备份**：`allowlist_members` 正是本次迁移新建的表，旧备份里没有它。

**本节已落成可执行脚本（主审 F06-R 整改）**——此前把关键步骤写在注释里（没有真正备份、
没有产出可再导入的名单）、并用错了服务状态属性。现在整条链是一个脚本，**失败即停**：

```powershell
# 管理员 PowerShell，工作目录 = 仓库根
powershell -ExecutionPolicy Bypass -File scripts\rollback_d037_d038.ps1 `
    -ExpectedRevision b8d4f2a05e31 -TargetSha 68a94b9 `
    -StopTimeoutSeconds 60 -OutDir data\rollback-evidence
```

脚本内部顺序（每一步都用退出码兜底，前一步失败**不会**到达后面的 downgrade / start）：

1. **服务存在性**：`Get-Service` 取 **`Status`** 属性核对（该对象**没有** `State` 属性）——
   服务缺失立即 `exit 1`；
2. **精确停服**：`sc.exe stop` 后由 `scripts/rollback_preflight.py services` 轮询到
   **两个服务都 `Stopped`**（`StopPending` ≠ `Stopped`；超时或查询失败 → 非 0 → 中止）；
3. **一致性备份 + 名单导出**：`… backup-export` 调用项目入口
   `app.reports.backup.backup_sqlite`（SQLite `Connection.backup` + `quick_check`，WAL 下
   不会漏已提交数据）留备份，再**从该备份**导出 `allowlist-*.txt` 并**回读校验**
   （文件存在非空、能再解析导入、与库内启用集合一致）——启用成员为 0 或用了升级前的
   旧备份（没有 `allowlist_members` 表）都直接非 0 中止；
4. **降级 + 切码 + 版本核对**：`alembic downgrade` → `git switch --detach` → `… check-version`：
   数据库 `alembic_version` 与代码 `get_head_revision()` **都**等于目标 revision 才允许启动；
5. **启动两个服务**。

脚本自身的逻辑回归（`tests/test_r132_rollback_preflight.py`，25 项）：服务状态解析与
`STOP_PENDING`/超时/缺服务三条中止路径、备份+导出+回读校验（含"空名单/旧备份"两处拒绝）、
版本不一致拒绝，以及对本 `.ps1` 的静态门禁（必须用 `Status`、不得出现 `State`、每个关键
步骤后必须有 `Assert-ExitCode`、步骤顺序不可颠倒）。

> **数据影响（主审 F06 明确要求写清）**：`downgrade` 会 **`DROP` 整张 `allowlist_members` 表**
> ——这是**可逆 schema，不是无损恢复**：再升级回来是**空表**，必须用第 1 步导出的文件重新导入。
>
> **"保留名单"与"回退到旧代码"无法同时满足**：旧代码的启动校验同样要求 head 相等，而 head 相等
> 就意味着新表已被 DROP。要保留名单只能：① 先导出名单 → ② 按本节回滚 → ③ 重新部署新版 → ④ 重新导入。
>
> **只想关闭个别行为**（例如群名片撤回、成员白名单放行）时，**不要回滚版本**：用后台群动作开关 /
> 运行期急停 / 白名单一键停用即可，风险远小于版本回退（这些开关都是运行期生效，无需迁移）。

**回滚演练证据（回滚前必须已通过）**：迁移"升 → `alembic check` → 降 → 再升"往返已在
**独立临时库**验证——`tests/test_r132_member_import_review.py::test_member_migration_downgrade_preserves_existing_tables`
（断言：旧表哨兵数据保留、降级后新表消失、再升级为空表）。

**前置检查演练证据（主审 F06-R 要求"失败不得到达 downgrade/start"）**：
`tests/test_r132_rollback_preflight.py`（25 项）用**隔离库 + 可注入的服务状态**覆盖了
`StopPending` 轮询、超时中止、服务缺失中止、备份/导出失败（空名单、旧备份无表）、
版本不一致中止，并静态断言 `.ps1` 的步骤顺序与退出码检查——即"这些失败路径都不会
走到降级或启动"。
⚠️ **仍未具备的证据**：本机（Windows）**没有实跑过完整回滚链**（未停生产服务、未在
生产库演练）。生产演练须负责人授权并在维护窗口执行，**不得在业务时段直接降库**。

### C. 数据异常

1. 停两个服务
2. **先留证**：把当前 `data/moderation.db`（连同 `-wal` / `-shm`）复制到别处，不要直接覆盖丢弃
3. 用最近一次**已验证**的备份覆盖 `data/moderation.db`，并删除残留的 `-wal` / `-shm`
4. 核对一致性：`uv run alembic current` 与 `get_head_revision()` **必须相等**；
   备份版本落后代码 head 时回到第 4 步重新迁移，领先时按 B1 回退代码与迁移
5. 启动后核对：案件、审批、幂等记录、报告可读；**所有"结果不确定"的外部动作一律进人工复核，禁止盲目重放**（AGENTS 硬要求）

### D. 白名单导入导错

- **重新上传**正确文件即可纠正（全量同步），或用后台「导出」拿到当前值、改完再传
- 误导入**不会产生处罚**（最坏是放行范围变化）；受影响消息仍逐条落库，可按时间追查
- 设计上"停用而非删除"：误删的成员记录仍在列表中，可一键「启用」恢复

---

## 4. 边界与注意事项

- 本手册只管本机的两个 Windows 服务；**NapCat / QQ 是 GUI 程序**，部署期间不要重启它们
- 部署期间会有短时消息积压（Web 停止 → OneBot 断连），恢复后由心跳确认，**不要手工补投**
- **禁止**为了让服务起来而跳过迁移、或临时把数据库版本改回旧值（会让"数据库版本 ≠ 代码 head"永久不一致）
- 本轮的迁移脚本是**未跟踪文件**：任何"移出改动"的操作都必须带上未跟踪文件（`git stash -u` 或手工移出 3 个新增文件），只回退已跟踪文件会留下 head 不一致
- NapCat 若要求扫码/安全验证：保持人工模式，**不得反复自动登录**
- 部署完成 ≠ 验收通过：W2/W3/W4/W5 与 24×7 整机验收仍按 `docs/windows-delivery-checklist.md`
- D-037 的"成员白名单全类别放行"属高风险政策，建议部署后送主审独立复验
- 取证时**不要**把 `.env`、访问令牌、真实成员内容贴进任何文档或提交
