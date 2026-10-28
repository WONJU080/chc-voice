"""Chinese -> Japanese translation over an OpenAI-compatible Chat Completions API."""

from __future__ import annotations

import re

import httpx

from ..state import VoiceError
from .prompt import DEFAULT_TONE, build_messages

CHINESE_PUNCT = "，；：（）【】“”‘’,;:"
KANA = re.compile(r"[\u3040-\u30ff]")
HAN = re.compile(r"[\u4e00-\u9fff]")
DIGITS = "0123456789"
DIGIT_READINGS = {
    "0": "ゼロ", "1": "いち", "2": "に", "3": "さん", "4": "よん",
    "5": "ご", "6": "ろく", "7": "なな", "8": "はち", "9": "きゅう",
}
REFUSAL_MARKERS = (
    "入力してください", "翻訳する内容がありません", "翻訳したい中国語",
    "中文内容", "请输入", "请提供", "没有需要翻译", "无法翻译",
    "内容がありません", "テキストを入力",
)


class TranslationError(VoiceError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__("翻译 LLM", message, status=status)


def clean_translation(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    for prefix in ("日语：", "日文：", "日语:", "日文:", "翻译：", "翻译:", "Japanese:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.strip().strip('"').strip("'").strip()
    return text


def is_refusal(text: str) -> bool:
    return any(marker in text for marker in REFUSAL_MARKERS)


def digits_to_japanese(text: str) -> str:
    """Read a digit string the way it was likely spoken: one digit at a time."""
    pieces: list[str] = []
    for char in text:
        if char in DIGIT_READINGS:
            pieces.append(DIGIT_READINGS[char])
        elif char in " ,，、\t":
            pieces.append("、")
        else:
            pieces.append(char)
    result = re.sub(r"、+", "、", "".join(pieces)).strip("、 ")
    return result


def is_number_like(text: str) -> bool:
    return bool(text) and all(char.isdigit() or char in " .,，。、!?！？" for char in text)


def looks_japanese(text: str) -> bool:
    """Natural Japanese has kana; Chinese text with no kana is a failed translation."""
    if not text or is_refusal(text):
        return False
    if any(char in CHINESE_PUNCT for char in text):
        return False
    kana = len(KANA.findall(text))
    han = len(HAN.findall(text))
    if kana == 0:
        return False
    # A short phrase may be mostly kanji, but not without any kana.
    return kana >= 2 or (han > 0 and kana >= 1)


class ChatTranslator:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        *,
        proxy: str | None = None,
        timeout: float = 20.0,
        tone: str = DEFAULT_TONE,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.proxy = proxy or None
        self.timeout = timeout
        self.tone = tone

    async def translate(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        # Numbers and English should not be sent to the translator as if they were
        # Chinese prose; a model may refuse or ask for Chinese input.
        if not HAN.search(text):
            if is_number_like(text):
                return digits_to_japanese(text)
            return text
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = await self._request(text, retry=attempt > 0)
            except TranslationError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    continue
                break
            cleaned = clean_translation(raw)
            if is_refusal(cleaned):
                last_error = TranslationError("翻译服务没有给出译文，要求输入中文")
            elif looks_japanese(cleaned):
                return cleaned
            else:
                last_error = TranslationError("翻译结果不像日语，已丢弃")
            if attempt == 0:
                continue
        if isinstance(last_error, TranslationError):
            raise last_error
        raise TranslationError(f"翻译失败：{last_error}")

    async def _request(self, text: str, *, retry: bool) -> str:
        payload = {
            "model": self.model,
            "messages": build_messages(text, retry=retry, tone=self.tone),
            "temperature": 0.2,
            "max_tokens": 300,
            "stream": False,
        }
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy, timeout=self.timeout, trust_env=False
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise TranslationError(f"翻译服务连接失败：{exc}") from exc
        if response.status_code != 200:
            message = {
                401: "翻译 LLM 拒绝了这个 key（HTTP 401）",
                403: "翻译 LLM 拒绝访问（HTTP 403）",
                429: "翻译 LLM 限流（HTTP 429）",
            }.get(response.status_code, f"翻译 LLM 返回 HTTP {response.status_code}")
            raise TranslationError(message, status=response.status_code)
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError("翻译 LLM 返回了无法解析的结果") from exc
        if not isinstance(content, str):
            raise TranslationError("翻译 LLM 没有返回文本")
        return content

    async def test(self, text: str = "你好，很高兴认识你。") -> str:
        return await self.translate(text)
