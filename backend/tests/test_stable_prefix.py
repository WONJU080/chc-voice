from __future__ import annotations

from chc_voice.text.segmenter import split_for_tts
from chc_voice.text.stable_prefix import CommitRule, StablePrefixCommitter, longest_common_prefix


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_longest_common_prefix() -> None:
    assert longest_common_prefix(["今天", "今天天", "今天天气"]) == "今天"
    assert longest_common_prefix([]) == ""
    assert longest_common_prefix(["", "abc"]) == ""


def test_waits_for_min_chars_and_stability() -> None:
    clock = Clock()
    committer = StablePrefixCommitter(CommitRule(stable_ms=250, min_chars=6, max_chars=40), clock=clock)
    assert committer.feed("今天天气") == []
    clock.advance(0.1)
    assert committer.feed("今天天气真") == []
    clock.advance(0.1)
    # No punctuation and fewer than max_chars: hold for a pause.
    assert committer.feed("今天天气真不错") == []
    clock.advance(0.6)
    commits = committer.tick()
    assert [commit.text for commit in commits] == ["今天天气真不错"]
    assert commits[0].reason == "pause"


def test_punctuation_commits_before_final() -> None:
    clock = Clock()
    committer = StablePrefixCommitter(CommitRule(stable_ms=200, min_chars=6, max_chars=40), clock=clock)
    partials = ["今天天气真不错，", "今天天气真不错，我们", "今天天气真不错，我们出去"]
    commits = []
    for text in partials:
        clock.advance(0.1)
        commits.extend(committer.feed(text))
    clock.advance(0.1)
    commits.extend(committer.feed("今天天气真不错，我们出去走走吧。", final=True))
    assert any(commit.reason == "punctuation" for commit in commits)
    assert "".join(commit.text for commit in commits) == "今天天气真不错，我们出去走走吧。"


def test_final_never_duplicates_committed_text() -> None:
    clock = Clock()
    committer = StablePrefixCommitter(CommitRule(stable_ms=100, min_chars=2, max_chars=40), clock=clock)
    text = "你好，世界。"
    commits = []
    for index in range(1, len(text) + 1):
        clock.advance(0.1)
        commits.extend(committer.feed(text[:index]))
    commits.extend(committer.feed(text, final=True))
    assert "".join(commit.text for commit in commits) == text
    assert committer.committed == 0  # reset after final


def test_no_retraction_when_asr_revises() -> None:
    clock = Clock()
    committer = StablePrefixCommitter(CommitRule(stable_ms=100, min_chars=2, max_chars=40), clock=clock)
    clock.advance(0.1)
    commits = committer.feed("你好，世界。")
    assert commits
    spoken = "".join(commit.text for commit in commits)
    # A revision shorter than what was already spoken must not remove it.
    clock.advance(0.1)
    committer.feed("你好")
    assert committer.committed >= len(spoken)
    assert committer.revisions >= 1


def test_max_chars_splits_without_breaking_ascii_word() -> None:
    clock = Clock()
    committer = StablePrefixCommitter(CommitRule(stable_ms=80, min_chars=4, max_chars=12), clock=clock)
    text = "this is a long sentence with words"
    for index in range(1, len(text) + 1, 2):
        clock.advance(0.05)
        commits = committer.feed(text[:index])
    for chunk in commits:
        assert len(chunk.text) <= 13


def test_mode_b_commits_at_comma_even_below_min_chars() -> None:
    clock = Clock()
    committer = StablePrefixCommitter(CommitRule(stable_ms=200, min_chars=10, max_chars=60), clock=clock)
    # Eight characters plus a comma: below min_chars 10, but a comma is a clause boundary.
    commits = []
    for text in ["今天天气真不错，", "今天天气真不错，我们", "今天天气真不错，我们下午"]:
        clock.advance(0.1)
        commits.extend(committer.feed(text))
    assert commits and commits[0].text.endswith("，")
    assert commits[0].reason == "punctuation"


def test_segmenter_keeps_punctuation_and_respects_limit() -> None:
    assert split_for_tts("你好，世界。", 20) == ["你好，世界。"]
    chunks = split_for_tts("第一句。第二句。第三句。", 5)
    assert all(len(chunk) <= 5 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == "第一句。第二句。第三句。"
