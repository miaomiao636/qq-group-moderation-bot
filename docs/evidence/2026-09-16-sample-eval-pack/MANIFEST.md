# 评测数据包（R-113 §5.3 补证）

供独立复算；**不含**消息原文、媒体、真实群成员身份。匿名输入为正式评测器输入的逐字节副本。

## 文件与哈希（sha256）

- `eval_input_anonymized.jsonl`（150 行 metadata-only）：`5672ef20d2a4012b66cb5c6698fa772e2729f9c106cf4dabbacf7ba9f187fa32`
- `eval_report.json`（聚合输出）：`dd67f3699e917b00dfb3b4139a786e76f1186d6697e88352abe9abf5fc18674f`

## 复现命令

```bash
# 1) 标注池 → 评测输入（独立创建输出；已存在需 --force）
python scripts/sample_to_eval.py --input <标注后样本池.jsonl> --output <eval.jsonl> \
  --model-revision "deepseek-flash/qwen3.8-flash (mixed window)" \
  --rule-revision "t204-v13 (mixed-window caveat)"
# 2) 正式聚合
python -m app.reports.evaluation --input <eval.jsonl> --output <report.json>
```

## 口径与版本

- 抽样：2026-09-15 15:32（Asia/Shanghai），`sample_draw --seed 20260915`；候选池 799 → 分层抽 150。
- 类型分布：text 91 / image 46 / mixed 5 / share_card 4 / unknown 2 / video 1 / forward_record 1。
- 人工盲标：图片 52 条按编号看图（09-15 深夜）、文字/其他 98 条按 T01–T98 清单（09-16）；违规 36 / 正常 114。
- 判定窗口模型/规则为**混合口径**（未逐条冻结版本——限制见 `docs/evidence/2026-09-16-sample-evaluation.md` §3）。
- 降级：`unavailable=degraded` 1 条（R-113 F02 修复后自存储详情恢复）。

## 结论口径

`release_decision=REQUIRES_HUMAN_REVIEW`；precision/recall 门槛未达（见报告）；不得表述为发布通过或现场"误撤率"。
