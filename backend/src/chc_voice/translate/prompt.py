"""The translation prompt for mode B.

No character persona, no memory, no chat: this is a speech translator.
"""

from __future__ import annotations

SYSTEM_PROMPT = """你是语音翻译器。
把用户的中文口语翻译成自然的日语。
保留原意、语气和重要信息。
输入可能包含阿拉伯数字、英文或很短的词；数字串按逐位读法（例如 123 → いち、に、さん）。
不要拒绝，不要要求用户补充中文，不要添加解释、括号或中文。
只输出日语文本。"""

DEFAULT_TONE = "casual"

TONE_PROMPTS = {
    "casual": (
        "语气：日语口语常体（タメ口），像关系亲近的人自然说话。"
        "不要使用です、ます、ございます等敬语，不要使用书面语。"
    ),
    "neutral": (
        "语气：自然的日语口语常体，避免过度敬语。"
        "必要时可以用轻度的です・ます，但不要堆砌尊敬語・謙譲語。"
    ),
    "polite": (
        "语气：礼貌的です・ます体，保持自然。"
        "不要过度使用尊敬語・謙譲語，不要像客服念稿。"
    ),
    "keigo": (
        "语气：正式的商务敬语，适当使用尊敬語・謙譲語，适合对客户或上级说话。"
    ),
}

RETRY_SUFFIX = """

上一次输出不是日语（没有假名或含有中文标点）。请只输出自然的日语，不要解释。"""


def build_messages(text: str, *, retry: bool = False, tone: str = DEFAULT_TONE) -> list[dict[str, str]]:
    instruction = TONE_PROMPTS.get(tone, TONE_PROMPTS[DEFAULT_TONE])
    system = SYSTEM_PROMPT + "\n" + instruction + (RETRY_SUFFIX if retry else "")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text.strip()},
    ]
