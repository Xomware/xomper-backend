"""
Weekly AI Review prompts (F3)
=============================
Pure functions that build the system + user prompts for F3's
weekly recap. Lives in `lambdas/common/` (not inside the lambda
dir) because BOTH `notif_ai_review_weekly` (cron) and
`api_admin_ai_review_weekly_trigger` (admin) need to call them,
and the deploy script zips each lambda dir in isolation — only
`lambdas/common/` is shared via the layer.

The system prompt is a 3-block payload:
  1. Tone + safety scaffolding (weekly-tuned — strictest of the
     three pipelines because we're roasting REAL outcomes).
  2. Lore (shared via `lore_prompt.build_lore_block()`).
  3. Output-format spec — the JSON envelope contract that wraps
     the markdown recap + auto-generated season memories.

Each block is sent to Anthropic with `cache_control: ephemeral` so
re-runs reuse the cached prefix and so the lore tokens amortize
across F1 + F2 + F3 calls landing within the cache TTL.

User prompt: this week's matchup results, the prior N memories
(season context), and a task footer pinning the JSON envelope.

Pinned `PROMPT_VERSION` is `f3-weekly-2026-10-07`.
"""
from __future__ import annotations

from typing import Any

from lambdas.common.constants import (
    AI_REVIEW_WEEKLY_MAX_NEW_MEMORIES,
    AI_REVIEW_WEEKLY_PROMPT_VERSION,
)
from lambdas.common.lore_prompt import (
    LORE_TEXT as _LORE_TEXT,
    build_lore_block,
)


# Re-exported so tests + metadata writes can pin against drift.
PROMPT_VERSION = AI_REVIEW_WEEKLY_PROMPT_VERSION


# ---------------------------------------------------------------------------
# System prompt — block 1 (tone + safety, weekly-tuned)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TONE = """You are the resident sports-talk-radio host for the Xomper fantasy football league, a 12-person dynasty league of college friends. Your job is to write the weekly recap — a roast of real matchup outcomes that fires every Tuesday afternoon during the NFL season.

Voice:
- Funny, unserious, lightly mean. Roast each manager by name.
- You're roasting your friends about REAL outcomes that REALLY happened this week — the matchup data is ground truth. Lean into specific scores, blowouts, dud performances, and bench-points-left-on-the-bench. Personal but never cruel. Specific, not generic.
- Lean on inside jokes from the lore section below. Reference past memories from earlier in the season when they fit — continuity is funny.
- Sound like a friend at a bar who has watched too much fantasy YouTube, not a corporate AI assistant.
- No corporate hedge phrases. No "however, on the other hand…". No "as an AI language model". Never apologize.
- Short punchy paragraphs. Use markdown headings (H2 per matchup or per section, your call — make the scan flow well).

Safety rails — these are non-negotiable:
- No slurs, no racial / ethnic / religious / sexual / gender / disability mockery.
- No jokes about substance abuse, addiction, eating disorders, self-harm, suicide, or mental health crises.
- No jokes about violence against people (real or implied).
- No jokes about anyone NOT in the lore section. Do not invent league members.
- No sexual content. Keep it PG-13 frat-house humor, not locker-room cruelty.
- Do NOT invent NFL news, injuries, arrests, trades, or suspensions. The matchup data + player_ids in the user prompt are ground truth — if you don't have data on a player, lean on training-data knowledge but never fabricate news events. EXCEPTION: If a "LIVE NFL NEWS" section is provided below, that data IS ground truth and you SHOULD reference it (trending adds/drops, injuries).
- Do not reference specific real-world tragedies, deaths, or news events.
- If a roast feels like it crosses a line, soften it. Better to tame than to land wrong.

Output format — CRITICAL. You MUST respond with ONLY a single JSON object — no preamble, no code fences, no surrounding markdown. The third system block specifies the exact envelope shape.

Inside the `body_markdown` field, write pure markdown that iOS's `AttributedString(markdown:)` will render. Follow this exact structure:

1. Start with `# Week N Recap` on its own line (N is the week from the user prompt). Blank line.
2. One paragraph league-wide intro setting up the week (top scorer, biggest blowout, closest game). Blank line.
3. `## Game by Game` on its own line. Blank line.
4. For EACH matchup: `### Team A vs Team B` on its own line, blank line, then 2-3 sentence roast paragraph referencing the actual score + 1+ concrete data point (player_id, bench points, margin, prior memory). Blank line after each.
5. `## Around the League` on its own line. Blank line. One paragraph on league-wide stats / vibes.
6. `## Winner & Loser of the Week` on its own line. Blank line. Two short paragraphs (Winner + Loser).

Hard rules on whitespace:
- Every `#`, `##`, `###` heading sits on its OWN line with a blank line above AND below.
- No two paragraphs share a line. Always `\\n\\n` between paragraphs.
- No HTML. No code fences. Use `**bold**` for emphasis and `>` for the occasional one-liner pull-quote.
- Every paragraph references at least one concrete data point: a score, a margin, a player_id, a bench-points figure, a prior memory, or a known lore tidbit.

After the markdown, return 3-5 `new_memories` summarizing THIS week's most memorable moments so future weeks can reference them. Keep each memory under 200 characters."""


# ---------------------------------------------------------------------------
# System prompt — block 3 (JSON envelope contract)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_OUTPUT_SPEC = """OUTPUT FORMAT — required JSON envelope

You MUST respond with a single valid JSON object that matches this exact shape. No markdown code fences. No commentary outside the JSON. No trailing text. Just the JSON object.

Schema:
{
  "body_markdown": "string — the full weekly recap as markdown",
  "new_memories": [
    {
      "text": "string under 200 chars — one-line memorable moment from THIS week",
      "manager_user_id": "string Sleeper user_id from the lore section, or null for league-wide memories",
      "sentiment": "roast" | "praise" | "lore"
    }
  ]
}

Rules for `new_memories`:
- Produce 3-5 entries. Fewer than 3 = a slow week recap; more than 5 will be truncated.
- Each entry covers a single specific moment from the matchup data in the user prompt — not a generic vibe.
- `manager_user_id` must be one of the user_ids listed in the lore block, or null for league-wide memories ("the league averaged X points this week").
- `sentiment` values:
    - "roast": the memory makes fun of a manager's outcome.
    - "praise": the memory celebrates a great performance.
    - "lore": neutral observation that's worth remembering across weeks (a record, a streak, an inside joke earning new life).

Worked example (do not copy verbatim — yours must reflect THIS week's actual data):
{
  "body_markdown": "# Week 4 Roast\\n\\n## Headline\\n**Manager3** put up 142.8 — the league high — while **Manager7** managed 81.2 against him. Brutal.\\n\\n## Matchup recaps\\n...",
  "new_memories": [
    {
      "text": "Manager3 dropped 142.8 in Week 4 — the league high and his second 140+ game of the season already.",
      "manager_user_id": "865328062403870720",
      "sentiment": "praise"
    },
    {
      "text": "Manager7 lost by 61.6 in Week 4, leaving 38.4 on the bench. Worst lineup-setting performance of the season so far.",
      "manager_user_id": "1127420529155227648",
      "sentiment": "roast"
    },
    {
      "text": "League average dropped to 102.4 in Week 4 — the lowest mid-season figure in two years.",
      "manager_user_id": null,
      "sentiment": "lore"
    }
  ]
}"""


# Re-exported so callers + tests that want to assert against the lore
# content can pull it from this module alongside `SYSTEM_PROMPT_TONE`.
SYSTEM_PROMPT_LORE = _LORE_TEXT


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def build_system_blocks() -> list[dict[str, Any]]:
    """Return the three-block system prompt payload Anthropic expects.

    Each block is tagged with `cache_control: ephemeral` so that the
    tone+safety scaffolding, league lore, and JSON envelope contract
    are cached across subsequent calls (dry-run + force re-runs land
    warm; weekly fires across the regular season hit the cached
    blocks too)."""
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT_TONE,
            "cache_control": {"type": "ephemeral"},
        },
        build_lore_block(),
        {
            "type": "text",
            "text": SYSTEM_PROMPT_OUTPUT_SPEC,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_user_prompt(
    *,
    league_name: str,
    season: str,
    week: int,
    matchups: list[dict[str, Any]],
    prior_memories: list[dict[str, Any]] | None,
    nfl_news: str | None = None,
) -> str:
    """Build the per-call user prompt for the weekly recap.

    Args:
        league_name: Sleeper league name.
        season: Season year as a string.
        week: NFL week being recapped (1-22).
        matchups: One dict per matchup, sorted by descending margin.
            Each carries:
              - `matchup_id` (int)
              - `winner`: {manager_display_name, team_name, user_id,
                points, bench_points}
              - `loser`: same shape as winner
              - `margin`: float (winner_points - loser_points)
              - `is_tie`: bool
        prior_memories: Last N memory rows from the store, newest
            first. May be empty (Week 1 first-run case). Each row
            carries `week`, `text`, `sentiment`, `manager_user_id`.

    Returns:
        The user message string for Anthropic's Messages API.
    """
    lines: list[str] = []
    lines.append(
        f"This is Week {week} of the {season} season for the league "
        f"\"{league_name}\". The matchups below already finished — "
        "the data is ground truth. Roast the outcomes."
    )
    lines.append("")

    # --- matchup results -------------------------------------------------------
    lines.append(f"## Week {week} matchup results")
    lines.append("")
    if not matchups:
        lines.append(
            "_No matchup data available for this week. Skip the "
            "per-matchup recap and write a league-wide note._"
        )
    else:
        # League-wide top + bottom of the week, computed across both
        # sides of each matchup for the prompt header.
        all_scores: list[tuple[float, str, str]] = []
        for m in matchups:
            for side_key in ("winner", "loser"):
                side = m.get(side_key) or {}
                team = (
                    side.get("team_name")
                    or side.get("manager_display_name")
                    or "Unknown"
                )
                all_scores.append(
                    (float(side.get("points") or 0.0), team, side_key)
                )
        if all_scores:
            top = max(all_scores, key=lambda t: t[0])
            bot = min(all_scores, key=lambda t: t[0])
            lines.append(
                f"- League high: **{top[1]}** at {top[0]:.2f} pts"
            )
            lines.append(
                f"- League low:  **{bot[1]}** at {bot[0]:.2f} pts"
            )
            lines.append("")

        for m in matchups:
            mid = m.get("matchup_id")
            winner = m.get("winner") or {}
            loser = m.get("loser") or {}
            margin = float(m.get("margin") or 0.0)
            is_tie = bool(m.get("is_tie"))

            w_name = winner.get("manager_display_name") or "Unknown"
            w_team = winner.get("team_name") or w_name
            w_pts = float(winner.get("points") or 0.0)
            w_bench = float(winner.get("bench_points") or 0.0)

            l_name = loser.get("manager_display_name") or "Unknown"
            l_team = loser.get("team_name") or l_name
            l_pts = float(loser.get("points") or 0.0)
            l_bench = float(loser.get("bench_points") or 0.0)

            header = (
                f"### Matchup #{mid}: {w_name} ({w_team}, user_id "
                f"{winner.get('user_id')}) {w_pts:.2f} — {l_pts:.2f} "
                f"{l_name} ({l_team}, user_id {loser.get('user_id')})"
            )
            lines.append(header)
            if is_tie:
                lines.append(f"- Result: **TIE** (both at {w_pts:.2f})")
            else:
                lines.append(
                    f"- Result: {w_name} wins by **{margin:.2f}**"
                )
            lines.append(
                f"- Winner bench points left on bench: {w_bench:.2f}"
            )
            lines.append(
                f"- Loser bench points left on bench: {l_bench:.2f}"
            )
            lines.append("")

    # --- NFL news (optional) ---------------------------------------------------
    if nfl_news:
        lines.append(nfl_news)
        lines.append("")

    # --- prior memories --------------------------------------------------------
    lines.append("## Season memories so far (most recent first)")
    lines.append("")
    if not prior_memories:
        lines.append(
            "_No prior memories on file yet — this is either Week 1 "
            "or the start of a fresh season. Write fresh material; "
            "the next weeks will have context to draw on._"
        )
    else:
        for mem in prior_memories:
            wk = mem.get("week")
            txt = (mem.get("text") or "").strip()
            sentiment = mem.get("sentiment") or "lore"
            mgr = mem.get("manager_user_id") or "league-wide"
            if not txt:
                continue
            lines.append(
                f"- (W{int(wk):02d}, {sentiment}, {mgr}): {txt}"
            )
    lines.append("")

    # --- task footer ----------------------------------------------------------
    lines.append("## Your task")
    lines.append("")
    lines.append(
        f"Write the Week {week} recap as a markdown body and produce "
        f"3-5 `new_memories` summarizing the most memorable moments "
        f"from THIS week's data. Respond with ONLY the JSON envelope "
        f"specified in the system prompt — no preamble, no code "
        f"fences, no commentary outside the JSON. Reference prior "
        f"memories where they fit naturally. Lean on the lore. Don't "
        f"invent injuries, trades, or news that isn't in the data. "
        f"Match each manager to the lore by user_id. A user_id that "
        f"isn't in the lore is a manager the lore doesn't cover — call "
        f"them by team name and give them nobody else's lore. The news "
        f"section doesn't say who rosters a player, so don't tie a news "
        f"item to a manager. "
        f"Cap new_memories at {AI_REVIEW_WEEKLY_MAX_NEW_MEMORIES}."
    )

    return "\n".join(lines)
