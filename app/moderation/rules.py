"""文字规则引擎与刷屏检测（T-103）。

置信度模型（可解释、确定性，不使用大模型自由文本控制处罚）：
- 每条命中规则贡献 confidence_delta；总分 >= HIGH_THRESHOLD(0.90) 且命中
  两条以上独立信号（或命中明确黑名单词）=> violation_high（建议撤回+禁言+警告）；
- 有信号但未达阈值 => record_only（只记录转人工）；
- 无信号 => allow。

保护角色（群主/管理员）命中任何规则都只 record_only，绝不建议处罚（群规+架构约束）。
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from app.core.contracts import StandardMessage
from app.moderation.decision import ModerationDecision, RuleHit
from app.moderation.extract import extract_signals
from app.moderation.normalization import apply_variants, has_variant_trick

if TYPE_CHECKING:
    from app.moderation.dynamic_rules import RuleSnapshot

HIGH_THRESHOLD = 0.90

CONTEXT_SENSITIVE_AD_TERMS: tuple[str, ...] = (
    "兼职",
    "刷单",
    "一单",
    "日结",
    "秒结",
    "评论员",
    "试做",
)

DISCUSSION_MARKERS: tuple[str, ...] = (
    "靠谱吗",
    "靠谱么",
    "被骗",
    "骗局",
    "不要上当",
    "讨论",
    "不做",
    "拒绝",
    "真的假的",
    "是否可靠",
)

AD_INTENT_MARKERS: tuple[str, ...] = (
    "招",
    "招募",
    "招聘",
    "接单",
    "操作",
    "结算",
    "联系",
    "私聊",
    "加我",
    "加微",
    "群里到",
    "开始干活",
    "长期",
)

ALLOWED_SHARE_SOURCES: tuple[str, ...] = ("万能校园墙",)
SEVERE_CATEGORIES: set[str] = {"porn", "violence", "fraud"}

# 明确黑名单词（命中即贡献0.70，覆盖实测样本与常见违法词）。
# R-103 后，兼职/刷单/一单等语境敏感词不会单独形成硬证据，
# 必须结合广告意图且不处于反诈/询问/拒绝语境。
BLACKLIST_EXPLICIT: tuple[str, ...] = (
    "刷单",
    "代发一条",
    "口令红包",
    "红包结算",
    "日结",
    "兼职",
    "加我微信",
    "加微",
    "一单",
    "秒结",
    "评论员",
    "试做",
    "上分",
    "下分",
    "蚂蚁秒赚",
    "秒赚",
    "趣闲赚",
    "补单",
    "拼单返利",
    "点赞返利",
    "垫资",
    "押金垫付",
    "无需垫付",
    "色情",
    "裸聊",
    "援交",
    "约炮",
    "赌球",
    "网赌",
    "博彩",
    "上门服务",
    "毒品",
    "冰粉",
    "麻古",
    "枪支",
    "弹药",
)

# 弱信号词（首个0.30，每个额外+0.20，封顶0.90；组合到0.90即高置信）
SOFT_SIGNALS: tuple[str, ...] = (
    "招募",
    "招素人",
    "招聘",
    "日租",
    "闲置",
    "收购",
    "代打",
    "代发",
    "加我",
    "私聊",
    "有兴趣联系",
    "感兴趣",
    "长期干",
    "挣点外快",
    "无需押金",
    "日收益",
    "多劳多得",
    "结算",
    "当天结",
    "立结",
    "不代发",
    "发文",
    "推广单",
    "素人",
    "团长",
    "开播",
    "挂机",
    "上新",
    "接单",
    # 2026-09-05 负责人实测新增（房产/家教/推销类）
    "家教",
    "直招",
    "补习",
    "补课",
    "中介",
    "上门",
    "招收",
    "在卖",
    "出售",
    "出租",
    "商铺",
    "档口",
    "收租",
    "包租",
    "产权",
    "来电咨询",
    "开盘",
    "总价",
    "推销",
    "私信我",
    "长期有效",
    "数据标注",
)

FLOOD_WINDOW_SECONDS = 60.0
FLOOD_MAX_MESSAGES = 3  # 负责人确认：连续发3条一模一样即撤回+禁言
FLOOD_CONFIDENCE = 0.95  # 刷屏为负责人明确的确定性规则，单条规则即高置信

_HIGH_ACTIONS: tuple[str, ...] = ("recall", "mute", "warn")  # 永远不含 kick


def _fingerprint(msg: StandardMessage) -> str:
    """消息内容指纹：归一化文本 + 附件类型与大小（相同字样/图片/表情包视为相同）。"""
    att_part = ",".join(
        f"{a.content_type}:{a.size}"
        for a in sorted(msg.attachments, key=lambda a: (a.content_type, a.size or 0))
    )
    payload = f"{apply_variants(msg.text)}|{att_part}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evaluate_text_rules(
    text: str, blacklist: tuple[str, ...] = BLACKLIST_EXPLICIT
) -> tuple[list[RuleHit], float, str | None]:
    """返回（规则命中列表, 置信度合计, 主类别）。"""
    hits: list[RuleHit] = []
    total = 0.0
    category: str | None = None
    variant_text = apply_variants(text)

    discussion_context = _is_discussion_context(variant_text)
    sensitive_hits = [
        kw for kw in CONTEXT_SENSITIVE_AD_TERMS if kw in blacklist and kw in variant_text
    ]
    explicit_hits = [
        kw for kw in blacklist if kw in variant_text and kw not in CONTEXT_SENSITIVE_AD_TERMS
    ]
    hard_ad_combo = bool(sensitive_hits) and _has_ad_intent(variant_text) and not discussion_context
    if explicit_hits:
        hits.append(
            RuleHit(
                rule_id="R001",
                rule_name="explicit_blacklist",
                category="ad",
                confidence_delta=0.70,
                evidence_masked=f"{len(explicit_hits)}个黑名单词",
            )
        )
        total += 0.70
        category = category or "ad"

    if hard_ad_combo:
        hits.append(
            RuleHit(
                rule_id="R001",
                rule_name="explicit_ad_combo",
                category="ad",
                confidence_delta=0.70,
                evidence_masked=f"广告意图+{len(sensitive_hits)}个语境词",
            )
        )
        total += 0.70
        category = category or "ad"

    if sensitive_hits and not hard_ad_combo:
        hits.append(
            RuleHit(
                rule_id="R007",
                rule_name="contextual_ad_terms",
                category="ad",
                confidence_delta=min(0.25 + 0.10 * (len(sensitive_hits) - 1), 0.45),
                evidence_masked=f"{len(sensitive_hits)}个语境敏感词",
            )
        )
        total += min(0.25 + 0.10 * (len(sensitive_hits) - 1), 0.45)
        category = category or "ad"

    soft_hits = [kw for kw in SOFT_SIGNALS if kw in variant_text]
    if soft_hits:
        delta = min(0.30 + 0.20 * (len(soft_hits) - 1), 0.90)
        hits.append(
            RuleHit(
                rule_id="R002",
                rule_name="soft_signals",
                category="ad",
                confidence_delta=delta,
                evidence_masked=f"{len(soft_hits)}个弱信号",
            )
        )
        total += delta
        category = category or "ad"

    contact_signals = extract_signals(variant_text)
    if contact_signals:
        kinds = {s.kind for s in contact_signals}
        delta = min(0.25 + 0.15 * (len(kinds) - 1), 0.55)
        hits.append(
            RuleHit(
                rule_id="R003",
                rule_name="contact_extraction",
                category="ad",
                confidence_delta=delta,
                evidence_masked=",".join(sorted(kinds)),
            )
        )
        total += delta
        category = category or "ad"

    if has_variant_trick(text) and (explicit_hits or soft_hits or contact_signals):
        hits.append(
            RuleHit(
                rule_id="R004",
                rule_name="variant_obfuscation",
                category="ad",
                confidence_delta=0.10,
                evidence_masked="检测到谐音/代称变体",
            )
        )
        total += 0.10

    for porn_word in ("色情", "裸聊", "援交", "约炮"):
        if porn_word in variant_text:
            category = "porn"
            break
    if explicit_hits and explicit_hits[0] in (
        "枪支",
        "弹药",
        "毒品",
        "麻古",
        "冰粉",
        "赌球",
        "网赌",
        "博彩",
    ):
        category = (
            "violence" if explicit_hits[0] in ("枪支", "弹药", "毒品", "麻古", "冰粉") else "fraud"
        )

    return hits, min(total, 1.0), category


def _is_discussion_context(text: str) -> bool:
    return any(marker in text for marker in DISCUSSION_MARKERS)


def _has_ad_intent(text: str) -> bool:
    return any(marker in text for marker in AD_INTENT_MARKERS)


def _is_share_source_allowed(msg: StandardMessage) -> bool:
    if msg.kind != "share_card":
        return False
    card = msg.share_card
    source_text = " ".join(
        part
        for part in (
            card.source if card else "",
            card.title if card else "",
            card.prompt if card else "",
            card.tag if card else "",
            msg.text,
        )
        if part
    )
    return any(source in source_text for source in ALLOWED_SHARE_SOURCES)


class FrequencyTracker:
    """滑动窗口刷屏检测：同群同成员，相同内容指纹 60 秒内达到 3 条即触发。"""

    def __init__(
        self, window_seconds: float = FLOOD_WINDOW_SECONDS, max_messages: int = FLOOD_MAX_MESSAGES
    ) -> None:
        self._window = window_seconds
        self._max = max_messages
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def check(
        self, group_openid: str, member_openid: str, msg: StandardMessage, now: float | None = None
    ) -> bool:
        """返回 True 表示构成刷屏（本条为超限条目）。"""
        now = time.monotonic() if now is None else now
        key = f"{group_openid}:{member_openid}"
        fp = _fingerprint(msg)
        # 指纹纳入 key，保证「相同内容」才计数
        keyed = f"{key}:{fp}"
        dq = self._history[keyed]
        dq.append(now)
        while dq and now - dq[0] > self._window:
            dq.popleft()
        # 清理其他过期键，防膨胀（简化：定期惰性清理）
        if len(self._history) > 50_000:
            self._history.clear()
        return len(dq) >= self._max


def evaluate_text(
    text: str, blacklist: tuple[str, ...] = BLACKLIST_EXPLICIT
) -> tuple[list[RuleHit], float, str | None]:
    """对纯文本执行规则评分（供语音转写、文件内容等非直接消息来源复用）。"""
    return _evaluate_text_rules(text, blacklist)


class TextRuleEngine:
    """文字规则引擎：输入统一消息，输出确定性决策。"""

    def __init__(
        self,
        high_threshold: float = HIGH_THRESHOLD,
        frequency_tracker: FrequencyTracker | None = None,
        extra_blacklist: tuple[str, ...] = (),
        rule_snapshot: RuleSnapshot | None = None,
    ) -> None:
        self._high_threshold = high_threshold
        self._blacklist = BLACKLIST_EXPLICIT + tuple(extra_blacklist)
        self.frequency = frequency_tracker or FrequencyTracker()
        self._rule_snapshot = rule_snapshot

    def set_rule_snapshot(self, rule_snapshot: RuleSnapshot | None) -> None:
        """替换运行时动态规则快照，同时保留刷屏等进程内状态。"""
        self._rule_snapshot = rule_snapshot

    def evaluate(
        self,
        msg: StandardMessage,
        *,
        now: float | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> ModerationDecision:
        """评估一条消息，返回决策（决策结构中永不存在 kick）。"""
        hits, confidence, category = _evaluate_text_rules(msg.text, self._blacklist)
        protected = msg.sender.role in ("owner", "admin")

        flood = self.frequency.check(msg.external_group_id, msg.external_user_id, msg, now=now)
        if flood:
            hits.append(
                RuleHit(
                    rule_id="R005",
                    rule_name="flood",
                    category="flood",
                    confidence_delta=FLOOD_CONFIDENCE,
                    evidence_masked="60秒内相同内容超过3条（连续刷屏）",
                )
            )
            confidence = max(confidence, FLOOD_CONFIDENCE)
            category = category or "flood"

        verdict: str
        actions: list[str] = []
        reason = ""

        share_card = msg.kind == "share_card"
        share_source_allowed = _is_share_source_allowed(msg)
        if share_card and not share_source_allowed:
            hits.append(
                RuleHit(
                    rule_id="R006",
                    rule_name="share_card",
                    category="ad",
                    confidence_delta=0.0,
                    evidence_masked="未知来源分享卡片",
                )
            )
            category = category or "ad"

        has_hard_blacklist = any(h.rule_id == "R001" for h in hits)

        if protected:
            verdict = "record_only"
            reason = "保护角色（群主/管理员）：命中信号仅记录，不处罚"
        elif share_card and share_source_allowed:
            if category in SEVERE_CATEGORIES:
                verdict = "record_only"
                reason = "允许来源分享卡片含高风险类别信号，转人工复核"
            else:
                verdict = "allow"
                confidence = 0.0
                actions = []
                reason = "允许来源分享卡片，放行"
        elif share_card and not has_hard_blacklist and confidence < self._high_threshold:
            verdict = "record_only"
            actions = []
            reason = "未知来源分享卡片，转人工复核"
        elif share_card:
            verdict = "violation_high"
            confidence = max(confidence, 0.95)
            actions = list(_HIGH_ACTIONS)
            reason = "未知来源分享卡片含明确引流或违规证据"
        elif has_hard_blacklist:
            # R-102 审计选项A：命中明确黑名单词（R001）即硬证据，直接高置信违规
            verdict = "violation_high"
            confidence = max(confidence, self._high_threshold)
            actions = list(_HIGH_ACTIONS)
            reason = (
                f"命中明确黑名单词（硬证据R001），置信度{confidence:.2f}≥{self._high_threshold}"
            )
        elif confidence >= self._high_threshold:
            verdict = "violation_high"
            actions = list(_HIGH_ACTIONS)
            reason = f"置信度{confidence:.2f}≥{self._high_threshold}，命中{len(hits)}条规则"
        elif hits:
            verdict = "record_only"
            reason = f"置信度{confidence:.2f}未达阈值，只记录转人工"
        else:
            verdict = "allow"
            reason = "未命中任何规则"

        decision = ModerationDecision(
            message_id=msg.message_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            sender_role=msg.sender.role,
            verdict=verdict,
            category=category,
            confidence=round(confidence, 2),
            rule_hits=hits,
            recommended_actions=actions,
            reason=reason,
            is_protected_sender=protected,
        )
        return self._merge_dynamic_decision(msg, decision)

    def _merge_dynamic_decision(
        self, msg: StandardMessage, base: ModerationDecision
    ) -> ModerationDecision:
        if self._rule_snapshot is None:
            return base

        from app.moderation.dynamic_rules import DynamicRuleEngine

        dynamic = DynamicRuleEngine(self._rule_snapshot).evaluate(msg)
        if not dynamic.rule_hits:
            return base
        if dynamic.verdict == "allow" and base.verdict == "violation_high":
            return base.model_copy(
                update={
                    "verdict": "record_only",
                    "recommended_actions": [],
                    "confidence": min(base.confidence, 0.85),
                    "rule_hits": base.rule_hits + dynamic.rule_hits,
                    "reason": base.reason + "；动态允许规则冲突，转人工复核",
                }
            )
        if dynamic.verdict == "violation_high" and base.verdict != "violation_high":
            return dynamic
        if dynamic.verdict == "record_only" and base.verdict == "allow":
            return dynamic
        if dynamic.verdict == "allow" and base.verdict == "allow":
            return dynamic
        return base.model_copy(update={"rule_hits": base.rule_hits + dynamic.rule_hits})
