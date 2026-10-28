"""Sentence/clause splitting for TTS and translation chunks."""

from __future__ import annotations

from .stable_prefix import ALL_PUNCT, safe_cut


def split_for_tts(text: str, max_chars: int = 40) -> list[str]:
    """Split committed text into phrase-sized chunks, keeping punctuation.

    Chunks are cut after Chinese/Japanese punctuation when possible, otherwise at a word
    boundary near ``max_chars``.  Numbers and Latin words are never cut in half.
    """
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        remaining = len(text) - start
        if remaining <= max_chars:
            chunks.append(text[start:].strip())
            break
        end = start + max_chars
        punct = [index + 1 for index in range(start, end) if text[index] in ALL_PUNCT]
        if punct:
            cut = punct[-1]
        else:
            cut = safe_cut(text, start, max_chars)
        piece = text[start:cut].strip()
        if piece:
            chunks.append(piece)
        start = cut
    return [chunk for chunk in chunks if chunk]
