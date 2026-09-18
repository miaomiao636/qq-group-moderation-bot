## r132 二轮整改完成（请复验新 SHA）

**受审对象**：`a354d17`（你复验的）→ 本次提交（见下方 SHA）。
报告：`docs/2026-09-18-r132-round2-remediation.md`。

### 你的新增探针结果

| 阶段 | 结果 |
| --- | --- |
| 整改前（本机） | `test_f05_plan_integrity.py` 6 failed、`test_qr_review_pair_unresolved.py` 2 failed（其余 8 项被守卫拦在 setup） |
| 整改后（本机） | **全部通过**（含你能在 macOS 跑通的 16 项） |
| 一轮 42 项 | 42 passed（未回退） |
| 全量 | **1461 passed / 15 skipped / 0 failed**；ruff check+format（241 文件）、mypy（88 源文件） |

你二轮的 3 个探针文件已**原样入库**：`tests/test_r132_f05_plan_integrity.py`、`tests/test_r132_qr_review_pair.py`、
`tests/test_r132_window_card_adjacent.py`。仅两处**非断言**适配：① 文件级 lint 豁免头；
② `test_r132_window_card_adjacent.py` 的守卫在 **Windows** 上放行 loopback —— 你们 macOS 的原始守卫会把
Windows `ProactorEventLoop` 的 socketpair 误判为"意外网络连接"（本机 8 项因此被拦在 setup）；
适配后**仍然禁止一切非本机连接**，探针意图不变；Ubuntu CI 会按原样跑你方版本。

### 逐项

- **F02-R**：抽出 `_secondary_pair_is_valid()`（**主路径与 QR 门共用同一判据**）并新增
  `_attachment_reviews_unresolved()`；QR 放行前先要求"全部附件已定论"——缺二审、异类、
  低置信、非独立模型、孤儿二审、降级/需人工都不能被另一张图的码洗成已审完。
- **F04-R**：窗口候选按 `decision.py` 导出的 `FORWARD_RECORD_RECALL_RULE_ID` /
  `GROUP_CARD_RECALL_RULE_ID` **显式排除结构性撤回规则**（不猜 R0xx 段号）；两种段顺序、窗口内外均保持 D-038。
- **F03-R**：`_is_group_card` **删除 app 子串判定**，只认 `meta.group`（含群号/群名等字段）或
  `app == com.tencent.qun.share 且 view == group` 的明确组合；`com.example.groupbuy` / `quniversity` 不再命中。
- **N01（我上一轮引入的退化）**：来源扫描**不再在第一个来源 break**——收集全部合格来源后再判定，
  顺序无关；"带码来源"要求**前缀 + 结构化 `has_miniprogram_code=true` 同时成立**；
  **在途图片保护独立计算**，不再被"已存在合格来源"跳过。
- **F05-R**：计划纳入"被改动行的 `updated_at` 版本"并进入批准指纹；`set_member_enabled()` 即便
  enabled 值未变也**推进版本**（覆盖"对已停用行再次明确停用"这一 ABA 场景）；写入改为
  **条件写入**（`WHERE id=? AND updated_at=?`，0 行命中即抛异常整体回滚）；确认流程改为
  **锁内校验归属/状态/指纹（新会话 + BEGIN IMMEDIATE）→ 批准 + 一次性认领 → 锁内条件写入 +
  导入审计 + 计划终态（同一事务）**，归属校验提前到批准之前（不再破坏其它会话的计划）。
- **F06-R**：回滚手册 §3 B 改为四步——等待两个服务 `Stopped`（60 秒超时中止，明确
  `STOP_PENDING ≠ STOPPED`）→ 用项目一致性备份入口（WAL，禁止只 copy 主库文件）→
  从**降级前的当前库**导出名单并核对启用数（**修正**：`allowlist_members` 是本次迁移新建表，
  旧备份里没有）→ 降级/切版本逐条检查退出码、失败即中止。
- **P2**：备注版本绑定、终态与名单同事务、跨会话不消费他人计划、允许来源卡片按卡片结构判断
  （消除"允许来源卡 + 普通文字"的新增误转人工）、F09 三处版本号 `t204-v16`。

### 仍未完成（未宣称完成）

1. 本次提交的**三 job CI** 需重新产出。
2. 固定 UTC 半开窗口、绑定账号/群集合/部署 SHA 的动作统计导出；`code 1200` 只作"调用超时"证据。
3. `PROGRESS.md` / `HANDOFF.md` 里"急停开启/解除待办"的旧文字待清理（属 F10 收尾）。
4. 负责人决策项未越权改动：图片哈希白名单（缺样本）、单模型直接决定 85% 图片处罚、名单大幅停用阈值、D-038 动作等级。

### 口径（负责人已明确）

**窗口内诈骗豁免仅限"带小程序二维码"的来源图**——前缀「小程序码通过」与结构化
`has_miniprogram_code=true` 必须同时成立；校园墙来源图之后的诈骗照常处理；窗口内在途图片保护始终生效。
