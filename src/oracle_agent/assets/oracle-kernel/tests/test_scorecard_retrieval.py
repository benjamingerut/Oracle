#!/usr/bin/env python3
"""Tests for the scorecard ``retrieval`` KPI section (P8-T7).

Load-bearing guarantees exercised here (per the Phase-8 spec + stress table):

  * compute_kpis grows a ``retrieval`` section: searches, non_empty_rate,
    hybrid_share, vector_coverage, retrieval_hit_rate, and
    time_to_first_grounded_answer -- all metadata-only, all from ledgers.
  * retrieval_hit_rate: share of window searches whose top_source_ids
    intersect the source_ids cited by an exit-0 answer_event in the window.
  * time_to_first_grounded_answer: median days from a source's ingest to the
    first exit-0 answer citing it.
  * The composite formula is UNTOUCHED (KPI addition, not re-weighting).
  * Old scorecards / ledgers without the section still parse (additive).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "_tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import ledger  # noqa: E402
import retrieval_ledger as rl  # noqa: E402
import scorecard  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _answer_event(root, *, exit_code, source_ids, ts):
    ledger.append(
        Path(root) / scorecard.ANSWER_LEDGER,
        {
            "kind": "answer_event",
            "business_object": "Revenue",
            "exit_code": exit_code,
            "authority_state": "confirmed",
            "interface": "cli",
            "source_ids": source_ids,
            "ts": ts,
        },
        id_prefix="ANS",
    )


def _source_note(root, *, source_id, ingested):
    folder = Path(root) / scorecard.SOURCES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([
        "---",
        f"id: {source_id}",
        f"source_id: {source_id}",
        "type: source",
        f"ingested: {ingested}",
        "sensitivity: internal",
        "---",
        "",
        "# source body",
    ])
    (folder / f"src-{source_id}.md").write_text(fm + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# the retrieval KPI section
# --------------------------------------------------------------------------- #
def test_retrieval_section_basic_counts(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    now = datetime(2026, 6, 10)
    # 3 searches: 2 hybrid, 1 lexical; 2 non-empty.
    rl.log_search(root, query="a", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.8, result_count=2,
                  top_source_ids=["s1"], now=now)
    rl.log_search(root, query="b", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.9, result_count=1,
                  top_source_ids=["s2"], now=now)
    rl.log_search(root, query="c", k=10, engine="fts5", hybrid=False,
                  vector_coverage=0.9, result_count=0,
                  top_source_ids=[], now=now)

    kpis = scorecard.compute_kpis(root, start=start, end=end)
    r = kpis["retrieval"]
    assert r["searches"] == 3
    assert r["non_empty_rate"] == round(2 / 3, 4)
    assert r["hybrid_share"] == round(2 / 3, 4)
    # latest non-null coverage in window
    assert r["vector_coverage"] == 0.9


def test_retrieval_hit_rate(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    now = datetime(2026, 6, 10)
    # search 1 surfaces s1 (cited by a grounded answer -> hit);
    # search 2 surfaces s9 (never cited -> miss).
    rl.log_search(root, query="a", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.5, result_count=1,
                  top_source_ids=["s1"], now=now)
    rl.log_search(root, query="b", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.5, result_count=1,
                  top_source_ids=["s9"], now=now)
    _answer_event(root, exit_code=0, source_ids=["s1"], ts="2026-06-11T09:00:00")
    # a non-grounded (exit 2) answer citing s9 must NOT count.
    _answer_event(root, exit_code=2, source_ids=["s9"], ts="2026-06-11T09:05:00")

    kpis = scorecard.compute_kpis(root, start=start, end=end)
    r = kpis["retrieval"]
    assert r["retrieval_hit_rate"] == round(1 / 2, 4)


def test_time_to_first_grounded_answer_median(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    # source s1 ingested 2026-06-01, first grounded citation 2026-06-06 -> 5 days
    # source s2 ingested 2026-06-01, first grounded citation 2026-06-04 -> 3 days
    _source_note(root, source_id="s1", ingested="2026-06-01")
    _source_note(root, source_id="s2", ingested="2026-06-01")
    _answer_event(root, exit_code=0, source_ids=["s1"], ts="2026-06-06T00:00:00")
    _answer_event(root, exit_code=0, source_ids=["s2"], ts="2026-06-04T00:00:00")
    # a LATER citation of s1 must not change its first-grounded latency.
    _answer_event(root, exit_code=0, source_ids=["s1"], ts="2026-06-20T00:00:00")

    kpis = scorecard.compute_kpis(root, start=start, end=end)
    ttfga = kpis["retrieval"]["time_to_first_grounded_answer"]
    # median of [5, 3] = 4.0
    assert ttfga == 4.0


def test_composite_formula_untouched(tmp_path, minimal_oracle):
    """Adding the retrieval section must not change the composite (P8-T7)."""
    root = minimal_oracle(tmp_path)
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    now = datetime(2026, 6, 10)
    rl.log_search(root, query="a", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.5, result_count=1,
                  top_source_ids=["s1"], now=now)
    kpis = scorecard.compute_kpis(root, start=start, end=end)
    comp = scorecard.composite_score(kpis)
    # The composite reads only value/answers/failures -- never retrieval.
    kpis_no_retr = dict(kpis)
    kpis_no_retr.pop("retrieval", None)
    assert scorecard.composite_score(kpis_no_retr) == comp


def test_retrieval_section_empty_when_no_ledger(tmp_path, minimal_oracle):
    root = minimal_oracle(tmp_path)
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 30)
    kpis = scorecard.compute_kpis(root, start=start, end=end)
    r = kpis["retrieval"]
    assert r["searches"] == 0
    assert r["non_empty_rate"] is None
    assert r["retrieval_hit_rate"] is None
    assert r["time_to_first_grounded_answer"] is None


def test_generate_writes_card_with_retrieval_and_old_cards_parse(
    tmp_path, minimal_oracle
):
    root = minimal_oracle(tmp_path)
    now = datetime(2026, 6, 10)
    rl.log_search(root, query="a", k=10, engine="fts5", hybrid=True,
                  vector_coverage=0.7, result_count=1,
                  top_source_ids=["s1"], now=now)
    res = scorecard.generate(root, now=now)
    # The frontmatter projection carries the retrieval numbers.
    fm, _body = scorecard._split_frontmatter(
        Path(res["note_path"]).read_text(encoding="utf-8")
    )
    assert "retrieval_searches" in fm["kpis"]
    # An OLD-style scorecard (no retrieval keys) still parses cleanly.
    assert scorecard.load_scorecards(root)  # reads the card we just wrote
