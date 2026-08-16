"""Generate the demo fixtures and the ground truth that goes with them.

Produces source workbooks, a candidate CSV, and a ground truth file recording
which candidate each live record *should* match. All fictional, so the pipeline
runs end to end with no database and no real data.

The ground truth is what makes evaluation possible. Without it you can only
report how many rows landed in each band, which says nothing about whether the
answers were right.

Cases are tagged so results can be broken down by difficulty rather than
reported as a single accuracy number. "97% accurate" is meaningless if the
failures are all concentrated in the cases that actually matter.

Run:  python scripts/generate_fixtures.py
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

SEED = 20260815
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
WORKBOOKS = FIXTURES / "workbooks"
QUARTER_START = date(2026, 4, 1)
QUARTER_END = date(2026, 6, 30)

SOURCES = [
    ("Northwest Partners", "NWP", "northwest", ["OR", "WA"]),
    ("Great Lakes Collective", "GLC", "great-lakes", ["WI", "OH"]),
    ("Southwest Alliance", "SWA", "southwest", ["AZ", "NM"]),
    ("Atlantic Community Fund", "ACF", "atlantic", ["VA", "NC"]),
]

FIRST = ["Dana", "Miguel", "Priya", "Tomas", "Alice", "Joon", "Rosa", "Ellis", "Nadia", "Owen"]
LAST = ["Reyes", "Okafor", "Lindqvist", "Baptiste", "Moreau", "Kaur", "Novak", "Hale", "Ferrara", "Whitlock"]
HEAD = ["Cedar", "Ironwood", "Harbor", "Meridian", "Blue Ridge", "Kestrel", "Foundry", "Lantern", "Verdant", "Copper"]
TAIL = ["Bakery", "Logistics", "Dental", "Roofing", "Automotive", "Landscaping", "Print Shop", "Catering", "Fitness", "Upholstery"]


class Builder:
    """Accumulates live rows, candidates and ground truth together.

    Keeping them in one place is the point: a fixture whose correct answer is
    recorded somewhere else drifts out of sync the first time you edit it.
    """

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.candidates: list[dict] = []
        self.truth: list[dict] = []
        self.next_id = 1000

    def add_candidate(self, source_code: str, source_name: str, business: str,
                      state: str, zip_code: str, amount: int) -> int:
        self.next_id += 1
        low, high = int(amount * 0.6), int(amount * 1.6)
        lead_start = QUARTER_START - timedelta(days=self.rng.randint(5, 90))
        self.candidates.append({
            "application_id": self.next_id,
            "business_id": self.next_id + 500000,
            "customer_id": self.next_id + 900000,
            "candidate_source": source_code,
            "candidate_source_name": source_name,
            "application_status": self.rng.choice(["OPEN", "APPROVED", "IN_REVIEW"]),
            "loan_range": f"${low:,} - ${high:,}",
            "application_created_date": lead_start.isoformat(),
            "lead_start_date": lead_start.isoformat(),
            "crm_business_name": business,
            "customer_first_name": self.rng.choice(FIRST),
            "customer_last_name": self.rng.choice(LAST),
            "business_state": state,
            "business_zip_code": zip_code,
            "prescreen_created_date": lead_start.isoformat(),
            "prescreen_first_name": self.rng.choice(FIRST),
            "prescreen_last_name": self.rng.choice(LAST),
            "prescreen_state": state,
            "prescreen_zip_code": zip_code,
            "prescreen_business_zip_code": zip_code,
        })
        return self.next_id

    def expect(self, source_code: str, live_name: str, state: str,
               application_id: int | None, case: str) -> None:
        self.truth.append({
            "source_code": source_code,
            "live_business_name": live_name,
            "state": state,
            "expected_application_id": "" if application_id is None else application_id,
            "case": case,
        })


def build() -> None:
    b = Builder(SEED)
    WORKBOOKS.mkdir(parents=True, exist_ok=True)

    for source_name, code, slug, states in SOURCES:
        live: list[dict] = []
        st = states[0]
        alt = states[1] if len(states) > 1 else states[0]

        def row(name: str, state: str, day: int, amount: int, zip_code: str = "") -> None:
            live.append({
                "Business Name": name,
                "State": state,
                "Origination Date": QUARTER_START + timedelta(days=day),
                "$ disbursed": amount,
                "Zip Code": zip_code,
            })

        # ---------------------------------------------------------------
        # Clean matches. Should be resolved without help.
        # ---------------------------------------------------------------
        for i in range(12):
            base = f"{b.rng.choice(HEAD)} {b.rng.choice(TAIL)}"
            zip_code = f"{b.rng.randint(10000, 99999)}"
            amount = b.rng.choice([15000, 25000, 40000, 75000, 120000])
            row(f"{base} LLC", st, 3 + i, amount, zip_code)
            app = b.add_candidate(code, source_name, base, st, zip_code, amount)
            b.expect(code, f"{base} LLC", st, app, "clean")

        # ---------------------------------------------------------------
        # Solvable by normalization alone.
        # ---------------------------------------------------------------
        app = b.add_candidate(code, source_name, "Harbor Point Marine LLC", st, "48201", 52000)
        row("Harbor Point Marine, L.L.C.", st, 20, 52000, "48201")
        b.expect(code, "Harbor Point Marine, L.L.C.", st, app, "legal_suffix")

        app = b.add_candidate(code, source_name, "Kestrel Coffee Roasters", st, "48202", 31000)
        row("Kestrel Holdings LLC dba Kestrel Coffee Roasters", st, 22, 31000, "48202")
        b.expect(code, "Kestrel Holdings LLC dba Kestrel Coffee Roasters", st, app, "dba")

        app = b.add_candidate(code, source_name, "Custom Cabinetry Ironwood", st, "48203", 68000)
        row("Ironwood Custom Cabinetry", st, 31, 68000, "48203")
        b.expect(code, "Ironwood Custom Cabinetry", st, app, "transposed")

        # Same name in two states. Only the same-state one is correct.
        app = b.add_candidate(code, source_name, "Meridian Freight Services", st, "48204", 90000)
        b.add_candidate(code, source_name, "Meridian Freight Services", alt, "77001", 90000)
        row("Meridian Freight Services", st, 44, 90000, "48204")
        b.expect(code, "Meridian Freight Services", st, app, "cross_state")

        # ---------------------------------------------------------------
        # Genuinely ambiguous. These are the eval set that matters: a human
        # can resolve them from context, string distance alone cannot.
        # ---------------------------------------------------------------
        # Two plausible candidates, one is a different business entirely.
        app = b.add_candidate(code, source_name, "Cedar Ridge Bakery", st, "50101", 33000)
        b.add_candidate(code, source_name, "Cedar Ridge Bistro", st, "50101", 33000)
        row("Cedar Ridge Bakery & Cafe", st, 50, 33000, "50101")
        b.expect(code, "Cedar Ridge Bakery & Cafe", st, app, "ambiguous_sibling")

        # Abbreviated form vs spelled out, with a decoy sharing the prefix.
        app = b.add_candidate(code, source_name, "Northwest Ironwood Supply", st, "50102", 47000)
        b.add_candidate(code, source_name, "Northwest Iron Works", st, "50102", 47000)
        row("NW Ironwood Supply Co", st, 51, 47000, "50102")
        b.expect(code, "NW Ironwood Supply Co", st, app, "ambiguous_abbreviation")

        # Business renamed between systems; the decoy is the closer string.
        app = b.add_candidate(code, source_name, "Foundry Works Group", st, "50103", 59000)
        b.add_candidate(code, source_name, "Foundry Fitness", st, "50103", 59000)
        row("The Foundry Works", st, 52, 59000, "50103")
        b.expect(code, "The Foundry Works", st, app, "ambiguous_rename")

        # Misspelling in the source workbook.
        app = b.add_candidate(code, source_name, "Verdant Landscaping", st, "50104", 21000)
        b.add_candidate(code, source_name, "Verdant Landscape Design", st, "50104", 21000)
        row("Verdent Landscaping", st, 53, 21000, "50104")
        b.expect(code, "Verdent Landscaping", st, app, "ambiguous_misspelling")

        # Trap: the only shared words are stopwords. Correct answer is no match.
        b.add_candidate(code, source_name, "Capital Ventures Group", st, "50105", 44000)
        row("Capital Investment Group", st, 54, 44000, "50105")
        b.expect(code, "Capital Investment Group", st, None, "stopword_trap")

        # Trap: plausible-looking name with no counterpart at all.
        row("Blue Ridge Tannery", st, 55, 26000, "50106")
        b.expect(code, "Blue Ridge Tannery", st, None, "no_counterpart")

        # ---------------------------------------------------------------
        # Rows that should never reach matching.
        # ---------------------------------------------------------------
        row("", st, 5, 10000)
        row("Verdant Tile Works", "", 6, 12000)
        row("A100234", st, 7, 8000)

        # Duplicated row, should collapse to one review item.
        app = b.add_candidate(code, source_name, "Lantern Street Bakery", st, "48206", 27000)
        row("Lantern Street Bakery LLC", st, 60, 27000, "48206")
        row("Lantern Street Bakery LLC", st, 60, 27000, "48206")
        b.expect(code, "Lantern Street Bakery LLC", st, app, "duplicate")

        # Outside the quarter entirely.
        live.append({"Business Name": "Copper Ridge Fitness", "State": st,
                     "Origination Date": QUARTER_START - timedelta(days=45),
                     "$ disbursed": 22000, "Zip Code": "48207"})

        path = WORKBOOKS / f"{slug}-originations-2026Q2.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame(live).to_excel(writer, sheet_name="Origination Detail", index=False)
            pd.DataFrame(columns=["Note"]).to_excel(writer, sheet_name="Cover", index=False)
        print(f"  {path.relative_to(ROOT)}  ({len(live)} rows)")

    # Candidates with no counterpart in any workbook, so the CRM side has noise too.
    for _ in range(20):
        source_name, code, _, states = b.rng.choice(SOURCES)
        b.add_candidate(code, source_name, f"{b.rng.choice(HEAD)} {b.rng.choice(TAIL)}",
                        b.rng.choice(states), f"{b.rng.randint(10000, 99999)}",
                        b.rng.choice([18000, 33000, 61000]))

    pd.DataFrame(b.candidates).to_csv(FIXTURES / "candidates.csv", index=False)
    print(f"  fixtures/candidates.csv  ({len(b.candidates)} candidates)")

    truth = pd.DataFrame(b.truth)
    truth.to_csv(FIXTURES / "ground_truth.csv", index=False)
    print(f"  fixtures/ground_truth.csv  ({len(truth)} labelled records)")
    print("\n  cases:")
    for case, n in truth["case"].value_counts().sort_index().items():
        print(f"    {case:<26} {n}")


if __name__ == "__main__":
    print("Generating fixtures...")
    build()
    print("Done.")
