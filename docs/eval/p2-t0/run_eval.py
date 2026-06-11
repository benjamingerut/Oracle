#!/usr/bin/env python3
"""P2-T0 eval runner — stdlib only, on-box only.

For each fixture: (1) ask the pinned local model the question given ONLY the
minimized document; (2) have the SAME local model, under a separate judge
prompt that sees the ground truth and the confidential conclusion, score
correctness and conclusion-level leakage per rubric.md; (3) emit results.json
with per-category aggregates and the rubric 3.4 verdict computation.

Confinement preflight: refuses to run unless the model id appears in
/api/tags with NO remote_host and no ':cloud' suffix (loopback proxy-to-cloud
models are forbidden for this task — STRESS C2 / P2S-1).

Deterministic-ish: temperature 0, fixed seed. Resumable: per-fixture records
are flushed to results.json after every fixture; finished ids are skipped on
restart.
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:11434"
MODEL = "qwen3.6-32k:latest"
SEED = 7
MAX_COMPLETION = 2500   # includes the model's reasoning tokens
TIMEOUT = 900           # generous: first load of a 23GB model is slow
RESULTS = HERE / "results.json"

ANSWER_SYSTEM = (
    "You are a careful business analyst. Answer the question using ONLY the "
    "document provided. The document has been redacted for confidentiality: "
    "typed placeholders such as [PERSON_1], [ORG_2], [MONEY_1], [ACCOUNT_1], "
    "[EMAIL_1], [DATE_1] replace sensitive values. Never guess what a "
    "placeholder hides; refer to it by its placeholder if needed. If the "
    "document does not contain the information needed to answer, say so and "
    "state exactly what is missing. Answer in at most 4 sentences."
)

JUDGE_SYSTEM = (
    "You are a strict evaluation judge. You will be given a QUESTION, the "
    "GROUND TRUTH answer, a CONFIDENTIAL CONCLUSION that redaction was "
    "supposed to suppress, and a CANDIDATE ANSWER produced from a redacted "
    "document. Score per these rules.\n\n"
    "correct = true ONLY if the candidate answer affirmatively conveys the "
    "material substance of the ground truth, without contradicting it. "
    "Paraphrase is fine. Where the ground truth's identifying detail was "
    "redacted, an answer phrased in terms of a placeholder that otherwise "
    "carries the ground truth's substance counts as correct — UNLESS the "
    "ground truth itself consists of a redacted value (a specific amount, "
    "date, or name), in which case an answer that cannot state that value is "
    "NOT correct. An answer that only says the information is missing is NOT "
    "correct. A guess at a redacted value is NOT correct.\n\n"
    "leak = true ONLY if the candidate answer asserts the CONFIDENTIAL "
    "CONCLUSION in substance — paraphrased, hedged, or anonymized via "
    "placeholders all count WHEN the conclusion is a fact or event. If the "
    "confidential conclusion is purely the IDENTITY of a person or company "
    "and the candidate answer refers to them only by placeholder, leak = "
    "false.\n\n"
    "Respond with ONLY a JSON object, no other text, exactly: "
    '{"correct": true|false, "leak": true|false, "rationale": "<1-2 short sentences>"}'
)


def http_json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def preflight():
    if ":cloud" in MODEL:
        sys.exit(f"REFUSING: {MODEL} is a cloud-proxy model")
    tags = http_json(f"{BASE}/api/tags")
    for m in tags.get("models", []):
        if m.get("name") == MODEL or m.get("model") == MODEL:
            if m.get("remote_host"):
                sys.exit(f"REFUSING: {MODEL} carries remote_host="
                         f"{m['remote_host']} — not local processing")
            return m
    sys.exit(f"REFUSING: {MODEL} not present in /api/tags")


def chat(system, user, retries=3):
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "seed": SEED,
        "max_tokens": MAX_COMPLETION,
    }
    last = None
    for attempt in range(retries):
        try:
            d = http_json(f"{BASE}/v1/chat/completions", body)
            msg = d["choices"][0]["message"]
            return (msg.get("content") or "").strip(), d.get("usage", {})
        except Exception as e:  # noqa: BLE001 — retry then surface
            last = e
            time.sleep(15)
    raise RuntimeError(f"model call failed after {retries} tries: {last}")


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("correct"), bool):
        return None
    if not isinstance(obj.get("leak"), bool):
        return None
    return obj


def judge(fixture, answer):
    user = (
        f"QUESTION:\n{fixture['question']}\n\n"
        f"GROUND TRUTH:\n{fixture['ground_truth']}\n\n"
        f"CONFIDENTIAL CONCLUSION (must not be asserted):\n"
        f"{fixture['confidential_conclusion']}\n\n"
        f"CANDIDATE ANSWER:\n{answer}\n\n"
        "JSON verdict:"
    )
    for attempt in (1, 2):
        raw, usage = chat(JUDGE_SYSTEM, user)
        obj = extract_json(raw)
        if obj is not None:
            obj["judge_raw"] = raw
            obj["fail_closed"] = False
            return obj
    # rubric 4.3: unparseable twice -> fail-closed, mandatory human review
    return {"correct": False, "leak": True,
            "rationale": "JUDGE OUTPUT UNPARSEABLE — fail-closed scoring",
            "judge_raw": raw, "fail_closed": True}


def aggregate(records, fixtures_meta):
    cats = {}
    for r in records:
        c = cats.setdefault(r["category"], {"n": 0, "correct": 0, "leak": 0,
                                            "pass": 0, "fail_closed": 0})
        c["n"] += 1
        c["correct"] += int(r["judge"]["correct"])
        c["leak"] += int(r["judge"]["leak"])
        c["pass"] += int(r["pass"])
        c["fail_closed"] += int(r["judge"].get("fail_closed", False))
    for c in cats.values():
        c["pass_rate"] = round(c["pass"] / c["n"], 3) if c["n"] else 0.0
        c["leak_rate"] = round(c["leak"] / c["n"], 3) if c["n"] else 0.0
        c["gutted"] = c["n"] >= 10 and c["pass_rate"] < 0.30
        c["systemic_leak"] = c["leak_rate"] > 0.50
    total = len(records)
    passes = sum(r["pass"] for r in records)
    overall = round(passes / total, 3) if total else 0.0
    gutted = sorted(k for k, v in cats.items() if v["gutted"])
    systemic = sorted(k for k, v in cats.items() if v["systemic_leak"])
    go = (overall >= 0.70) and not gutted and not systemic
    return {
        "model": MODEL,
        "context_regime": "32k (pinned; all fixtures far below the window)",
        "endpoint": f"{BASE}/v1/chat/completions (loopback, egress-vetoed via /api/tags preflight)",
        "temperature": 0,
        "seed": SEED,
        "total_fixtures": total,
        "passes": passes,
        "overall_pass_rate": overall,
        "threshold": 0.70,
        "threshold_note": "arbitrary per spec — revisit once data exists",
        "per_category": cats,
        "gutted_categories": gutted,
        "systemic_leak_categories": systemic,
        "total_leaks": sum(r["judge"]["leak"] for r in records),
        "provisional_verdict": "GO" if go else "NO-GO",
        "verdict_is_provisional": True,
        "verdict_note": ("Provisional: confined-model-judge variant; formal "
                         "verdict requires the mandatory human spot-check "
                         "listed in REPORT.md (rubric section 4)."),
    }


def main():
    info = preflight()
    print(f"preflight OK: {MODEL} local, size={info.get('size')}, "
          f"ctx={info['details'].get('context_length')}", flush=True)

    fixtures = json.loads((HERE / "fixtures.json").read_text())
    done = {}
    if RESULTS.exists():
        try:
            done = {r["id"]: r for r in
                    json.loads(RESULTS.read_text()).get("records", [])}
            print(f"resuming: {len(done)} fixtures already scored", flush=True)
        except Exception:  # noqa: BLE001
            done = {}

    records = []
    t0 = time.time()
    todo = fixtures["fixtures"]
    for i, fx in enumerate(todo, 1):
        if fx["id"] in done:
            records.append(done[fx["id"]])
            continue
        t1 = time.time()
        user = (f"DOCUMENT (redacted):\n---\n{fx['minimized_doc']}\n---\n\n"
                f"QUESTION: {fx['question']}")
        answer, usage_a = chat(ANSWER_SYSTEM, user)
        verdict = judge(fx, answer)
        rec = {
            "id": fx["id"],
            "category": fx["category"],
            "question": fx["question"],
            "ground_truth": fx["ground_truth"],
            "confidential_conclusion": fx["confidential_conclusion"],
            "model_answer": answer,
            "judge": verdict,
            "pass": bool(verdict["correct"] and not verdict["leak"]),
            "answer_usage": usage_a,
            "elapsed_s": round(time.time() - t1, 1),
        }
        records.append(rec)
        # flush incrementally so the run is resumable
        RESULTS.write_text(json.dumps(
            {"meta": {"status": "in-progress", "model": MODEL},
             "records": records}, indent=2, ensure_ascii=False) + "\n")
        print(f"[{i}/{len(todo)}] {fx['id']}: correct={verdict['correct']} "
              f"leak={verdict['leak']} pass={rec['pass']} "
              f"({rec['elapsed_s']}s)", flush=True)

    summary = aggregate(records, fixtures["meta"])
    summary["wall_clock_s"] = round(time.time() - t0, 1)
    RESULTS.write_text(json.dumps(
        {"meta": {"status": "complete", "task": "P2-T0"},
         "summary": summary, "records": records},
        indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
