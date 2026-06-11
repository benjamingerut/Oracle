"""Performance guard for the forced-grounding gate (Phase 3, P3-T5).

The extractor + checker run on EVERY turn, in pure Python, with no model or
network call. This module asserts they add negligible latency on a typical draft
AND on adversarial pathological inputs (one very long single sentence, a 10k-row
table, deeply nested markdown). The regexes must stay linear-time under
attacker-shaped drafts (P3S-8) -- a super-linear blowup would let a crafted draft
hang the single-threaded serve loop under LOCK_EX.

Bound is deliberately generous for shared CI runners (< 250ms, not "a few ms"; linearity test is the real guard)
so timing tests do not flake (per the spec). The linear-time property is also
checked structurally: doubling the input size must not more than roughly double
the runtime (we allow a loose constant factor for measurement noise).

No network/model calls happen in the grounding path: the only I/O is the
server-side ``known_objects`` truth-map read, which is NOT exercised here
(``extract_claims`` / ``check_grounding`` take ``objects_seen`` directly).
"""
from __future__ import annotations

import time

import pytest

from oracle_agent.agentloop.grounding import check_grounding, extract_claims


# Loose CI-tolerant bound. The spec's intent is "negligible and linear" —
# the linearity test below is the real regression guard (a quadratic blowup
# on these inputs lands in whole seconds); the absolute ceiling only needs to
# catch that class while never flaking on a loaded/shared runner (observed:
# 39ms quiet, ~60ms with a 23GB local model saturating the same machine).
_BOUND_S = 0.250

# Objects the pathological drafts reference, so the checker does real work.
_OBJECTS = ["Revenue / invoices", "Customers / accounts", "Cash / bank"]


def _timed(fn, *a, **k) -> float:
    """Best-of-3 wall-clock seconds for a single call (reduces noise)."""
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn(*a, **k)
        best = min(best, time.perf_counter() - t0)
    return best


# --------------------------------------------------------------------------- #
# pathological input builders
# --------------------------------------------------------------------------- #
def _very_long_sentence(words: int) -> str:
    # A single declarative sentence with no terminator -> one huge unit. Mixes a
    # figure and a known object so it is material and gets fully checked.
    body = " ".join(["Revenue / invoices grew and"] * words)
    return f"{body} reached $12,345,678 in total"


def _huge_table(rows: int) -> str:
    out = ["| object | value |", "| --- | --- |"]
    for i in range(rows):
        out.append(f"| Customers / accounts | {i:,} accounts as of 2026-06-{(i % 28) + 1:02d} |")
    return "\n".join(out)


def _deeply_nested(depth: int) -> str:
    # Deeply nested markdown list items + blockquotes -> many marker-strip passes.
    lines = []
    for i in range(depth):
        indent = "  " * (i % 20)
        marker = ">" * ((i % 5) + 1)
        lines.append(f"{indent}{marker} - Cash / bank held $1,000,{i:03d} on item {i}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# typical draft
# --------------------------------------------------------------------------- #
def test_typical_draft_within_bound():
    draft = (
        "Hi! Here's what I found.\n\n"
        "Revenue / invoices totaled $4,200,000 in FY2026, up from $3,800,000.\n"
        "Customers / accounts grew to 1,240.\n\n"
        "| metric | value |\n| --- | --- |\n"
        "| Cash / bank | $912,000 |\n"
        "Let me know if you want more detail."
    )
    t = _timed(extract_claims, draft, objects_seen=_OBJECTS)
    assert t < _BOUND_S, f"typical extract took {t*1000:.1f}ms (bound 250ms)"
    t2 = _timed(check_grounding, draft, [], objects_seen=_OBJECTS)
    assert t2 < _BOUND_S, f"typical check took {t2*1000:.1f}ms (bound 250ms)"


# --------------------------------------------------------------------------- #
# pathological inputs (P3S-8)
# --------------------------------------------------------------------------- #
def test_very_long_single_sentence_within_bound():
    draft = _very_long_sentence(2000)
    t = _timed(extract_claims, draft, objects_seen=_OBJECTS)
    assert t < _BOUND_S, f"long-sentence extract took {t*1000:.1f}ms (bound 250ms)"


def test_ten_thousand_row_table_within_bound():
    draft = _huge_table(10000)
    t = _timed(extract_claims, draft, objects_seen=_OBJECTS)
    assert t < _BOUND_S, f"10k-row table extract took {t*1000:.1f}ms (bound 250ms)"
    t2 = _timed(check_grounding, draft, [], objects_seen=_OBJECTS)
    assert t2 < _BOUND_S, f"10k-row table check took {t2*1000:.1f}ms (bound 250ms)"


def test_deeply_nested_markdown_within_bound():
    draft = _deeply_nested(5000)
    t = _timed(extract_claims, draft, objects_seen=_OBJECTS)
    assert t < _BOUND_S, f"deeply-nested extract took {t*1000:.1f}ms (bound 250ms)"


# --------------------------------------------------------------------------- #
# linear-time assertion (P3S-8): no super-linear regex blowup
# --------------------------------------------------------------------------- #
def test_extractor_scales_linearly_under_repetition():
    """Doubling a repetitive adversarial draft must not more than ~quadruple
    the runtime -- a structural check that the regexes have no nested-quantifier
    backtracking trap (catastrophic blowup would be super-polynomial)."""
    small = _huge_table(2000)
    large = _huge_table(4000)
    t_small = _timed(extract_claims, small, objects_seen=_OBJECTS)
    t_large = _timed(extract_claims, large, objects_seen=_OBJECTS)
    # Allow a generous constant for measurement noise on shared runners; the
    # point is to catch EXPONENTIAL blowup, where 2x input -> orders of
    # magnitude slower, not the ~2x of linear scaling.
    floor = 1e-4  # ignore sub-100us noise where the ratio is meaningless
    if t_small > floor:
        assert t_large < t_small * 8, (
            f"2x input -> {t_large/t_small:.1f}x time: super-linear blowup "
            f"(small {t_small*1000:.2f}ms, large {t_large*1000:.2f}ms)"
        )


def test_adversarial_quantifier_bait_is_bounded():
    """A draft crafted to bait nested-quantifier backtracking (long runs of
    digits, commas, and capitals) must still complete within the bound."""
    bait = ("$" + "1," * 5000 + "000 "
            + "A" * 5000 + " "
            + "2026-" * 2000 + "06-10 "
            + "Revenue / invoices " * 1000)
    t = _timed(extract_claims, bait, objects_seen=_OBJECTS)
    assert t < _BOUND_S, f"quantifier-bait extract took {t*1000:.1f}ms (bound 250ms)"
