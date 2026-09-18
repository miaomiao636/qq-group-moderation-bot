# 项目现状与待主审复核清单（2026-09-16）

> 本文件供主审复核与负责人查阅。所有变更均有：PR + CI（双平台）、回归测试、
> `admin_audits` 审计、`DECISIONS.md` 决策记录三重背书。
>
> **09-17 更新**：主审 5060361 复验的 R1–R3 整改已完成并入库回归——见第六节（本批待复验）。

## 一、当前系统状态（核心服务运行，QQ 通知待恢复）

| 项 | 状态 |
| --- | --- |
| QQ 账号 | **<BOT_QQ>**（NapCat online，connect_count=1；旧号 <OLD_BOT_QQ> 已退役） |
| 管理群（真实动作） | **13 个**（全部 >500 人：11 既有 + 2 新增）；另有 3 个历史群审核关 |
| 审核（判定/记录） | 全量运行——收到消息的群全部判定入库（无记录群**默认审核启用**，历史行为） |
| 真实动作（撤回） | 仅 13 个显式授权群（action_enabled=1）；其他群**默认关** |
| 急停 | 已解除（负责人 09-16 确认"恢复动作"；审计 emergency_resume） |
| 白名单 | 1 词（负责人维护，**启用中**；C01–C03 修复部署后已于 22:10 恢复启用、审计留痕）——W01 修复后为"**仅免广告**"语义 |
| 通知 | 邮件通道正常；QQ 通知通道待负责人将新号拉入通知群（1043951076）后自动恢复 |
| 服务 | QQBotRuntime + QQBotWeb 运行中——22:07 提权重启加载全部修复（QQBotRuntime PID 29388 / QQBotWeb PID 44672）；healthz ok、NapCat ready/online |
| 数据库 | alembic head = **b8d4f2a05e31**（`alembic check` 零漂移） |

## 二、今日变更汇总（按主题）

### A. 判定政策（负责人口径）
1. **D-031 办证/学历完全放行**（PR #18）：本地豁免 allow + 全链路不得升级；
   停用 3 条"办证=诈骗"动态规则（审计 v53–v55）+ 拒绝候选 #281。
2. **D-032 群主/管理员卡片完全放行**（PR #19）：卡片 allow + 全链路保护；
   普通成员卡片与非卡片不变。
3. **D-033 全局白名单**（PR #22，前任实现）→ **R-115 W01 修复**（PR #27，见下）。

### B. 运营（负责人指令）
4. **QQ 换号** <OLD_BOT_QQ> → <BOT_QQ>（D-034，PR #25；换号手册 + 实操记录 PR #26）。
5. **管理群扩至 13 个**——所有 >500 人群（新增：大学生家教群 635 人、
   2026大一新生交流群 557 人）；审计 `ops:owner-groups-gt500-2026-09-16`。

### C. R-115 审查整改（主审报告 58344a6）
6. **W01（P1）白名单仅免广告**（PR #27）：`ALLOWLIST_ALLOW_RULE_ID` 移出
   `POLICY_ALLOW_RULE_IDS`；DR/AI/媒体逐次复核类别；未知图片回人工；
   更正 2 个错误预期测试；主审 40 项开/关对照**正式入库**
   （`tests/test_r115_w01_policy_probe.py`，修复前 16 failed → 修复后 **40 passed**）。
7. **W02–W04（P2×3）管理面加固**（PR #28）：
   - W02 启停显式目标 + 幂等（双击/重试不翻转；缺目标拒绝）；
   - W03 `AUTOINCREMENT` 防 ID 复用（旧表单不能操作替代对象）；
   - W04 `normalized` 唯一约束 + 并发冲突安全复用 + 历史重复保守合并；
   - 迁移 `b8d4f2a05e31`（升级/降级/check 零漂移；迁移前重复核查不静默）。
8. **止损操作**（已执行）：白名单词曾临时停用（旧代码风险窗口，19:17），
   修复上线后由负责人重新启用（19:46，审计可查）。
9. **C01–C03（P1×3）判定边界整改**（主审 6b7b990 复验新增）：
   - C01 政策独立计票——白名单命中不再遮蔽 D-031 办证保护（重叠场景两种开关都 allow）；
   - C02 广告证据不参与处罚——AI/DR 广告证据不进处罚候选；非广告证据独立达原门槛
     （DR 高门槛 0.90 / AI 确认）才升级，弱严重信号转人工（record_only、空建议）；
   - C03 故障/未知不放行——AI 失败/配置错误/needs_review/未知类别一律转人工；
- 回归：主审交付包 4 探针正式入库（共 52 项），修复前 10 failed → **52/52**；
  止损与恢复：白名单词先停用（风险窗口）；修复部署（22:07 重启）后已于 22:10
  恢复启用（审计 `ops:owner-allowlist-resume-c01c03-2026-09-16`）。

## 三、待主审复核清单（本轮提交）

| # | 项目 | 证据 | 复验要点 |
| --- | --- | --- | --- |
| 1 | R-115 W01（P1） | PR #27（merge `8d6661a`）；探针 40/40 | 主审 policy probe 复跑应 **40 passed** |
| 2 | R-115 W02（P2） | PR #28（merge `6a90ea9`）；回归 4 项 | 重复停用不启用；旧页面（缺目标）拒绝 |
| 3 | R-115 W03（P2） | 同上 | 删除后新增 ID 单调；旧 id 操作返回"不存在" |
| 4 | R-115 W04（P2） | 同上 | 等价词仅一行；停用即真实失效 |
| 4b | R-115 C01–C03（P1×3，6b7b990 复验新增） | 本轮修复；交付包 4 探针入库（52 项） | `test_policy_overlap` 12/12；`test_w01_residual` 14/14；原 40 项不倒退 |
| 5 | 迁移 `b8d4f2a05e31` | 迁移文件 + `alembic check` | upgrade/downgrade/零漂移/数据保留 |
| 6 | 运营变更（D-034） | `admin_audits` | 知情项（非代码；换号 + 13 群授权） |
| 7 | D-031/D-032 边界 | 负责人本轮确认 | "两项保持完全放行，仅收紧 D-033"——文档已同步 |

**主审复验命令**（从仓库根目录；格式同主审附加说明）：

```powershell
$repo = (Get-Location).Path
$env:PYTHONPATH = "$repo;$repo\tests"
.\.venv\Scripts\python.exe -m pytest -c pyproject.toml -p conftest <探针文件> -q -o addopts=""
```

- policy probe（40 项）：修复后应 40 passed；
- admin probe：按核心断言核对——**重复停用不启用、旧表单不操作新对象、
  等价词无重复生效**（接口已按最小修复调整：显式 `target_enabled` 字段；
  旧对象操作返回"不存在"/幂等——对应主审报告允许的等价调整）。

**本轮 R-115 回归（6 个文件，主审可整体复跑）**：

```powershell
.\.venv\Scripts\python.exe -m pytest -c pyproject.toml -p conftest tests/test_r115_w01_policy_probe.py tests/test_r115_policy_overlap.py tests/test_r115_w01_residual.py tests/test_r115_allowlist.py tests/test_r115_admin_reaccept.py tests/test_r115_admin_migration.py -q -o addopts=""
```

## 四、本地验证数据（本次交付）

- 全量回归（C01–C03 修复后最终版）：**1301 collected / 0 failed / 1286 passed / 15 skipped**
  （15 项跳过为环境依赖：符号链接权限、生产服务运行时锁快照守卫等，非代码缺陷）；
- ruff check / format、mypy（87 源文件）通过；CI（ubuntu + windows + 干净依赖）全绿；
- 本地 SQLite 迁移 `upgrade → downgrade → upgrade` 通过，迁移前备份
  `data/moderation.db.bak-before-r115w`；
- 运行时：healthz `ok`、NapCat `ready/online`、connect_count=1；
- 部署与恢复（09-16 晚）：22:07 提权重启（QQBotRuntime PID 29388 / QQBotWeb PID 44672）
  加载全部修复；22:10 白名单词恢复启用（审计 `ops:owner-allowlist-resume-c01c03-2026-09-16`）；
  换号后真实动作正常（当日 190 成功 / 10 次 QQ 侧偶发 recall 超时，非权限问题，已留痕）。

## 五、已知边界与未包含（透明说明）

- **非授权群**：新号在的其他 ~130 群，消息仍会被判定/记录（默认审核开、
  动作默认关）；群管理页对"见过的群"自动列出（影子判定入列），可按需配置；
- **"同步群列表（自动发现+批量授权）"**：候选功能，未实施；
- **通知 QQ 通道**：依赖负责人将新号拉入通知群（1043951076）或改目标群；
- **历史 150 条评测**：仍为混合版本基线，不证明三项新政策效果（既定门槛不变）；
- 本轮整改**不包含**任何对 D-031/D-032 的收紧（负责人明确保留）。

---

## 六、09-17 主审复验（5060361）整改 —— R1–R3（主审 ddeb893 复验通过，已关闭）

主审复验包 `qqbot-review-5060361` 结论：指定 6 文件 105/105、旧交付 52/52 全过，
旧反例确已修复；新增 3 项残余（24 项复验探针实测 11 failed / 13 passed）。本批已全部修复：

| 项 | 位置 | 修复 | 关闭标准（已满足） |
| --- | --- | --- | --- |
| R1（P1，C03 残余） | `ai.py` 提前放行 | 判据改为依据**原始全量结果**（category=None 的 needs_review 不再被空集检查吞掉）；纯 None 控制组与 AI 未启用保持放行 | null 待人工 4 项 + 正常控制 2 项全过 |
| R2（P2，C02 残余） | `rules.py` 弱/强分支、`ai.py` 转人工分支 | 类别/置信度由**非广告证据同源产生**（不得混入广告分、不得丢失类别、不得被 AI 广告分抬升）；verdict/动作不变、hits 全保留 | 弱 4 项 + 强 2 项（含类别/置信度断言）全过 |
| R3（P2，C01 残余） | `rules.py` 执行顺序 | D-031 办证保护**先于** D-033 弱信号分支（条件与边界不变） | 12 项（三类别 × 0.2/0.95 × 白名单开/关）全过 |

本批验证（交付证据）：
- 主审复验探针 24 项**正式入库**为回归：`tests/test_r115_c02_c03_edges.py`、
  `tests/test_r115_certificate_dynamic_overlap.py`（修复前 11 failed → **24/24**）；
- R-115 六文件 **105/105**；全量 **1325 收集 / 0 failed / 1310 passed / 15 skipped**
  （15 项为环境依赖跳过：符号链接权限、运行时锁快照守卫等）；
- ruff check / format（app tests alembic，227 文件）、mypy（87 源文件）通过；
  CI（ubuntu + windows + 干净依赖）随本 PR 附。
- 政策不变：D-031/D-032 完全放行、D-033"仅免广告"——本批仅补"口径未落实到位"的边界面。

> 注：主审第三节"运行数据核对"的补证项已交付：`docs/evidence/2026-09-17-runtime-evidence/`
> （动作统计 / 意图状态 / 审计元数据原行 / 失败明细与状态；导出为只读操作，未触发新动作）。

---

## 七、09-17 主审复验（ddeb893）T1 整改 —— 文字通道 needs_review 转人工（主审复验通过，已关闭）

主审 ddeb893 复验结论（`qqbot-review-ddeb893`）：R1–R3 按原关闭标准**通过、可关闭**；
运行补证 v2 维持通过；单列 1 项 **P2 旧缺陷**（非 R1–R3 引入）：普通成员纯文字消息，
固定文字模型返回 category=None + needs_review=True 时，真实 `run_pipeline` 输出
allow、未进入人工处理（5060361 基线同样复现：2 failed / 2 passed）。本批已修复：

| 项 | 位置 | 修复 | 关闭标准（已满足） |
| --- | --- | --- | --- |
| T1（P2） | `ai.py` `merge_ai_evidence` 文字循环 | 非降级文字结果的**未消解 needs_review** 与视觉"未消解 gray"同口径进入人工兜底条件（新增 `unresolved_text_review` 并入转人工 if）；不改升级判定与既有 record_only 分支的原因/类别口径 | 4 项探针全过：True→record_only、False→allow（建议均空），白名单开/关全覆盖 |

本批验证（交付证据）：
- 主审 T1 探针 4 项**正式入库**：`tests/test_r115_text_needs_review.py`
  （修复前 2 failed / 2 passed → **4/4**；主审原始探针文件在本机复跑同样 4/4）；
- r115 系（9 文件）**133/133**；全量 **1329 收集 / 0 failed / 1314 passed / 15 skipped**
  （15 项为环境依赖跳过）；
- ruff check / format（228 文件）、mypy（87 源文件）通过；CI（ubuntu + windows +
  干净依赖）随本 PR 附。
- 政策不变：D-031/D-032 完全放行、D-033"仅免广告"；T1 修复仅补"文字通道未消解
  疑问未进人工"的口径缺口，不改任何已授权政策的判定。
- 部署：**已于 09-17 13:09 提权重启生效**（QQBotWeb PID 40952 / QQBotRuntime PID 45976；
  healthz ok、NapCat online；通知通道按负责人要求保持双停）。

> 主审 ddeb893 复验同时确认：运行补证包相比已独立重算的 `229081e`，仅账号脱敏变化，
> meta/动作日志/意图/群配置/healthz 逐字节不变（上轮计算可复用）；CI 记录与仓库一致。

> 主审复验确认（2026-09-17）：状态文档 §七与代码一致——文字模型要求人工时**不再静默
> 放行**、正常控制仍放行；D-031/D-032 与视觉二审逻辑未改变。**T1 关闭。**

---

## 八、09-17 负责人口径调整：校园墙图豁免放宽为「口径 C」（本批待复验）

背景（实案，10:49–10:51）：成员海报经视觉**确认**为校园墙来源（`校园墙白名单|文案:…`），
但其后两条**改写版**广告文字与图内文案相似度仅 17.0% / 11.6%（原口径门槛 60%）→ 被自动
撤回并立案；另一条还因"中间隔着消息"未满足「紧邻」。负责人复核后决定放宽（决策 D-036）：

| 项 | 原口径（2026-09-14） | 新口径 C（2026-09-17） |
| --- | --- | --- |
| 相似度 | 文字与图内文案 ≥60% | **取消**（图后任意广告文字） |
| 紧邻 | 图与文字之间不得有该成员其他消息 | **取消** |
| 数量 | 一图只免一条（原子绑定） | **取消**（窗口内多条共享） |
| 保留 | 视觉结构化确认来源 / 仅 ad / 2 分钟按发送时间 / 账号-群-成员作用域 / 图审核中不处罚 | 全部保留 |

- **代码**：`app/moderation/wall_pair.py`（重算式豁免，不再写绑定）、
  `app/runtime/pipeline.py`（取消绑定保留逻辑 + 注释更新）。
- **回归**：相关 6 个测试文件同步更新（含实案回归：改写文案（低相似度）、record_only 来源图、
  中间消息、窗口边界、并发共享均应豁免）；全量 **1330 收集 / 0 failed / 1315 passed /
  15 skipped**；ruff check / format（228 文件）与 mypy（87 源文件）通过。
- **部署**：**已于 09-17 14:29 提权重启生效**（QQBotWeb PID 41448 / QQBotRuntime PID 3164；
  healthz ok、NapCat online；口径 C 代码加载已核验）。

**复验要点（建议逐条核对）**：

1. **移除项**：代码中不再出现相似度门槛 /「紧邻」/「一图一条」与 `wall_paired` 绑定
   （`wall_pair.py` 重算式；`pipeline.py` upsert 已简化）；文档与代码一致。
2. **实案回归**：`tests/test_wall_pair.py`——改写文案（低相似度）、来源图为 record_only、
   中间消息、一图豁免多条、并发共享 → 均应 `record_only`（不撤回、不立案）。
3. **安全边界（必须仍拒绝）**：fraud/violence 不豁免；未含图内文案 / 否定表述 / 未消疑
   vision 不作来源；窗口外（>120s / 同秒无法定序）不豁免；跨账号/跨群/跨成员不豁免；
   图片审核中不处罚也不豁免。
4. **回归不倒退**：原 129 项（R-115 八文件）+ 视觉二审控制组 + 全量 1330 收集 0 failed。
5. **一键复跑命令**（仓库根）：

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -c pyproject.toml -p conftest `
     tests\test_wall_pair.py tests\test_wall_pair_contract.py tests\test_pairing_inflight.py `
     tests\test_pairing_active_context.py tests\test_shadow_pair_binding.py `
     tests\test_pairing_retention_metadata.py -q -o addopts=""
   ```

   （预期 **66 passed**）

**补丁（2026-09-18，主审 f08157d 复验 P1）**：同秒 + 前图未完成（inbox 排队 / 下载中 /
数据库 processing）时，文字仍可能带处罚建议进入动作编排——"图片审核中不处罚"保护未覆盖
同秒。修复：未完成图片的保护窗口改为 `0 <= delta <= 120`（**含同秒**），只转人工、清空
处罚建议；**豁免判定仍要求图严格先发（0 < delta）**，已完成图同秒照旧不豁免。主审 4 项
探针转正式回归 + 补同秒数据库 processing 覆盖（`tests/test_pairing_inflight.py`）；
主审原始探针修复前 2 failed / 2 passed → **4/4**；相关 6 文件 **66/66**。
