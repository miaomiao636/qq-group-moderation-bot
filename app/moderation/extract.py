"""联系方式与链接提取（T-103）。

覆盖实测样本中的形态：手机号、QQ号、QQ群号、微信号、支付宝口令、URL/域名。
提取结果只保留类型与遮蔽摘要（不落原始联系方式到日志）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_QQ_NUMBER = re.compile(r"(?<!\d)[1-9]\d{4,10}(?!\d)")
_WECHAT = re.compile(
    r"(?:微信|加微|加v|v信|微信号)[^\da-z]{0,3}([a-zA-Z][a-zA-Z0-9_-]{5,19})", re.IGNORECASE
)
_URL = re.compile(r"(?:https?://|www\.)[^\s，。,；;）)】]+")
_DOMAIN = re.compile(
    r"(?<![\w.])(?:[a-z0-9-]+\.)+(?:com|cn|net|org|top|xyz|vip|cc|me|tv|app|site|shop)(?![\w.])",
    re.IGNORECASE,
)
_ALIPAY = re.compile(r"支付宝口令|口令红包")


@dataclass(frozen=True)
class ExtractedSignal:
    kind: str  # phone / qq / wechat / url / domain / alipay
    masked: str  # 遮蔽后的摘要


def _mask(value: str, keep_head: int = 3, keep_tail: int = 2) -> str:
    if len(value) <= keep_head + keep_tail:
        return "*" * len(value)
    return value[:keep_head] + "*" * (len(value) - keep_head - keep_tail) + value[-keep_tail:]


def extract_signals(text: str) -> list[ExtractedSignal]:
    """从（已归一化的）文本提取联系方式与链接信号。"""
    signals: list[ExtractedSignal] = []
    for match in _PHONE.findall(text):
        signals.append(ExtractedSignal("phone", _mask(match)))
    for match in _WECHAT.finditer(text):
        signals.append(ExtractedSignal("wechat", _mask(match.group(1))))
    for match in _URL.findall(text):
        signals.append(ExtractedSignal("url", _mask(match, 6, 4)))
    for match in _ALIPAY.finditer(text):
        signals.append(ExtractedSignal("alipay", match.group(0)))
    domain_match = _DOMAIN.search(text)
    if domain_match:
        signals.append(ExtractedSignal("domain", _mask(domain_match.group(0), 4, 2)))
    # QQ号：QQ/扣扣/企鹅/群号 关键词后的数字，避免把普通数字当QQ号
    for kw_match in re.finditer(
        r"(?:qq|扣扣|企鹅|群号|裙号|账号|帐号|浩)\s*[：:为是]?\s*(\d{5,11})", text, re.IGNORECASE
    ):
        signals.append(ExtractedSignal("qq", _mask(kw_match.group(1))))
    return signals


def has_contact_signal(text: str) -> bool:
    return bool(extract_signals(text))
