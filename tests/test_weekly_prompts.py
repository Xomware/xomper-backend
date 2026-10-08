"""
Tests for `lambdas.common.weekly_prompts`.

Locks the prompt structure that F3 sends to Anthropic. Drift in
either system block, in the JSON envelope spec, or in the user
prompt body would land badly on Claude's output, so these are
intentionally noisy.
"""
from __future__ import annotations

import pytest

from lambdas.common.constants import AI_REVIEW_WEEKLY_PROMPT_VERSION
from lambdas.common.league_lore import LEAGUE_LORE
from lambdas.common.weekly_prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT_LORE,
    SYSTEM_PROMPT_OUTPUT_SPEC,
    SYSTEM_PROMPT_TONE,
    build_system_blocks,
    build_user_prompt,
)


class TestSystemBlocks:
    def test_returns_three_blocks(self) -> None:
        blocks = build_system_blocks()
        assert len(blocks) == 3

    def test_all_blocks_are_text_with_ephemeral_cache(self) -> None:
        for block in build_system_blocks():
            assert block["type"] == "text"
            assert block["cache_control"] == {"type": "ephemeral"}
            assert isinstance(block["text"], str)
            assert block["text"].strip(), "system block text is empty"

    def test_block_one_is_tone_anchored_on_weekly(self) -> None:
        blocks = build_system_blocks()
        text = blocks[0]["text"]
        assert text == SYSTEM_PROMPT_TONE
        # Weekly-tuned anchors.
        for phrase in [
            "weekly",
            "Tuesday",
            "roast",
            "matchup",
        ]:
            assert phrase in text, f"missing phrase: {phrase!r}"

    def test_block_one_keeps_safety_rails_verbatim(self) -> None:
        text = build_system_blocks()[0]["text"]
        for phrase in [
            "Safety rails",
            "No slurs",
            "Do NOT invent NFL news",
            "PG-13",
        ]:
            assert phrase in text, f"missing safety phrase: {phrase!r}"

    def test_block_two_is_the_shared_lore_block(self) -> None:
        blocks = build_system_blocks()
        lore = blocks[1]["text"]
        from lambdas.common.lore_prompt import LORE_TEXT

        assert lore == LORE_TEXT
        assert lore == SYSTEM_PROMPT_LORE

    def test_block_two_contains_all_twelve_managers(self) -> None:
        lore = build_system_blocks()[1]["text"]
        for uid, profile in LEAGUE_LORE.items():
            assert uid in lore, f"missing user_id in lore: {uid}"
            assert profile.display_name in lore, (
                f"missing display_name in lore: {profile.display_name}"
            )

    def test_block_three_is_the_json_envelope_spec(self) -> None:
        blocks = build_system_blocks()
        spec = blocks[2]["text"]
        assert spec == SYSTEM_PROMPT_OUTPUT_SPEC
        for phrase in [
            "OUTPUT FORMAT",
            "body_markdown",
            "new_memories",
            "manager_user_id",
            "sentiment",
            "roast",
            "praise",
            "lore",
        ]:
            assert phrase in spec, f"missing envelope phrase: {phrase!r}"

    def test_prompt_version_pinned(self) -> None:
        assert PROMPT_VERSION == AI_REVIEW_WEEKLY_PROMPT_VERSION
        assert PROMPT_VERSION.startswith("f3-weekly-")


class TestBuildUserPrompt:
    def _sample_matchups(self, count: int = 6) -> list[dict]:
        matchups = []
        for i in range(count):
            winner = {
                "roster_id": (i * 2) + 1,
                "user_id": f"u{(i * 2) + 1}",
                "manager_display_name": f"Manager{(i * 2) + 1}",
                "team_name": f"Team{(i * 2) + 1}",
                "points": 130.0 - i,
                "bench_points": 20.0 + i,
            }
            loser = {
                "roster_id": (i * 2) + 2,
                "user_id": f"u{(i * 2) + 2}",
                "manager_display_name": f"Manager{(i * 2) + 2}",
                "team_name": f"Team{(i * 2) + 2}",
                "points": 90.0 + i,
                "bench_points": 30.0 - i,
            }
            matchups.append(
                {
                    "matchup_id": i + 1,
                    "winner": winner,
                    "loser": loser,
                    "margin": winner["points"] - loser["points"],
                    "is_tie": False,
                }
            )
        return matchups

    def _sample_memories(self, count: int = 4) -> list[dict]:
        return [
            {
                "memory_id": f"mid{i}",
                "season": "2026",
                "week": i + 1,
                "text": f"memory snippet {i + 1}",
                "sentiment": ["roast", "praise", "lore"][i % 3],
                "manager_user_id": f"u{i + 1}" if i % 2 == 0 else None,
            }
            for i in range(count)
        ]

    def test_includes_week_header(self) -> None:
        prompt = build_user_prompt(
            league_name="CLT DYNASTY",
            season="2026",
            week=4,
            matchups=[],
            prior_memories=[],
        )
        assert "Week 4" in prompt

    def test_renders_all_matchups_with_scores(self) -> None:
        matchups = self._sample_matchups(6)
        prompt = build_user_prompt(
            league_name="CLT DYNASTY",
            season="2026",
            week=4,
            matchups=matchups,
            prior_memories=[],
        )
        for m in matchups:
            assert f"Matchup #{m['matchup_id']}" in prompt
            assert m["winner"]["manager_display_name"] in prompt
            assert m["loser"]["manager_display_name"] in prompt
            # The lore is keyed by user_id; without it the model guesses
            # who a Sleeper handle belongs to.
            assert f"user_id {m['winner']['user_id']}" in prompt
            assert f"user_id {m['loser']['user_id']}" in prompt

    def test_renders_league_high_and_low(self) -> None:
        matchups = self._sample_matchups(2)
        prompt = build_user_prompt(
            league_name="CLT",
            season="2026",
            week=4,
            matchups=matchups,
            prior_memories=[],
        )
        assert "League high" in prompt
        assert "League low" in prompt

    def test_renders_prior_memories_bullets(self) -> None:
        memories = self._sample_memories(4)
        prompt = build_user_prompt(
            league_name="CLT",
            season="2026",
            week=4,
            matchups=[],
            prior_memories=memories,
        )
        for mem in memories:
            assert mem["text"] in prompt
        assert "Season memories" in prompt

    def test_empty_matchups_fallback_text(self) -> None:
        prompt = build_user_prompt(
            league_name="CLT",
            season="2026",
            week=4,
            matchups=[],
            prior_memories=[],
        )
        assert "No matchup data" in prompt

    def test_empty_memories_fallback_text(self) -> None:
        prompt = build_user_prompt(
            league_name="CLT",
            season="2026",
            week=1,
            matchups=[],
            prior_memories=[],
        )
        assert "No prior memories" in prompt

    def test_task_footer_includes_json_envelope_instruction(self) -> None:
        prompt = build_user_prompt(
            league_name="CLT",
            season="2026",
            week=4,
            matchups=[],
            prior_memories=[],
        )
        assert "## Your task" in prompt
        assert "JSON envelope" in prompt
        assert "new_memories" in prompt

    def test_tie_renders_tie_marker(self) -> None:
        matchup = {
            "matchup_id": 1,
            "winner": {
                "roster_id": 1,
                "user_id": "u1",
                "manager_display_name": "Manager1",
                "team_name": "Team1",
                "points": 110.0,
                "bench_points": 10.0,
            },
            "loser": {
                "roster_id": 2,
                "user_id": "u2",
                "manager_display_name": "Manager2",
                "team_name": "Team2",
                "points": 110.0,
                "bench_points": 12.0,
            },
            "margin": 0.0,
            "is_tie": True,
        }
        prompt = build_user_prompt(
            league_name="CLT",
            season="2026",
            week=4,
            matchups=[matchup],
            prior_memories=[],
        )
        assert "TIE" in prompt
