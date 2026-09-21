# 图片审核决定同库权威（A2）：已裁定的隔离候选

2026-09-22：负责人授权按推荐方案 A 继续，由我方承担主审和实施，要求不大改。
这份记录替代旧“等待外部六项裁定”的状态；旧提案保留在 Git 历史。
**候选只在独立 worktree/分支实现，未迁移生产、未部署、未改开关。**

## 六项裁定

1. 扩展 image_allowlist：decision_state/source/operator/version、decided_at、history_json。
2. rejected 必须有 disabled 行；运行时仍读取 enabled=1，判定逻辑、阈值与 enforce 不变。
3. SQLite 事务提交即决定生效；导出失败显式 DECISIONS_COMMITTED_EXPORT_PENDING、退出非零，不补偿。
4. 独立 backfill 工具默认 dry-run；显式 --apply 才写入，冲突全批拒绝，完成标记与决定同事务。
5. migration、回填与新代码在同一维护窗口上线。此前生产 checkout 保持旧兼容 migration head。
6. R1 是同 revision 兼容桥接，不是简单 revert；R2 删列降级是单独合成演练方案。

## 权威、版本与导出

新脚本 scripts/image_decision_authority.py 集中写入。批次验证前固定观察版本，
BEGIN IMMEDIATE 内按 expected_version 比较并更新；任何冲突整批回滚，不自动换新版本重试。
状态、来源、操作者、版本、enabled 与历史追加在同一事务中提交。
普通 seed 不得覆盖人工拒绝，显式重批才可以重新启用。

history_json 原样保留 legacy_snapshot 全部字段及 legacy_row；events 记录后续版本、
操作标识、来源、操作者、时间及之前的备注。它不从旧历史反推当前决定。
无审核时间时 decided_at 为 NULL；新增拒绝行的 created_at 是真实回填创建时刻，不能冒充源时间。
非法非空时间、状态矛盾、未知标记版本、歧义来源均拒绝回填。

原 decision_lock 全链边界保留，改为按数据库的可重入线程锁和 OS 文件锁。
进程退出释放锁，不根据 mtime 抢占活锁；锁文件长期保留防止换 inode。
导出取得锁后新读完整权威，再原子替换整份 JSON。损坏导出先保留原字节。
export-only 不重复人工决定，不增加版本或历史；verify-authority 比较完整规范快照，
不用每行版本的最大值冒充全局版本。

身份核验在同一把协调锁内观察 DB+JSON。DB/JSON 内容只读，首次协调可能创建 .review.lock；
缺失、陈旧、损坏导出均明确待重试，不能给整体通过。来源链仍核对原图字节与审核清单。
旧schema只读候选过滤仍明确标候选；写工具与身份核验不能回落到旧 JSON 权威。

## 迁移与回填

迁移 e1c7d4b8a902 接在 d4b7c1e9a502 后，只新增列和索引，不自动回填。
新增列不代表零成本，建索引和锁等待须算进维护窗口。
回填读取旧 DB 与 JSON，保留 enabled 集合，拒绝双方矛盾，不自动选择一方。
system_settings.image_decision_authority_version=1 与全批回填一起提交。
空表没有完成标记也不能冒充已回填；重复 apply 验证后不改既有版本。

命令（所有生产写命令仍须维护窗口授权）：

    python scripts/backfill_image_decisions.py --db <db> --dry-run
    python scripts/backfill_image_decisions.py --db <db> --apply
    python scripts/apply_review_decisions.py --db <db> --export-only
    python scripts/apply_review_decisions.py --db <db> --verify-authority

## 兼容与恢复

| 代码/库 | 旧 revision | 新 revision 未回填 | 新 revision 已回填 |
|---|---|---|---|
| 原旧代码 | 原行为 | 启动门禁拒绝：revision 不相等 | 同样拒绝 |
| A2代码 | 启动门禁/工具拒绝 | 服务schema兼容；决定工具明确NOT_BACKFILLED | 可验证权威与派生导出 |

app/db.py 的门禁是 revision != head，两边不一致均拒绝，不能改松门禁解决发布顺序。

R1兼容桥接保留新migration、模型及同库权威读写/导出，仅回退有问题的外围工具界面；
旧补偿写工具不得混入桥接。由于A2没有改变在线判定，服务可继续使用原判定路径。
具体桥接必须在合成数据库上验证 startup/maintenance 与决定/导出一致，不能仅凭表多列推断。

R2在合成副本演练 downgrade 至旧 head，再用冻结旧代码校验启动。需要重建表，
不得默认在生产执行。维护窗口内、恢复流量之前失败可恢复预迁移一致备份；
恢复流量之后不得无说明覆盖整库，避免丢掉新消息与处置记录。

## 探针与验收

原件封存、每项旧断言与新契约映射见
[适配登记](2026-09-22-authority-a2-adaptations.md)。
不仅有旧提案四类：JSON损坏、初始化fixture、schema精确断言与身份来源也逐处登记。
不删除原件、不skip/xfail、不保留误导性补偿来换绿。
最终执行 SHA、命令、数量、独立恢复演练和剩余限制见
[候选验收](2026-09-22-authority-a2-validation.md)。

生产窗口尚未授权；没有生产迁移、部署、回滚、enforce、群授权、动作开关变更。


## 切换期间的写入纪律

A2 的 OS 文件锁与旧 B 方案 O_EXCL 锁不是互操作协议。维护窗口必须停止旧审核 CLI、确认没有旧写入者，再迁移、回填、导出和切换全部写入口；不能让新旧工具同时写同一库。生产服务当前不改动，但也不能以“不改 enabled 查询”为由在生产 checkout 提前放入新迁移。
