"""Run the LLM adjudicator over the deferred band and re-score the result.

Only records the deterministic scorer left in review or possible-match are
eligible. For each one the top candidates from the same source and state are
offered, and the model either picks one or declines.

The output is a comparison against the baseline, because "the LLM got 82%" is
not a useful number on its own. What matters is whether automation went up
without the error rate going up with it.

Run:  python eval/run_adjudication.py            # uses ANTHROPIC_API_KEY
      python eval/run_adjudication.py --stub     # no network, for wiring checks
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from entity_match_pipeline.llm_adjudicator import (  # noqa: E402
    Candidate,
    ClaudeModel,
    Request,
    StubModel,
    scrub,
)
from entity_match_pipeline.normalization import build_name_variants  # noqa: E402
from run_eval import DEFERRED_BANDS, evaluate, render  # noqa: E402

TRUTH = ROOT / "fixtures" / "ground_truth.csv"
HISTORY = ROOT / "output" / "match_history.sqlite3"
CANDIDATES = ROOT / "fixtures" / "candidates.csv"


def _best_name_score(left: str, right: str) -> float:
    """Best similarity across the normalized variants of both names."""
    lv = build_name_variants(left) or [str(left)]
    rv = build_name_variants(right) or [str(right)]
    return max(fuzz.token_set_ratio(a, b) for a in lv for b in rv)


def load_deferred(history_path: Path) -> list[dict]:
    con = sqlite3.connect(history_path)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in DEFERRED_BANDS)
    rows = con.execute(
        f"select source_code, source_name, live_business_name, state, decision_band,"
        f" primary_origination_amount, primary_origination_date, candidate_application_id"
        f" from loan_matches where decision_band in ({placeholders})",
        tuple(DEFERRED_BANDS),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def build_request(row: dict, candidates: pd.DataFrame, top_n: int = 5) -> Request:
    """Offer the plausible candidates from the same source and state."""
    pool = candidates[
        (candidates["candidate_source"] == row["source_code"])
        & (candidates["business_state"] == row["state"])
    ].copy()
    if pool.empty:
        pool = candidates[candidates["candidate_source"] == row["source_code"]].copy()

    pool["_score"] = pool["crm_business_name"].apply(
        lambda name: _best_name_score(row["live_business_name"], name)
    )
    pool = pool.sort_values("_score", ascending=False).head(top_n)

    return Request(
        live_business_name=str(row["live_business_name"]),
        state=str(row["state"]),
        source_name=str(row.get("source_name") or row["source_code"]),
        amount=float(row["primary_origination_amount"]) if row.get("primary_origination_amount") else None,
        origination_date=str(row.get("primary_origination_date") or "")[:10],
        candidates=tuple(
            Candidate(
                application_id=int(c["application_id"]),
                business_name=str(c["crm_business_name"]),
                state=str(c["business_state"]),
                postal_code=str(c["business_zip_code"]),
                size_band=str(c["loan_range"]),
                lead_date=str(c["lead_start_date"])[:10],
                name_score=float(c["_score"]),
            )
            for _, c in pool.iterrows()
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stub", action="store_true", help="run without network")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "adjudicated.json")
    parser.add_argument("--md-out", type=Path, default=ROOT / "eval" / "adjudicated.md")
    args = parser.parse_args()

    if not HISTORY.exists():
        print("No match history. Run the pipeline first.")
        return 1

    deferred = load_deferred(HISTORY)
    print(f"  {len(deferred)} deferred records eligible for adjudication")
    if not deferred:
        return 0

    candidates = pd.read_csv(CANDIDATES)
    model = StubModel(always="no_match") if args.stub else ClaudeModel(**({"model": args.model} if args.model else {}))
    if not args.stub:
        print(f"  model: {model.model}")

    overlay: dict[tuple[str, str, str], dict] = {}
    records: list[dict] = []
    totals = {"in": 0, "out": 0, "ms": 0, "errors": 0, "refusals": 0, "resolved": 0}

    for row in deferred[: args.max_rows]:
        request = build_request(row, candidates)
        decision = model.adjudicate(request)
        key = (str(row["source_code"]), str(row["live_business_name"]), str(row["state"]))
        overlay[key] = {"resolved": decision.resolved, "application_id": decision.application_id}

        totals["in"] += decision.input_tokens
        totals["out"] += decision.output_tokens
        totals["ms"] += decision.latency_ms
        totals["errors"] += 1 if decision.error else 0
        totals["refusals"] += 1 if (not decision.resolved and not decision.error) else 0
        totals["resolved"] += 1 if decision.resolved else 0

        records.append({
            "source_code": row["source_code"],
            "live_business_name": row["live_business_name"],
            "state": row["state"],
            "offered": [c.application_id for c in request.candidates],
            "resolved": decision.resolved,
            "application_id": decision.application_id,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "refused_reason": decision.refused_reason,
            "error": scrub(decision.error) if decision.error else "",
            "latency_ms": decision.latency_ms,
        })
        flag = "resolved" if decision.resolved else ("ERROR" if decision.error else "declined")
        print(f"    {str(row['live_business_name'])[:38]:<38} {flag:<9} {decision.confidence}")

    baseline = evaluate(TRUTH, HISTORY)
    adjudicated = evaluate(TRUTH, HISTORY, overlay=overlay)

    n = max(len(records), 1)
    print()
    print(render(baseline, "Deterministic baseline (v1)"))
    print()
    print(render(adjudicated, "With LLM adjudication (v2)"))
    print()
    print("### Cost and latency")
    print()
    print(f"- adjudications: **{len(records)}**  (resolved {totals['resolved']}, declined {totals['refusals']}, errors {totals['errors']})")
    print(f"- tokens: {totals['in']:,} in / {totals['out']:,} out")
    print(f"- latency: {totals['ms'] / n:,.0f} ms average")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "baseline": {"metrics": baseline.metrics(), "counts": baseline.counts()},
        "adjudicated": {"metrics": adjudicated.metrics(), "counts": adjudicated.counts()},
        "usage": totals,
        "decisions": records,
    }, indent=2), encoding="utf-8")

    md = "\n\n".join([
        render(baseline, "Deterministic baseline (v1)"),
        render(adjudicated, "With LLM adjudication (v2)"),
        "## Cost and latency\n\n"
        f"- adjudications: **{len(records)}** (resolved {totals['resolved']}, declined {totals['refusals']}, errors {totals['errors']})\n"
        f"- tokens: {totals['in']:,} in / {totals['out']:,} out\n"
        f"- latency: {totals['ms'] / n:,.0f} ms average",
    ])
    args.md_out.write_text(md + "\n", encoding="utf-8")
    print(f"\n  wrote {args.out.relative_to(ROOT)} and {args.md_out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
