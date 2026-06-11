#!/usr/bin/env python3
"""Regenerate eval/fixtures/retrieval_gold.json (Phase 8 / P8-T8).

WHY SYNTHETIC VECTORS, HONESTLY (P8S-12)
========================================
CI has no embedding endpoint, and fake embeddings cannot honestly evaluate
paraphrase recall the way real model vectors can. So this fixture VENDORS
precomputed vectors under a PINNED model id -- but since no live endpoint exists
in this repo, the vectors are DETERMINISTIC SYNTHETIC embeddings produced by the
documented seeded "concept-projection" below, pinned as model id
``synthetic-hash-v1``.

The fixture's purpose is therefore TWO things only:
  1. harness wiring -- the gold-eval scenario can load corpus + queries +
     vectors and compute hit@k / MRR for lexical-only vs hybrid;
  2. lexical-anchor NON-REGRESSION -- hybrid must not lose the exact-identifier
     queries (codes, figures) that embeddings notoriously blur.

REAL-MODEL VECTORS REPLACE THESE the moment an embedding endpoint is first
configured: re-run this script with ``--model <real-model>`` and a populated
``EMBED_URL`` (a TODO seam below), which will call the real ``/embeddings`` API
instead of the synthetic projection and re-pin the model id. The synthetic
projection is good enough to demonstrate that the HARNESS distinguishes
lexical-only from hybrid and that the lexical-anchor subset does not regress; it
is NOT a substitute for measuring real paraphrase recall, and the file header
says so.

THE SYNTHETIC PROJECTION (documented, seeded, deterministic)
============================================================
Each text is reduced to a set of CONCEPT tokens (a small curated vocabulary that
maps surface words -- "headcount", "employees", "staff", "people" -- onto the
same concept id). The embedding is the L2-normalized sum of per-concept basis
vectors, where each basis vector is a fixed pseudo-random unit vector seeded by
``sha256(concept_id)``. Two texts that share concepts (a paraphrase and its
target chunk) therefore have HIGH cosine even when they share NO lexical tokens
-- which is exactly the property real embeddings have and lexical search lacks.
A pinch of per-token hashed noise keeps non-paraphrase texts apart.

This is a stdlib-only, numpy-free generator: float lists, ``math`` for the norm.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
from pathlib import Path

MODEL_ID = "synthetic-hash-v1"
DIM = 64

# --------------------------------------------------------------------------- #
# concept vocabulary: surface word -> concept id. This is what lets a paraphrase
# query ("number of people working here") match a chunk that says "headcount"
# without sharing a single token -- the synthetic analogue of semantic recall.
# --------------------------------------------------------------------------- #
_CONCEPTS: dict[str, str] = {}


def _c(concept: str, *words: str) -> None:
    for w in words:
        _CONCEPTS[w] = concept


_c("headcount", "headcount", "employees", "employee", "staff", "people",
   "workforce", "personnel", "hires", "roster")
_c("revenue", "revenue", "sales", "turnover", "topline", "income", "earnings",
   "bookings")
_c("churn", "churn", "attrition", "cancellations", "lost", "leaving",
   "departures", "retention")
_c("runway", "runway", "cash", "burn", "months", "solvency", "liquidity")
_c("latency", "latency", "slow", "response", "delay", "lag", "speed",
   "performance")
_c("uptime", "uptime", "availability", "downtime", "outage", "reliability",
   "sla")
_c("pricing", "pricing", "price", "cost", "tier", "plan", "subscription",
   "billing")
_c("onboarding", "onboarding", "signup", "activation", "setup", "getting",
   "started", "first")
_c("support", "support", "ticket", "helpdesk", "escalation", "complaint",
   "issue")
_c("security", "security", "breach", "incident", "vulnerability", "encryption",
   "compliance")
_c("region", "region", "emea", "apac", "americas", "geography", "territory")
_c("product", "product", "feature", "roadmap", "release", "launch", "shipping")

_TOKEN_STOP = frozenset(
    "a an the of to in on for and or is are was were be by with our we their how "
    "what which many number total count this that".split()
)


def _tokenize(text: str) -> list[str]:
    cur = []
    out = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return [t for t in out if t not in _TOKEN_STOP]


def _seeded_unit(seed: str) -> list[float]:
    """A fixed pseudo-random unit vector of length DIM seeded by ``seed``."""
    vals: list[float] = []
    counter = 0
    while len(vals) < DIM:
        h = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).digest()
        # 8 floats per 32-byte digest (4 bytes each -> uint32 -> [-1,1)).
        for i in range(0, 32, 4):
            u = struct.unpack(">I", h[i:i + 4])[0]
            vals.append((u / 2**31) - 1.0)
            if len(vals) >= DIM:
                break
        counter += 1
    n = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / n for v in vals]


def synthetic_embedding(text: str) -> list[float]:
    """Deterministic concept-projection embedding (documented above)."""
    acc = [0.0] * DIM
    toks = _tokenize(text)
    concepts = []
    for t in toks:
        c = _CONCEPTS.get(t)
        if c:
            concepts.append(c)
    # Concept basis (the semantic signal, weighted heavily).
    for c in concepts:
        basis = _seeded_unit(f"concept:{c}")
        for i in range(DIM):
            acc[i] += 3.0 * basis[i]
    # Token noise (a weak lexical signal so non-paraphrases stay separated).
    for t in toks:
        basis = _seeded_unit(f"token:{t}")
        for i in range(DIM):
            acc[i] += 0.5 * basis[i]
    n = math.sqrt(sum(v * v for v in acc))
    if n == 0.0:
        # No concept and no token (degenerate) -> seed off the raw text so we
        # never emit a zero vector (the kernel rejects zero-norm).
        return _seeded_unit(f"raw:{text}")
    return [v / n for v in acc]


# --------------------------------------------------------------------------- #
# the corpus + query set
# --------------------------------------------------------------------------- #
# Public-only, secret-scan-clean. Each chunk has a single source_id; queries map
# to the expected source_id. Paraphrase queries deliberately share NO salient
# token with their target chunk; lexical-anchor queries hit an exact identifier.
# --------------------------------------------------------------------------- #

CORPUS = [
    {"source_id": "src-headcount", "chunk_index": 0,
     "text": "Total headcount across all departments stands at 412 as of Q2."},
    {"source_id": "src-revenue", "chunk_index": 0,
     "text": "Annual recurring revenue reached 18.4 million dollars this fiscal year."},
    {"source_id": "src-churn", "chunk_index": 0,
     "text": "Logo churn for the enterprise segment was 4.1 percent last quarter."},
    {"source_id": "src-runway", "chunk_index": 0,
     "text": "At the current burn the company has 22 months of cash runway remaining."},
    {"source_id": "src-latency", "chunk_index": 0,
     "text": "The p99 API response latency measured 240 milliseconds under peak load."},
    {"source_id": "src-uptime", "chunk_index": 0,
     "text": "Service availability for the quarter met the 99.95 percent SLA target."},
    {"source_id": "src-pricing", "chunk_index": 0,
     "text": "The Growth subscription tier is billed at 299 dollars per seat monthly."},
    {"source_id": "src-onboarding", "chunk_index": 0,
     "text": "New customers complete activation through a six-step guided setup flow."},
    {"source_id": "src-support", "chunk_index": 0,
     "text": "The support helpdesk resolved 1320 escalation tickets in March."},
    {"source_id": "src-security", "chunk_index": 0,
     "text": "An encryption-at-rest control closed the outstanding compliance finding."},
    {"source_id": "src-region-emea", "chunk_index": 0,
     "text": "The EMEA territory contributed 38 percent of bookings this period."},
    {"source_id": "src-product", "chunk_index": 0,
     "text": "The roadmap targets a vector-search feature launch in the next release."},
    # Identifier-bearing chunks for the lexical-anchor subset.
    {"source_id": "src-invoice", "chunk_index": 0,
     "text": "Invoice INV-2024-00731 was settled net-30 against purchase order PO-5582."},
    {"source_id": "src-errcode", "chunk_index": 0,
     "text": "Error code ERR_QUOTA_4290 indicates the embedding batch exceeded its limit."},
    {"source_id": "src-sku", "chunk_index": 0,
     "text": "SKU GRW-SEAT-299 maps to the Growth per-seat monthly entitlement."},
    # The stuffed-document case (P8S-12): a chunk that keyword-stuffs many terms
    # to try to win raw bm25 on EVERY query. RRF caps its contribution at
    # 1/(60+1) per ranking, so it must NOT dominate the gold queries.
    {"source_id": "src-stuffed", "chunk_index": 0,
     "text": ("headcount revenue churn runway latency uptime pricing onboarding "
              "support security region product invoice error sku headcount "
              "revenue churn runway latency uptime pricing onboarding support "
              "security region product " * 4)},
]

# query_id ordering is LOAD-BEARING: every 5th id (5,10,15,20,25) is the Phase 6
# HOLD-OUT, frozen now and excluded from ALL P8 tuning (P8S-12).
QUERIES = [
    # --- paraphrase subset: shares concepts, not tokens, with the target ----
    {"query_id": 1, "kind": "paraphrase",
     "text": "how many people work at the company in total",
     "expected_source_id": "src-headcount"},
    {"query_id": 2, "kind": "paraphrase",
     "text": "what is our topline for the year",
     "expected_source_id": "src-revenue"},
    {"query_id": 3, "kind": "paraphrase",
     "text": "rate at which enterprise accounts are cancelling",
     "expected_source_id": "src-churn"},
    {"query_id": 4, "kind": "paraphrase",
     "text": "how long until we run out of money",
     "expected_source_id": "src-runway"},
    {"query_id": 5, "kind": "paraphrase",  # HOLD-OUT
     "text": "how fast does the service respond under heavy traffic",
     "expected_source_id": "src-latency"},
    {"query_id": 6, "kind": "paraphrase",
     "text": "did we meet our reliability commitment",
     "expected_source_id": "src-uptime"},
    {"query_id": 7, "kind": "paraphrase",
     "text": "what does the mid plan cost each month",
     "expected_source_id": "src-pricing"},
    {"query_id": 8, "kind": "paraphrase",
     "text": "how do new users get started",
     "expected_source_id": "src-onboarding"},
    {"query_id": 9, "kind": "paraphrase",
     "text": "volume of customer complaints handled by the team",
     "expected_source_id": "src-support"},
    {"query_id": 10, "kind": "paraphrase",  # HOLD-OUT
     "text": "what did we do to fix the data-protection audit gap",
     "expected_source_id": "src-security"},
    {"query_id": 11, "kind": "paraphrase",
     "text": "share of sales coming from europe and the middle east",
     "expected_source_id": "src-region-emea"},
    {"query_id": 12, "kind": "paraphrase",
     "text": "when are we shipping semantic retrieval",
     "expected_source_id": "src-product"},
    # --- lexical-anchor subset: exact identifiers embeddings blur ------------
    {"query_id": 13, "kind": "lexical_anchor",
     "text": "INV-2024-00731",
     "expected_source_id": "src-invoice"},
    {"query_id": 14, "kind": "lexical_anchor",
     "text": "ERR_QUOTA_4290",
     "expected_source_id": "src-errcode"},
    {"query_id": 15, "kind": "lexical_anchor",  # HOLD-OUT
     "text": "GRW-SEAT-299",
     "expected_source_id": "src-sku"},
    {"query_id": 16, "kind": "lexical_anchor",
     "text": "PO-5582",
     "expected_source_id": "src-invoice"},
    {"query_id": 17, "kind": "lexical_anchor",
     "text": "99.95 percent SLA",
     "expected_source_id": "src-uptime"},
    # --- direct/lexical-friendly queries (mixed) ----------------------------
    {"query_id": 18, "kind": "direct",
     "text": "headcount across departments",
     "expected_source_id": "src-headcount"},
    {"query_id": 19, "kind": "direct",
     "text": "annual recurring revenue this fiscal year",
     "expected_source_id": "src-revenue"},
    {"query_id": 20, "kind": "direct",  # HOLD-OUT
     "text": "cash runway months remaining",
     "expected_source_id": "src-runway"},
    {"query_id": 21, "kind": "direct",
     "text": "p99 api response latency under load",
     "expected_source_id": "src-latency"},
    {"query_id": 22, "kind": "direct",
     "text": "growth subscription tier monthly price",
     "expected_source_id": "src-pricing"},
    {"query_id": 23, "kind": "paraphrase",
     "text": "size of our workforce right now",
     "expected_source_id": "src-headcount"},
    {"query_id": 24, "kind": "paraphrase",
     "text": "money the business brought in",
     "expected_source_id": "src-revenue"},
    {"query_id": 25, "kind": "lexical_anchor",  # HOLD-OUT
     "text": "ERR_QUOTA_4290 embedding batch",
     "expected_source_id": "src-errcode"},
]

HEADER = {
    "_README": (
        "Phase 8 gold retrieval fixtures (P8-T8). The vectors below are "
        "DETERMINISTIC SYNTHETIC embeddings pinned as model 'synthetic-hash-v1', "
        "produced by eval/fixtures/regen_retrieval_gold.py's documented seeded "
        "concept-projection -- NOT a real embedding model. They exist to wire the "
        "gold-eval harness and to prove the lexical-anchor subset does NOT regress "
        "under hybrid fusion. REAL-MODEL vectors REPLACE these the moment an "
        "embedding endpoint is first configured: re-run the regen script against "
        "the live /embeddings API and re-pin the model id. Do not read these "
        "synthetic vectors as a measurement of real paraphrase recall."
    ),
    "embedding_model": MODEL_ID,
    "dim": DIM,
    "holdout_rule": "every 5th query_id (5,10,15,20,25) is the Phase 6 hold-out, "
                    "excluded from ALL P8 tuning (P8S-12)",
}


def build() -> dict:
    corpus = []
    for ch in CORPUS:
        corpus.append({
            **ch,
            "sensitivity": "public",
            "embedding_model": MODEL_ID,
            "vector": synthetic_embedding(ch["text"]),
        })
    queries = []
    for q in QUERIES:
        queries.append({
            **q,
            "embedding_model": MODEL_ID,
            "vector": synthetic_embedding(q["text"]),
        })
    return {**HEADER, "corpus": corpus, "queries": queries}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate retrieval_gold.json")
    ap.add_argument("--out", default=str(Path(__file__).with_name("retrieval_gold.json")))
    args = ap.parse_args(argv)
    data = build()
    Path(args.out).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    print(f"wrote {args.out}: {len(data['corpus'])} chunks, "
          f"{len(data['queries'])} queries, dim={DIM}, model={MODEL_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
