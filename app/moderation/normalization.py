"""文本归一化与变体识别（T-103）。

针对实测样本中出现的规避手法（决策 D-012/D-013 样本）：
- 全角/半角、大写小写、零宽字符、重复符号
- 谐音/代称变体：「裙」→「群」、「米」→「元（钱）」、「薯」→「书」、
  「抖y/抖因」→「抖音」、「➕/＋」→「加」、「V/威/微」→「微」
归一化结果只用于规则匹配，不改变原始消息内容。
"""

from __future__ import annotations

import re
import unicodedata

# 常见谐音/代称变体映射（键为归一化后仍可能出现的写法）
VARIANT_MAP: dict[str, str] = {
    "裙": "群",
    "憋": "币",
    "米": "元",
    "薯": "书",
    "抖因": "抖音",
    "抖y": "抖音",
    "抖英": "抖音",
    "小红薯": "小红书",
    "红暑": "红书",
    "➕": "加",
    "＋": "加",
    "威信": "微信",
    "V信": "微信",
    "v信": "微信",
    "扣扣": "QQ",
    "企鹅": "QQ",
    "蚂蚁秒赚": "秒赚",
    "微我": "加我微",
}

_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_REPEAT_PUNCT = re.compile(r"([!！?？.。~～@#＠＃])\1{2,}")
_SPACE_RUN = re.compile(r"\s+")


def normalize(text: str) -> str:
    """归一化：NFKC、去零宽、压缩重复符号与空白、小写。"""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _REPEAT_PUNCT.sub(r"\1", text)
    text = _SPACE_RUN.sub(" ", text)
    return text.lower().strip()


def apply_variants(text: str) -> str:
    """在归一化基础上应用谐音/代称变体替换。"""
    result = normalize(text)
    for variant, canonical in VARIANT_MAP.items():
        result = result.replace(variant.lower(), canonical.lower())
    return result


def has_variant_trick(text: str) -> bool:
    """检测原文是否存在变体规避（归一化前后关键词空间不同即视为有变体）。"""
    return apply_variants(text) != normalize(text)
