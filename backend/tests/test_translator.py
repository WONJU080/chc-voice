from __future__ import annotations

import pytest

from chc_voice.translate.prompt import build_messages
from chc_voice.translate.translator import (
    ChatTranslator,
    TranslationError,
    clean_translation,
    looks_japanese,
)


def test_clean_translation_strips_wrappers() -> None:
    assert clean_translation("```\nこんにちは。\n```") == "こんにちは。"
    assert clean_translation("日语：こんにちは。") == "こんにちは。"
    assert clean_translation('"こんにちは。"') == "こんにちは。"


def test_looks_japanese() -> None:
    assert looks_japanese("今日は本当にいい天気ですね。")
    assert looks_japanese("こんにちは。")
    assert not looks_japanese("今天天气真不错。")
    assert not looks_japanese("今日はいい天気，散歩しよう。")  # Chinese comma
    assert not looks_japanese("")


@pytest.mark.asyncio
async def test_translator_retries_once_when_output_is_chinese() -> None:
    calls = []

    class Retrying(ChatTranslator):
        async def _request(self, text: str, *, retry: bool) -> str:
            calls.append(retry)
            return "今天天气不错。" if len(calls) == 1 else "今日はいい天気ですね。"

    translator = Retrying("key")
    result = await translator.translate("今天天气不错。")
    assert result == "今日はいい天気ですね。"
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_translator_raises_when_both_attempts_fail() -> None:
    class AlwaysChinese(ChatTranslator):
        async def _request(self, text: str, *, retry: bool) -> str:
            return "今天天气不错。"

    translator = AlwaysChinese("key")
    with pytest.raises(TranslationError):
        await translator.translate("今天天气不错。")


@pytest.mark.asyncio
async def test_empty_text_returns_empty() -> None:
    translator = ChatTranslator("key")
    assert await translator.translate("   ") == ""


def test_tone_instruction_is_included() -> None:
    casual = build_messages("你好", tone="casual")[0]["content"]
    keigo = build_messages("你好", tone="keigo")[0]["content"]
    assert "タメ口" in casual and "です、ます" in casual
    assert "敬語" in keigo
    assert casual != keigo


@pytest.mark.asyncio
async def test_translator_passes_tone_to_request() -> None:
    seen = []

    class Recorder(ChatTranslator):
        async def _request(self, text: str, *, retry: bool) -> str:
            seen.append(self.tone)
            return "今日はいい天気だね。"

    translator = Recorder("key", tone="casual")
    assert await translator.translate("今天天气不错。") == "今日はいい天気だね。"
    assert seen == ["casual"]
