# 切换 OFFICIAL 模式操作手册

本手册描述从 SHADOW（仅记录）切换到 OFFICIAL（仅真实撤回）的完整流程。
开关默认关闭；缺少必要条件不执行真实动作。OFFICIAL是兼容保留的全局门禁名称，出口按provider选择，不表示OneBot通过官方API执行。

## 前置条件（不可跳过）

1. T-303 影子验证主审验收通过（24h 数据 + 断线演练证据齐备）
2. T-307 NapCat 动作 Adapter 主审验收通过
3. W2独立影子闭环通过。进入W3仅需负责人明确指定隔离群和同意受测成员；开放目标群则必须W3/W4通过。
4. 已知风险并接受：OFFICIAL 模式会撤回真实群成员的消息

**这些验收条件是人员流程门禁，不是代码自动读取的验收状态。开关全开并满足运行门禁就可能处罚真实成员。** 不要先全开再测试；按 `windows-delivery-checklist.md` 顺序执行。已有显式开关会随配置/DB保留，重启不是自动关闭，首次迁移R-105会安全关闭历史动作位。

## 三层开关（缺一不可）

```
第1层：全局配置（.env + 重启服务）
├─ APP_ENV=prod
├─ ACTION_MODE=OFFICIAL          ← 主开关：SHADOW → OFFICIAL
├─ EMERGENCY_STOP=false
├─ ONEBOT_SELF_ID=<专用QQ数字账号>
├─ ONEBOT_ACTION_STAGE=recall_only ← 唯一支持值；旧 full 档位不可启用
└─ ONEBOT_ACTIONS_ENABLED=true   ← T-307 独立第二道开关（默认 false）

第2层：每群路由（group_provider_routes 表）
└─ 消息入口 provider → 动作出口 provider 的同通道路由
    （身份必须明确，禁止双出口；变更经真人预览批准，不直接手改数据库）

第3层：每群面板开关（管理后台 → 群管理）
└─ 选择provider+群，预览完整变更并由真人批准，再执行；不是Agent自己点确认
```

## 操作步骤

### 1. 修改 `.env`

```ini
APP_ENV=prod
ACTION_MODE=OFFICIAL
EMERGENCY_STOP=false
ONEBOT_ACTIONS_ENABLED=true   # T-307 第二道开关
ONEBOT_ACTION_STAGE=recall_only
```

> 还需 `ADMIN_PASSWORD`、`ONEBOT_WS_ENABLED`、`ONEBOT_ACCESS_TOKEN`、`ONEBOT_SELF_ID`。OneBot-only不需要QQ官方凭据；官方出口另需QQ_APP_ID/QQ_APP_SECRET。令牌与管理员口令分离，不给通用Agent管理员口令。

### 2. 重启服务

按本机实际服务名停止/启动本项目进程；先确认只有一个Web/OneBot实例。历史报告的QQBotWeb/QQBotRuntime名称须实机核实，官方运行器仅在确有官方入口时启动。修改前备份、先影子启动，禁止热重载或多worker。

启动时配置校验 fail-closed：缺任何一项直接报错拒启（不会以半开状态运行）。

### 3. 面板开启目标群动作

管理后台 → 群管理 → 明确provider和目标群 → 预览变更 → 真人批准 → 执行。

- 核对同通道路由与唯一动作所有者，身份歧义时不得猜测或默认选一个
- **建议先只开一个低风险隔离测试群**，观察至少 24 小时再开其他群
- 关闭动作不会删除路由（路由是拓扑信息，动作开关由 `action_enabled` 守门）

### 4. 验证

- 由同意受测的成员在隔离群触发高置信违规，观察：
  - 消息被撤回；后台可审计结果和撤回通知
  - 首次或再次违规均无自动禁言、无群内违规警告，也不因再次违规自动立案
- 管理后台 → 案件/动作日志可审计每次执行结果
- `action_intents` 表出现 `SUCCEEDED` 状态记录

`recall_only` 是唯一支持的自动动作阶段。W3 先在一个隔离群核验撤回、保护角色、幂等及无禁言/警告外呼；W5 逐群启用时仍保持仅撤回。历史动作与案件记录继续保留供审计，不因重启或配置变更补罚；不要清库凑测试。

## 紧急回退

### 急停（不重启，立即生效）

优先后台“激活急停”：写共享DB，不需重启；Agent需emergency:stop权限，可直接激活，解除仍需真人批准。每个新外呼前重读急停，已发送请求无法撤回；核对在途UNKNOWN，不能声称立刻取消所有网络中的动作。

> 注意：`ACTION_MODE=OFFICIAL` 与 `EMERGENCY_STOP=true` 在配置层互斥——
> 启动校验会拒绝同时持有二者。急停的正确用法是先 `EMERGENCY_STOP=true` 再把
> `ACTION_MODE` 改回 `SHADOW` 重启，避免配置冲突。

### 完全回退到影子

```ini
ACTION_MODE=SHADOW
ONEBOT_ACTIONS_ENABLED=false
```

重启服务。已撤回消息无法自动恢复；历史禁言可由授权管理员人工解除。新消息不执行真实撤回。

## 按群临时关闭

不改动全局配置，仅对个别群停止动作：

管理后台 → 群管理 → 该群取消勾选「动作」→ 保存。

该群后续动作按最新DB开关拒绝，已发送请求需另行核对；其他群不受影响。

## 安全保证（T-307 实现）

- **数字 ID 强制校验**：非数字 `group_id/user_id/message_id` 直接 FAILED，绝不伪装或猜测
- **发送前未就绪 = FAILED**（确定未发出请求）；**发送后超时/断线 = UNKNOWN**（结果不确定，冻结禁止自动重放）
- **保护角色**：群主/管理员/白名单的违规只记录不处罚
- **幂等**：同一动作意图最多成功一次，重复事件不重复处罚
- **无踢人**：Adapter 不含任何踢人逻辑（AST 守护测试保证）；踢人必须人工审批（T-304）
- **默认关闭**：`ONEBOT_ACTIONS_ENABLED` 默认 false，代码同步/重启/NapCat 重连均不自动开启
