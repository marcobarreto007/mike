"""Unit tests for individual _THINK_PATTERNS in mike_completions.py.

Each test validates that one compiled pattern correctly strips its targeted
reasoning-leak text while leaving legitimate content intact.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.chat.mike_completions import _THINK_PATTERNS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_patterns(text: str, skip: frozenset[int] = frozenset()) -> str:
    """Apply all think patterns except those whose indices are in *skip*."""
    for i, pat in enumerate(_THINK_PATTERNS):
        if i in skip:
            continue
        text = pat.sub("", text)
    return text


def _strip_all(text: str) -> str:
    """Apply all 18 think patterns."""
    return _apply_patterns(text)


# ---------------------------------------------------------------------------
# Pattern 0: <think>...</think> XML tags
# ---------------------------------------------------------------------------

def test_think_xml_tag_basic():
    """Pattern 0 strips <think>...</think> blocks."""
    result = _apply_patterns(
        "<think>internal reasoning</think>Final answer",
        skip=frozenset(range(1, 18)),
    )
    assert "<think>" not in result
    assert "</think>" not in result
    assert "Final answer" in result


def test_think_xml_tag_multiline():
    """Pattern 0 strips multiline think blocks."""
    result = _apply_patterns(
        "<think>\nStep 1: analyze\nStep 2: verify\n</think>\nOutput here",
        skip=frozenset(range(1, 18)),
    )
    assert "<think>" not in result
    assert "Output here" in result


def test_think_xml_tag_empty():
    """Pattern 0 strips empty think blocks without error."""
    result = _apply_patterns(
        "<think></think>Content",
        skip=frozenset(range(1, 18)),
    )
    assert result.strip() == "Content"


# ---------------------------------------------------------------------------
# Pattern 1: <||channel>...<channel||> tags
# ---------------------------------------------------------------------------

def test_channel_tag_basic():
    """Pattern 1 strips channel tags."""
    result = _apply_patterns(
        "<||channel>system prompt here<channel||>User query",
        skip=frozenset({0, *range(2, 18)}),
    )
    assert "<||channel>" not in result
    assert "<channel||>" not in result
    assert "User query" in result


# ---------------------------------------------------------------------------
# Pattern 2: Analyze User Input
# ---------------------------------------------------------------------------

def test_analyze_user_input():
    """Pattern 2 strips 'Analyze User Input:' reasoning block."""
    result = _apply_patterns(
        "Analyze User Input: the user wants a cake recipe\n\nHere is the recipe.",
        skip=frozenset({*range(0, 2), *range(3, 18)}),
    )
    assert "Analyze User Input" not in result
    assert "Here is the recipe" in result


def test_analyze_user_input_numbered():
    """Pattern 2 strips numbered '1. Analyze User Input:'."""
    result = _apply_patterns(
        "1. Analyze User Input: check for mentions\n\nResponse.",
        skip=frozenset({*range(0, 2), *range(3, 18)}),
    )
    assert "Analyze User Input" not in result
    assert "Response" in result


# ---------------------------------------------------------------------------
# Pattern 3: Identify Key Requirements
# ---------------------------------------------------------------------------

def test_identify_key_requirements():
    """Pattern 3 strips 'Identify Key Requirements:' blocks."""
    result = _apply_patterns(
        "**1. Identify Key Requirements: accuracy, brevity\n\nAnswer here.",
        skip=frozenset({*range(0, 3), *range(4, 18)}),
    )
    assert "Identify Key Requirements" not in result
    assert "Answer here" in result


# ---------------------------------------------------------------------------
# Pattern 4: Determine Tool Usage / Response Strategy / Capabilities
# ---------------------------------------------------------------------------

def test_determine_tool_usage():
    """Pattern 4 strips 'Determine Tool Usage:' blocks."""
    result = _apply_patterns(
        "Determine Tool Usage: search, calculator\n\nResult: 42",
        skip=frozenset({*range(0, 4), *range(5, 18)}),
    )
    assert "Determine Tool Usage" not in result
    assert "Result: 42" in result


def test_determine_response_strategy():
    """Pattern 4 strips 'Determine Response Strategy:' blocks."""
    result = _apply_patterns(
        "Determine Response Strategy: be concise\n\nHello!",
        skip=frozenset({*range(0, 4), *range(5, 18)}),
    )
    assert "Determine Response Strategy" not in result
    assert "Hello" in result


def test_determine_capabilities():
    """Pattern 4 strips 'Determine Capabilities:' blocks."""
    result = _apply_patterns(
        "**2. Determine Capabilities: available\n\nOutput.",
        skip=frozenset({*range(0, 4), *range(5, 18)}),
    )
    assert "Determine Capabilities" not in result
    assert "Output" in result


# ---------------------------------------------------------------------------
# Pattern 5: Formulate Response
# ---------------------------------------------------------------------------

def test_formulate_response():
    """Pattern 5 strips 'Formulate Response' blocks."""
    result = _apply_patterns(
        "Formulate Response: use a friendly tone\n\nHi there!",
        skip=frozenset({*range(0, 5), *range(6, 18)}),
    )
    assert "Formulate Response" not in result
    assert "Hi there" in result


# ---------------------------------------------------------------------------
# Pattern 6: Check Constraints
# ---------------------------------------------------------------------------

def test_check_constraints():
    """Pattern 6 strips 'Check Constraints:' blocks."""
    result = _apply_patterns(
        "Check Constraints: max 200 words, no PII\n\nHere you go.",
        skip=frozenset({*range(0, 6), *range(7, 18)}),
    )
    assert "Check Constraints" not in result
    assert "Here you go" in result


# ---------------------------------------------------------------------------
# Pattern 7: Final Polish / Check / Output / Answer
# ---------------------------------------------------------------------------

def test_final_polish():
    """Pattern 7 strips 'Final Polish:' blocks."""
    result = _apply_patterns(
        "Final Polish: remove redundancy\n\nPolished text.",
        skip=frozenset({*range(0, 7), *range(8, 18)}),
    )
    assert "Final Polish" not in result
    assert "Polished text" in result


def test_final_check():
    """Pattern 7 strips 'Final Check:' blocks."""
    result = _apply_patterns(
        "Final Check: verify accuracy\n\nVerified answer.",
        skip=frozenset({*range(0, 7), *range(8, 18)}),
    )
    assert "Final Check" not in result
    assert "Verified answer" in result


def test_final_output():
    """Pattern 7 strips 'Final Output:' blocks."""
    result = _apply_patterns(
        "Final Output: formatted response\n\nOutput here.",
        skip=frozenset({*range(0, 7), *range(8, 18)}),
    )
    assert "Final Output" not in result
    assert "Output here" in result


def test_final_answer():
    """Pattern 7 strips 'Final Answer:' blocks."""
    result = _apply_patterns(
        "Final Answer: 42\n\n42.",
        skip=frozenset({*range(0, 7), *range(8, 18)}),
    )
    assert "Final Answer" not in result


# ---------------------------------------------------------------------------
# Pattern 8: "Here's a thinking process:"
# ---------------------------------------------------------------------------

def test_heres_a_thinking_process():
    """Pattern 8 strips 'Here's a thinking process:' blocks."""
    result = _apply_patterns(
        "Here's a thinking process: step 1, step 2\n\nReal response.",
        skip=frozenset({*range(0, 8), *range(9, 18)}),
    )
    assert "thinking process" not in result
    assert "Real response" in result


# ---------------------------------------------------------------------------
# Pattern 9: Planning Block
# ---------------------------------------------------------------------------

def test_planning_block():
    """Pattern 9 strips 'Planning Block:' reasoning."""
    result = _apply_patterns(
        "**1. Planning Block: outline structure\n\nStructured output.",
        skip=frozenset({*range(0, 9), *range(10, 18)}),
    )
    assert "Planning Block" not in result
    assert "Structured output" in result


# ---------------------------------------------------------------------------
# Pattern 10: Draft Response / Construction / Mental
# ---------------------------------------------------------------------------

def test_draft_response():
    """Pattern 10 strips 'Draft Response:' blocks."""
    result = _apply_patterns(
        "Draft Response: bullet points\n\n- Item 1\n- Item 2",
        skip=frozenset({*range(0, 10), *range(11, 18)}),
    )
    assert "Draft Response" not in result
    assert "- Item 1" in result


def test_draft_construction():
    """Pattern 10 strips 'Draft Construction:' blocks."""
    result = _apply_patterns(
        "Draft Construction: build from outline\n\nFinal text.",
        skip=frozenset({*range(0, 10), *range(11, 18)}),
    )
    assert "Draft Construction" not in result
    assert "Final text" in result


def test_draft_mental():
    """Pattern 10 strips 'Draft Mental:' blocks."""
    result = _apply_patterns(
        "Draft Mental: imagine the flow\n\nResponse.",
        skip=frozenset({*range(0, 10), *range(11, 18)}),
    )
    assert "Draft Mental" not in result
    assert "Response" in result


# ---------------------------------------------------------------------------
# Pattern 11: Mental Draft / Refinement
# ---------------------------------------------------------------------------

def test_mental_draft():
    """Pattern 11 strips 'Mental Draft:' blocks."""
    result = _apply_patterns(
        "Mental Draft: rough ideas\n\nPolished result.",
        skip=frozenset({*range(0, 11), *range(12, 18)}),
    )
    assert "Mental Draft" not in result
    assert "Polished result" in result


def test_mental_refinement():
    """Pattern 11 strips 'Mental Refinement:' blocks."""
    result = _apply_patterns(
        "**2. Mental Refinement: improve phrasing\n\nBetter text.",
        skip=frozenset({*range(0, 11), *range(12, 18)}),
    )
    assert "Mental Refinement" not in result
    assert "Better text" in result


# ---------------------------------------------------------------------------
# Pattern 12: Doomsday
# ---------------------------------------------------------------------------

def test_doomsday():
    """Pattern 12 strips 'Doomsday' reasoning leaks."""
    result = _apply_patterns(
        "Doomsday algorithm: adjust for leap year\n\nResult: Monday",
        skip=frozenset({*range(0, 12), *range(13, 18)}),
    )
    assert "Doomsday" not in result
    assert "Result: Monday" in result


# ---------------------------------------------------------------------------
# Pattern 13: Zeller
# ---------------------------------------------------------------------------

def test_zeller():
    """Pattern 13 strips 'Zeller' reasoning leaks."""
    result = _apply_patterns(
        "Zeller's congruence: h = (q + ...)\n\nDay: Tuesday",
        skip=frozenset({*range(0, 13), *range(14, 18)}),
    )
    assert "Zeller" not in result
    assert "Day: Tuesday" in result


# ---------------------------------------------------------------------------
# Pattern 14: Calculate Day of the Week
# ---------------------------------------------------------------------------

def test_calculate_day_of_week():
    """Pattern 14 strips 'Calculate Day of the Week' blocks."""
    result = _apply_patterns(
        "Calculate Day of the Week: using formula\n\nIt is Friday.",
        skip=frozenset({*range(0, 14), *range(15, 18)}),
    )
    assert "Calculate Day of the Week" not in result
    assert "It is Friday" in result


# ---------------------------------------------------------------------------
# Pattern 15: Let's calculate
# ---------------------------------------------------------------------------

def test_lets_calculate():
    """Pattern 15 strips 'Let's calculate' blocks."""
    result = _apply_patterns(
        "Let's calculate: 2 + 2 = 4\n\nThe answer is 4.",
        skip=frozenset({*range(0, 15), *range(16, 18)}),
    )
    assert "calculate" not in result.lower()
    assert "The answer is 4" in result


# ---------------------------------------------------------------------------
# Pattern 16: Let's verify
# ---------------------------------------------------------------------------

def test_lets_verify():
    """Pattern 16 strips 'Let's verify' blocks."""
    result = _apply_patterns(
        "Let's verify: 4 * 7 = 28\n\nConfirmed: 28.",
        skip=frozenset({*range(0, 16), *range(17, 18)}),
    )
    assert "verify" not in result.lower()
    assert "Confirmed: 28" in result


# ---------------------------------------------------------------------------
# Pattern 17: Self-Correction
# ---------------------------------------------------------------------------

def test_self_correction():
    """Pattern 17 strips 'Self-Correction' blocks."""
    result = _apply_patterns(
        "Self-Correction: I said Tuesday but it's Wednesday\n\nCorrect: Wednesday.",
        skip=frozenset({*range(0, 17)}),
    )
    assert "Self-Correction" not in result
    assert "Correct: Wednesday" in result


def test_selfcorrection_no_hyphen():
    """Pattern 17 strips 'SelfCorrection' without hyphen."""
    result = _apply_patterns(
        "SelfCorrection: previous was wrong\n\nFixed answer.",
        skip=frozenset({*range(0, 17)}),
    )
    assert "SelfCorrection" not in result
    assert "Fixed answer" in result


# ---------------------------------------------------------------------------
# Combined (all patterns) smoke tests
# ---------------------------------------------------------------------------

def test_all_patterns_combined():
    """All 18 patterns cooperate without double-stripping or crashing."""
    text = (
        "<think>First, I should understand the query.</think>\n"
        "1. Analyze User Input: the user wants directions.\n\n"
        "Identify Key Requirements: accuracy, brevity.\n\n"
        "Check Constraints: stay under 500 chars.\n\n"
        "Here is the final output: Go north for 3 blocks."
    )
    result = _strip_all(text)
    assert "<think>" not in result
    assert "Analyze User Input" not in result
    assert "Identify Key Requirements" not in result
    assert "Check Constraints" not in result
    assert "Go north for 3 blocks" in result


def test_clean_text_unchanged():
    """Clean output without reasoning markers passes through untouched."""
    clean_text = "The capital of France is Paris."
    result = _strip_all(clean_text)
    assert result.strip() == clean_text


def test_empty_input():
    """Empty input does not raise errors."""
    assert _strip_all("") == ""
    assert _strip_all("   ") == "   "


# ---------------------------------------------------------------------------
# Verify every pattern compiles independently
# ---------------------------------------------------------------------------

def test_all_patterns_compiled():
    """All 18 patterns are compiled re.Pattern objects."""
    assert len(_THINK_PATTERNS) == 18, f"Expected 18 patterns, got {len(_THINK_PATTERNS)}"
    for i, pat in enumerate(_THINK_PATTERNS):
        assert isinstance(pat, re.Pattern), f"Pattern {i} is not a compiled regex: {type(pat)}"


def test_all_patterns_have_dotall_ignorecase():
    """Every pattern uses re.DOTALL | re.IGNORECASE flags."""
    for i, pat in enumerate(_THINK_PATTERNS):
        flags = pat.flags
        assert flags & re.DOTALL, f"Pattern {i} missing re.DOTALL"
        assert flags & re.IGNORECASE, f"Pattern {i} missing re.IGNORECASE"
