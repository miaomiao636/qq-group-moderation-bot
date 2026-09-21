# A2 主审探针适配登记

原契约冻结基线：`adef6ee558be3ba9d53de7111c6eecfd21a74ef9`。原件逐字节副本与 SHA256 见
`docs/evidence/authority-a2-20260922/legacy-probes/` 和 `legacy-probes-manifest.json`。
副本目录禁止换行归一化；原件未删除、未弱化、未移出历史证据。

用户已授权我方主审并实施 A2。旧契约的验证见核心收尾记录；本候选须按新契约单独执行，
不能借用旧基线的通过数量。历史 nodeid 保留用于追踪，名字含 compensation/rollback 的用例
现在验证“决定已提交、派生导出失败不撤销”；文件头的旧 VERBATIM 声明仅描述已封存版本。

机器可核对的逐函数/逐 fixture/钩子变化见本目录证据中的 `adaptation-ast.json`，
完整逐行变化见 `legacy-adaptations.diff`。它们列出原/新函数与断言，不以总绿代替逐项审查。

| 文件 / 函数范围 | 旧契约或设置 | A2 登记后的契约与未变部分 |
|---|---|---|
| image_tool_set_consistency、image_tools_contract 的 sandbox | 手建旧 schema | 合成 A2 列与显式真实回填；普通导入/重复/候选断言保留 |
| image_tools_contract::test_seed_excludes_first_insert_control | 首次拒绝无行 | 必须有该哈希 disabled/rejected/version=1 行，不能重新进入生效名单 |
| image_tool_set_consistency 的 manual_disable；candidate_collection 的停用准备 | 只改 enabled | 通过真实决定事务停用；原“不得重引入”断言保留；内部测试另注入不一致行证明 fail-closed |
| online_offline_pair_consistency::seed | ORM 仅写 enabled | 合成库显式回填+真实权威批准；线上/离线判断、模型与阈值断言完全保留 |
| image_hash_core 的 migration roundtrip；unmigrated_startup_boundary | 固定旧 head 和八列 | 精确新 head、全部十四列、decided_at 可空，其余 NOT NULL、约束、升级/降级、旧库拒启全部保留 |
| identity_and_window::make_db / positive / 两个来源负例 | 四列库与 JSON 决定 | 完整合成 A2 权威；当前来源在 DB；正例导出真实快照；原图/批次/来源/DB和JSON字节只读断言保留 |
| identity_remaining_and_utc::snapshot；round11_identity_collisions::write_snapshot | JSON 是决定来源 | 合法来源经真实 DB 决定+导出；非法 JSON 单独作为派生损坏；新增内部参数化直接污染 DB 枚举/历史，避免仅因 stale 假绿 |
| review_write_contract::test_corrupt_rejection_snapshot_not_silently_replaced | 损坏 JSON 阻断并原位保留 | 先逐字节封存损坏导出，再从 DB 重建；拒绝决定必须已在 DB。不是删证据 |
| r9_write_residuals::test_corrupt_snapshot_blocks_generic_import | 损坏 JSON 阻断普通导入 | 已初始化 DB 权威不受派生 JSON 损坏否定；原损坏字节封存、DB决定和导出均核对 |
| r9_write_residuals::test_snapshot_failure_cannot_leave_committed_approval 两参数 | 导出失败必须撤销批准 | 写失败返回 PENDING/非零但 DB allowed/version=1；损坏导出可封存重建；无补偿 |
| r9_write_residuals 的并发拒绝/重批 | 单条 record_rejection 快照钩子 | 整份 export 钩子；保留真实线程和现有全链锁阻塞/顺序完成与最终不矛盾断言 |
| compensation_ownership 前两项 | 补偿不得覆盖后续决定 | export seam 内真实后续 apply，旧导出失败返回5，后来状态/来源及单调版本仍在 |
| compensation_ownership 的删除/重建两参数；round11_c03 外部行归属 | 补偿归属凭据 | 保留真实独立 SQL 删除/重建和精确外部行断言；导出失败不做 DB 回写；新未初始化外部行不能认证 |
| compensation_ownership::test_failed_reapproval_restores_prior_note_without_erasing_old_history | 精确恢复旧 enabled/note | 重批决定保持 enabled/allowed，完整旧事件不变且 previous_note 精确保留；不声称恢复了未发生的回滚 |
| round11_c03 部分快照两参数 | 第二个单条快照失败→部分恢复 | 单批事务两项均提交，再注入整份导出失败；PENDING且不输出IMPORT_OK；export-only恢复不重新决定 |
| round11_c03 late JSON；round12_c03 second snapshot | 补偿期间等待/报告 | 真实线程在保留的全链锁等待，旧导出失败后后续决定成功；最终DB/JSON一致、版本递增，断言不再要求补偿字串 |

没有 skip/xfail，也没有以放松锁使探针通过。旧只读候选兼容仅适用于无法读取旧 authority schema
时的明确候选过滤；A2 未回填/不一致不能用旧 JSON 或 enabled 冒充已批准。
