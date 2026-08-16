"""Score the matcher's decisions against known-correct answers.

Reads the ground truth emitted by scripts/generate_fixtures.py, reads what the
pipeline actually decided from the match history, and reports whether those
decisions were right.

The distinction that matters is not correct versus incorrect. It is:

    resolved correctly   the pipeline decided, and was right
    resolved wrongly     the pipeline decided, and was wrong        <- costly
    deferred             the pipeline declined, a human will look   <- safe
    missed               nothing surfaced at all

A wrong automatic decision is far more expensive than a deferral, because it
enters downstream systems unchallenged. A deferral only costs someone a minute.
Any metric that averages those two together is hiding the thing you care about.

Run:  python eval/run_eval.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRUTH = ROOT / "fixtures" / "ground_truth.csv"
DEFAULT_HISTORY = ROOT / "output" / "match_history.sqlite3"

AUTOMATED_BANDS = {"auto_accept"}
DEFERRED_BANDS = {"review", "possible_match"}


@dataclass
class Outcome:
    key: tuple[str, str, str]
    case: str
    expected: int | None
    predicted: int | None
    band: str | None
    score: float | None
    gap: float | None

    @property
    def label(self) -> str:
        wanted_match = self.expected is not None
        if self.band is None:
            return "missed" if wanted_match else "correctly_abstained"
        automated = self.band in AUTOMATED_BANDS
        if not wanted_match:
            # A trap. Anything auto-accepted here is a false resolution.
            return "false_resolution" if automated else "deferred_trap"
        correct = self.predicted == self.expected
        if automated:
            return "resolved_correct" if correct else "resolved_wrong"
        return "deferred_correct" if correct else "deferred_wrong"


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.label] = out.get(o.label, 0) + 1
        return out

    def metrics(self) -> dict[str, float | int]:
        c = self.counts()
        g = c.get
        expect_match = sum(1 for o in self.outcomes if o.expected is not None)
        traps = sum(1 for o in self.outcomes if o.expected is None)
        automated = g("resolved_correct", 0) + g("resolved_wrong", 0) + g("false_resolution", 0)
        deferred = g("deferred_correct", 0) + g("deferred_wrong", 0) + g("deferred_trap", 0)
        return {
            "records": len(self.outcomes),
            "expecting_a_match": expect_match,
            "traps_expecting_none": traps,
            "automated_decisions": automated,
            "deferred_to_human": deferred,
            # Of everything decided automatically, how much was right.
            "automation_precision": _pct(g("resolved_correct", 0), automated),
            # Of everything that should have matched, how much was automated correctly.
            "automation_rate": _pct(g("resolved_correct", 0), expect_match),
            # The number to watch: automatic decisions that were wrong, either a
            # wrong candidate or a trap that should never have resolved.
            "error_rate": _pct(g("resolved_wrong", 0) + g("false_resolution", 0), max(automated, 1)),
            "trap_survival": _pct(g("deferred_trap", 0) + g("correctly_abstained", 0), max(traps, 1)),
            "human_queue": deferred,
        }

    def by_case(self) -> pd.DataFrame:
        rows = []
        for o in self.outcomes:
            rows.append({"case": o.case, "label": o.label})
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        return (
            frame.pivot_table(index="case", columns="label", aggfunc=len, fill_value=0)
            .astype(int)
            .sort_index()
        )


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def load_predictions(history_path: Path) -> dict[tuple[str, str, str], dict]:
    con = sqlite3.connect(history_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select source_code, live_business_name, state, candidate_application_id,"
        " decision_band, match_score, score_gap from loan_matches"
    ).fetchall()
    con.close()
    predictions: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (str(r["source_code"]), str(r["live_business_name"]), str(r["state"]))
        # Keep the strongest decision if a record somehow appears twice.
        existing = predictions.get(key)
        if existing is None or (r["match_score"] or 0) > (existing["match_score"] or 0):
            predictions[key] = dict(r)
    return predictions


def evaluate(truth_path: Path, history_path: Path) -> Report:
    truth = pd.read_csv(truth_path)
    predictions = load_predictions(history_path)
    report = Report()
    for _, t in truth.iterrows():
        key = (str(t["source_code"]), str(t["live_business_name"]), str(t["state"]))
        expected = t["expected_application_id"]
        expected_id = None if pd.isna(expected) or str(expected).strip() == "" else int(expected)
        p = predictions.get(key)
        report.outcomes.append(Outcome(
            key=key,
            case=str(t["case"]),
            expected=expected_id,
            predicted=int(p["candidate_application_id"]) if p and p["candidate_application_id"] else None,
            band=str(p["decision_band"]) if p else None,
            score=float(p["match_score"]) if p and p["match_score"] is not None else None,
            gap=float(p["score_gap"]) if p and p["score_gap"] is not None else None,
        ))
    return report


def render(report: Report, label: str) -> str:
    m = report.metrics()
    lines = [f"## {label}", ""]
    lines.append(f"- records evaluated: **{m['records']}** "
                 f"({m['expecting_a_match']} expecting a match, {m['traps_expecting_none']} expecting none)")
    lines.append(f"- automated: **{m['automated_decisions']}**, deferred to a human: **{m['deferred_to_human']}**")
    lines.append(f"- automation precision: **{m['automation_precision']}%** — of what it decided, how much was right")
    lines.append(f"- automation rate: **{m['automation_rate']}%** — of what should have matched, how much it handled")
    lines.append(f"- **error rate: {m['error_rate']}%** — automated decisions that were wrong")
    lines.append(f"- trap survival: **{m['trap_survival']}%** — records with no correct answer that were not resolved anyway")
    lines.append("")
    lines.append("### Outcomes")
    lines.append("")
    lines.append("| outcome | n |")
    lines.append("|---|---|")
    for k, v in sorted(report.counts().items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("")
    by_case = report.by_case()
    if not by_case.empty:
        lines.append("### By case type")
        lines.append("")
        lines.extend(_markdown_table(by_case))
        lines.append("")
    return "\n".join(lines)


def _markdown_table(frame: pd.DataFrame) -> list[str]:
    """Render a DataFrame as a markdown table without pulling in tabulate."""
    headers = ["case"] + [str(c) for c in frame.columns]
    rows = [[str(idx)] + [str(v) for v in frame.loc[idx]] for idx in frame.index]
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    out = [
        "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |",
        "|" + "|".join("-" * (w + 2) for w in widths) + "|",
    ]
    out.extend("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(r)) + " |" for r in rows)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--label", default="Deterministic baseline (v1)")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()

    if not args.history.exists():
        print(f"No match history at {args.history}.")
        print("Run the pipeline first:")
        print("  python run_matcher.py --config config/settings.demo.json --quarter 2026Q2 --rebuild-history")
        return 1

    report = evaluate(args.truth, args.history)
    text = render(report, args.label)
    print(text)

    if args.md_out:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(text + "\n", encoding="utf-8")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"label": args.label, "metrics": report.metrics(), "counts": report.counts()}
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
