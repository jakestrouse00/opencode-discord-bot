"""Unit tests for the pure-stdlib text helpers in ``text_utils.py``."""

from opencode_discord_bot.text_utils import (
    DISCORD_MSG_MAX,
    _CHANNEL_NAME_MAX,
    _split_message,
    _extract_text,
    _final_assistant_text,
    _slugify_prompt,
)


class TestSplitMessage:
    def test_empty_returns_list_with_empty_string(self):
        assert _split_message("") == [""]

    def test_short_text_returned_unchanged(self):
        assert _split_message("hello") == ["hello"]

    def test_exact_limit_is_one_chunk(self):
        text = "x" * DISCORD_MSG_MAX
        assert _split_message(text) == [text]

    def test_over_limit_splits_at_limit(self):
        text = "x" * (DISCORD_MSG_MAX + 10)
        chunks = _split_message(text, limit=DISCORD_MSG_MAX)
        assert len(chunks) == 2
        assert len(chunks[0]) == DISCORD_MSG_MAX
        assert "".join(chunks) == text

    def test_prefers_code_fence_boundary(self):
        # A code block that closes near the limit should split right after
        # the closing fence so the block stays intact in the first chunk.
        before_fence = "y" * (DISCORD_MSG_MAX - 5)
        text = before_fence + "\n```\ncode\n```\nmore"
        chunks = _split_message(text)
        # The first chunk should contain the closing fence.
        assert "```" in chunks[0]
        assert "".join(chunks) == text

    def test_prefers_double_newline(self):
        body = "a" * (DISCORD_MSG_MAX - 5) + "\n\nsecond paragraph"
        chunks = _split_message(body)
        assert len(chunks) == 2
        assert "".join(chunks) == body

    def test_prefers_single_newline(self):
        body = "a" * (DISCORD_MSG_MAX - 5) + "\nsecond line"
        chunks = _split_message(body)
        assert len(chunks) == 2
        assert "".join(chunks) == body


class TestExtractText:
    def test_empty_parts(self):
        assert _extract_text([]) == ""

    def test_text_part(self):
        assert _extract_text([{"type": "text", "text": "hi"}]) == "hi"

    def test_multiple_text_parts_joined_with_newline(self):
        parts = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert _extract_text(parts) == "a\nb"

    def test_skips_non_text_parts(self):
        parts = [
            {"type": "image", "url": "x"},
            {"type": "text", "text": "kept"},
            {"type": "tool", "name": "y"},
        ]
        assert _extract_text(parts) == "kept"

    def test_skips_empty_text(self):
        assert _extract_text([{"type": "text", "text": ""}, {"type": "text", "text": "x"}]) == "x"


class TestFinalAssistantText:
    def test_empty_messages(self):
        assert _final_assistant_text([]) == ""

    def test_picks_last_assistant(self):
        msgs = [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "q"}]},
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "first"}]},
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "second"}]},
        ]
        assert _final_assistant_text(msgs) == "second"

    def test_falls_back_to_last_any_text_when_no_assistant(self):
        msgs = [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "only"}]},
        ]
        assert _final_assistant_text(msgs) == "only"

    def test_skips_non_dict_entries(self):
        msgs = ["junk", {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "ok"}]}]
        assert _final_assistant_text(msgs) == "ok"


class TestSlugifyPrompt:
    def test_empty_returns_fallback(self):
        assert _slugify_prompt("", "fallback") == "fallback"

    def test_all_symbols_returns_fallback(self):
        assert _slugify_prompt("@#$%^&*", "fallback") == "fallback"

    def test_takes_first_six_words(self):
        slug = _slugify_prompt("the quick brown fox jumps over the lazy dog", "fb")
        words = slug.split("-")
        assert len(words) <= 6

    def test_lowercases(self):
        slug = _slugify_prompt("Hello WORLD", "fb")
        assert slug == "hello-world"

    def test_collapses_non_alnum_to_hyphen(self):
        slug = _slugify_prompt("hello! world", "fb")
        assert slug == "hello-world"

    def test_collapses_consecutive_hyphens(self):
        slug = _slugify_prompt("hello   world", "fb")
        assert slug == "hello-world"

    def test_strips_leading_trailing_hyphens(self):
        slug = _slugify_prompt("!!!hello world!!!", "fb")
        assert slug == "hello-world"

    def test_capped_at_channel_name_max(self):
        long_prompt = " ".join(["word"] * 30)
        slug = _slugify_prompt(long_prompt, "fb")
        assert len(slug) <= _CHANNEL_NAME_MAX