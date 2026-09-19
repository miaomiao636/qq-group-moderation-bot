# r132 第七批整改对账（送审稿）— 回应主审对 `2ff2a8a` 的复验

**受审基线**：主审复验 `2ff2a8a`，结论"部分通过、不能整批关闭"，新增 22 项探针
**14 failed / 8 passed**（原 65 项全过、8 个入库文件 AST 一致）。

**整改结果**：本轮 22 项探针 **22 passed / 0 failed**（本机独立复现：整改前 14 failed / 8 passed →
整改后 22 passed），3 个探针文件**原样入库**成为本仓库正式回归。

## 一、逐项对账（按主审"最小关闭标准"）

| 主审编号 | 最小关闭标准 | 我的实现 | 复现→整改后 |
| --- | --- | --- | --- |
| **R6-01** 离线二审未复用在线判据 | ①在线/离线使用同一纯判据；②旧记录缺配置/证据时标 unknown/unavailable，**不按默认阈值猜成已消疑**；③保留有效二审对照 | 把在线判据抽成**唯一**的 `app.moderation.ai.attachment_reviews_unresolved(ai_results, local=None, *, primary_direct_threshold, secondary_review_low, secondary_review_high)`；离线 `scripts/image_allowlist_seed.py::detail_blockers` 用 `AIModerationResult.model_validate` 还原 `model_dump()` 字典后**调同一函数**（`local=None`：只用**已持久化**的 `review_reason`，不重算）；旧记录缺 `review_role`/`review_group` 且确有复核痕迹 → 标 `unresolved_unknown`（**保守计入例外**） | 6 → 6/6 通过 |
| **R6-02** 生效/候选混用 | ①明确分开"生效名单评估"与"候选图片搜集"，默认生效模式必须保留空集；②候选模式须显式标记模式/来源/读取状态，**不能悄悄补回已排除图**；③报告保留来源、**删掉"确认后即可 enforce"** | `build_whitelist` **只认生效名单**（为空也是结论，删除了上一版"把判定引用过的样本补回集合"的逻辑）；候选项改由 `collect` 收集并**显式标"未在生效名单（候选）"**、**不计入"会改变判定"**；清单头新增 **集合模式**（`active`/`candidate`/`candidate_referenced`/`empty`）、**来源构成**、**读取状态**（新 `effective_state()` 区分 `ok`/`missing_table`/`bad_schema`/`unreadable`）；非生效模式打粗体"本清单不是生效名单评估结果"；删除两处"确认后即可切 enforce" | 5 → 5/5 通过 |
| **R6-03** attempts=0 被算作外发越界 | 展示与边界共用"已尝试发送/未发送/无法判定"分类；零次发送不进目标集合；有时间不能为清零而剔除；`attempts>0` ≠ 客户端已撤回 | 目标查询加 `attempts > 0`；**新增两个单列集合**：`attempts=0`（未发送/拦截）与 `attempts IS NULL`（无法判定），都**不进目标集合**；结果表 `_status_label` 与边界**同一分类**；`attempts>0` 的失败/超时**保留**在目标集合；报告写明"attempts>0 ≠ 客户端最终未撤回" | 1 → 1/1 通过 |
| **R6-04** 动作跨账号混合 | ①能以真实字段/审计链无歧义归属时统一账号/provider 范围，无法归属部分显式列出；②不能归属时保留全局动作统计并与本账号判定分列 | 归属改为**可证明才剔除**：同一 provider 下存在判定行、其 `external_message_id` 等于该动作/意图的原始 `message_id`、且该判定复合键**属于别的账号** → 剔除；否则（本账号判定命中，或**找不到判定行 = 无法归属**）**保留**并单列 `action_unattributed_rows`。**没有**把判定表的 LIKE 前缀照搬到动作表（原始 ID 会重复，会把真实日志整片过滤或错误归属） | 2 → 2/2 通过 |

**新增回归**（原样入库，仅加文件头，未改断言）：
`tests/test_r132_review_online_offline_pair_consistency.py`、
`tests/test_r132_review_image_set_round6_remaining.py`、
`tests/test_r132_review_window_scope_followup.py`。

## 二、按 4.2 / 4.3 / 4.4 校准的内容

- **4.2 A09**：`window_stats.py --start` 帮助与本项目对账表都删掉"已证明生效"的过强措辞，改为
  "**可证明已加载的最早时点** 17:01:22，**不等于应用已就绪**；同批另有服务 17:01:28 启动，
  前 6 秒无逐条版本归属证明，**按未证明处理**"。
- **4.3 授权快照**：越界结论里加警示——只与**导出时刻**的授权集合比较，
  **不能证明动作发生时未越权**（该事实按 `NOT_PROVEN` 处理）；同时说明动作日志暂无显式一致性读事务。
- **4.4 对账表更正**：
  - R09-D 回归改指 `tests/test_r132_source_collection_contract.py`、
    `tests/test_r132_r09_real_unresolved_shapes.py`、`tests/test_r132_review_new_shadow_boundaries.py`；
  - 成员并发改指三个定向文件（`test_r132_f05_round2_edges.py`、`round3_state_drift.py`、`round4_supported_boundaries.py`）；
  - F06 改指 `tests/test_r132_rollback_preflight.py`；
  - **A06-R 那行"在线离线已共用同一 `detail_blockers`"是不实陈述**，已更正为"当时只统一了粗分类，
    在线用的是含二审有效性的 `_attachment_reviews_unresolved`，**本轮才真正共用**"；
  - **F06-C**：脚本只做服务 `Running` 检查、**不请求 healthz**，不等于应用健康/端到端恢复完成；
  - 删除"空集是结论"与"空集补候选"的自相矛盾表述；
  - "样本未到"改为"**最终批准集合 / 校准样本是否齐备及其入库状态待确认**"（已有样本文件 ≠ 已建立的生效名单）。

## 三、门禁与 CI

- **本仓库全量**：**1719** 用例 / **0 failed / 0 error / 15 skipped**；`ruff check` + `format --check`、
  `mypy app`（89 源文件）全绿。
- **CI**（GitHub Actions，三 job：Ubuntu / Windows / clean runtime-deps）：
  - `e8e600e`（R6 前四项代码修复）：[run 35433655263](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35433655263) **三 job 全部 success**；
  - `77e6ce0`（集合分离/归属 + 3 探针入库）：[run 35434168901](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35434168901) Ubuntu ✅ / clean runtime-deps ✅，Windows 结论见 PR 页面；
  - `33b9b3b`（文档校准与原样入库探针的说明）：[run 35434359636](https://github.com/miaomiao636/qq-group-moderation-bot/actions/runs/35434359636)。

## 四、仍然"未证明"的部分（不因本轮整改而改变）

1. **A09 前 6 秒**（17:01:22→17:01:28）没有逐条归属证明 → 该段按未证明，不作结论；
2. **历史授权快照**：越界核查只对导出时授权集合有效（`NOT_PROVEN`）；
3. **导出多查询无显式一致性读事务**：行级原件应基于一致性快照重出（已记录，待做）；
4. **Windows 回滚全链未实机演练**（只做了静态门禁 + 隔离库逻辑测试）；
5. **`enforce` 未实现**、**迁移未执行**、**未部署**（生产仍是先前加载的版本）；`IMAGE_HASH_MODE=off` 默认不变；
6. **图片最终批准集合与阈值校准未完成**（需负责人逐张结论）；
7. 复跑主审包时本机（Windows）有 1 个 **teardown ERROR**（`PermissionError: WinError 32`，pytest 清理临时库文件被占用），
   **不是断言失败**，macOS 环境不会出现；如需可在 Linux CI 上复核。

## 五、可直接转发的回评

> 主审 r132 第六轮复验（受审 `2ff2a8a`）的四项 P2 已全部整改，第七批推送至 `33b9b3b`：
> **您那 22 项探针整改后 22/22 通过**（本机整改前 14 failed / 8 passed → 整改后 22 passed，失败项名与您报告逐字一致），
> 3 个探针文件已**原样入库**（`tests/test_r132_review_{online_offline_pair_consistency,image_set_round6_remaining,window_scope_followup}.py`，未改断言）。
> 映射：R6-01 → 抽出唯一的 `attachment_reviews_unresolved`，离线 `detail_blockers` 还原字典后调同一函数，
> 缺证据标 `unresolved_unknown` 而不按默认阈值猜；R6-02 → `build_whitelist` 只认生效名单（为空也是结论），
> 候选由 `collect` 收集并显式标注、不计入"会改变判定"，清单头写模式/来源/读取状态，删除"确认后即可切 enforce"；
> R6-03 → 目标集合加 `attempts > 0`，`attempts=0` 与 `NULL` 单列不进目标集合；R6-04 → 归属改为"可证明才剔除，
> 否则保留并单列无法归属行数"，未照搬 LIKE 前缀。
> 文档已按 4.2/4.3/4.4 校准（A09 措辞、授权快照 NOT_PROVEN、对账映射、F06-C 只做 Running 检查、
> 更正"在线离线已共用同一判据"的不实陈述）。仓库全量 1719 / 0 failed / 15 skipped，ruff + mypy 全绿。
> **如实声明**：A09 前 6 秒仍无归属证明；历史授权快照未绑定（NOT_PROVEN）；导出多查询无显式一致性读事务；
> Windows 回滚链未实机演练；`enforce` 未实现、迁移未执行、未部署（`IMAGE_HASH_MODE=off` 不变）；
> 图片最终批准集合与阈值校准仍待负责人逐张结论。
