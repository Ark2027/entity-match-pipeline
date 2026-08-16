"""Generate the demo fixtures.

Produces four source workbooks and one candidate CSV, all fictional, so the
pipeline can be run end to end without a database or any real data.

The interesting part is not the volume, it is the hard cases. Real reconciliation
work is decided by the awkward 5%, so the generated data deliberately contains
legal-suffix variation, DBA forms, transposed words, same-name businesses in
different states, near-collisions made entirely of stopwords, rows that should
be rejected outright, and duplicates that should collapse.

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


def _dates(rng: random.Random, n: int) -> list[date]:
    span = (QUARTER_END - QUARTER_START).days
    return [QUARTER_START + timedelta(days=rng.randint(0, span)) for _ in range(n)]


def build() -> None:
    rng = random.Random(SEED)
    WORKBOOKS.mkdir(parents=True, exist_ok=True)

    candidates: list[dict] = []
    next_id = 1000

    for source_name, source_code, slug, states in SOURCES:
        live_rows: list[dict] = []

        # --- ordinary matches: same business on both sides, lightly reworded ---
        for _ in range(14):
            base = f"{rng.choice(HEAD)} {rng.choice(TAIL)}"
            state = rng.choice(states)
            zip_code = f"{rng.randint(10000, 99999)}"
            amount = rng.choice([15000, 25000, 40000, 75000, 120000])
            live_rows.append({
                "Business Name": f"{base} LLC",
                "State": state,
                "Origination Date": rng.choice(_dates(rng, 1)),
                "$ disbursed": amount,
                "Zip Code": zip_code,
            })
            next_id += 1
            candidates.append(_candidate(next_id, source_code, source_name, base, state, zip_code, amount, rng))

        # --- hard case 1: legal suffix punctuation differs ---
        state = states[0]
        live_rows.append({"Business Name": "Harbor Point Marine, L.L.C.", "State": state,
                          "Origination Date": QUARTER_START + timedelta(days=10), "$ disbursed": 52000, "Zip Code": "48201"})
        next_id += 1
        candidates.append(_candidate(next_id, source_code, source_name, "Harbor Point Marine LLC", state, "48201", 52000, rng))

        # --- hard case 2: DBA form, only the trading name is in the CRM ---
        live_rows.append({"Business Name": "Kestrel Holdings LLC dba Kestrel Coffee Roasters", "State": state,
                          "Origination Date": QUARTER_START + timedelta(days=22), "$ disbursed": 31000, "Zip Code": "48202"})
        next_id += 1
        candidates.append(_candidate(next_id, source_code, source_name, "Kestrel Coffee Roasters", state, "48202", 31000, rng))

        # --- hard case 3: word order transposed ---
        live_rows.append({"Business Name": "Ironwood Custom Cabinetry", "State": state,
                          "Origination Date": QUARTER_START + timedelta(days=31), "$ disbursed": 68000, "Zip Code": "48203"})
        next_id += 1
        candidates.append(_candidate(next_id, source_code, source_name, "Custom Cabinetry Ironwood", state, "48203", 68000, rng))

        # --- hard case 4: identical name, different state. Only one should match. ---
        if len(states) > 1:
            live_rows.append({"Business Name": "Meridian Freight Services", "State": states[0],
                              "Origination Date": QUARTER_START + timedelta(days=44), "$ disbursed": 90000, "Zip Code": "48204"})
            next_id += 1
            candidates.append(_candidate(next_id, source_code, source_name, "Meridian Freight Services", states[0], "48204", 90000, rng))
            next_id += 1
            candidates.append(_candidate(next_id, source_code, source_name, "Meridian Freight Services", states[1], "77001", 90000, rng))

        # --- hard case 5: nothing in common but stopwords. Should NOT auto-accept. ---
        live_rows.append({"Business Name": "Capital Investment Group", "State": state,
                          "Origination Date": QUARTER_START + timedelta(days=52), "$ disbursed": 44000, "Zip Code": "48205"})
        next_id += 1
        candidates.append(_candidate(next_id, source_code, source_name, "Capital Ventures Group", state, "48205", 44000, rng))

        # --- rejected rows: blank name, missing state, identifier-only name ---
        live_rows.append({"Business Name": "", "State": state,
                          "Origination Date": QUARTER_START + timedelta(days=5), "$ disbursed": 10000, "Zip Code": ""})
        live_rows.append({"Business Name": "Verdant Tile Works", "State": "",
                          "Origination Date": QUARTER_START + timedelta(days=6), "$ disbursed": 12000, "Zip Code": ""})
        live_rows.append({"Business Name": "A100234", "State": state,
                          "Origination Date": QUARTER_START + timedelta(days=7), "$ disbursed": 8000, "Zip Code": ""})

        # --- duplicate rows that should collapse into one review item ---
        dup = {"Business Name": "Lantern Street Bakery LLC", "State": state,
               "Origination Date": QUARTER_START + timedelta(days=60), "$ disbursed": 27000, "Zip Code": "48206"}
        live_rows.append(dup)
        live_rows.append(dict(dup))
        next_id += 1
        candidates.append(_candidate(next_id, source_code, source_name, "Lantern Street Bakery", state, "48206", 27000, rng))

        # --- a row outside the quarter, should be filtered out entirely ---
        live_rows.append({"Business Name": "Copper Ridge Fitness", "State": state,
                          "Origination Date": QUARTER_START - timedelta(days=45), "$ disbursed": 22000, "Zip Code": "48207"})

        path = WORKBOOKS / f"{slug}-originations-2026Q2.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame(live_rows).to_excel(writer, sheet_name="Origination Detail", index=False)
            pd.DataFrame(columns=["Note"]).to_excel(writer, sheet_name="Cover", index=False)
        print(f"  wrote {path.relative_to(ROOT)}  ({len(live_rows)} rows)")

    # --- candidates with no counterpart in any workbook ---
    rng2 = random.Random(SEED + 1)
    for _ in range(20):
        next_id += 1
        source_name, source_code, _, states = rng2.choice(SOURCES)
        candidates.append(_candidate(
            next_id, source_code, source_name,
            f"{rng2.choice(HEAD)} {rng2.choice(TAIL)}", rng2.choice(states),
            f"{rng2.randint(10000, 99999)}", rng2.choice([18000, 33000, 61000]), rng2,
        ))

    csv_path = FIXTURES / "candidates.csv"
    pd.DataFrame(candidates).to_csv(csv_path, index=False)
    print(f"  wrote {csv_path.relative_to(ROOT)}  ({len(candidates)} candidates)")


def _candidate(app_id: int, source_code: str, source_name: str, business: str,
               state: str, zip_code: str, amount: int, rng: random.Random) -> dict:
    low = int(amount * 0.6)
    high = int(amount * 1.6)
    lead_start = QUARTER_START - timedelta(days=rng.randint(5, 90))
    return {
        "application_id": app_id,
        "business_id": app_id + 500000,
        "customer_id": app_id + 900000,
        "candidate_source": source_code,
        "candidate_source_name": source_name,
        "application_status": rng.choice(["OPEN", "APPROVED", "IN_REVIEW"]),
        "loan_range": f"${low:,} - ${high:,}",
        "application_created_date": lead_start.isoformat(),
        "lead_start_date": lead_start.isoformat(),
        "crm_business_name": business,
        "customer_first_name": rng.choice(FIRST),
        "customer_last_name": rng.choice(LAST),
        "business_state": state,
        "business_zip_code": zip_code,
        "prescreen_created_date": lead_start.isoformat(),
        "prescreen_first_name": rng.choice(FIRST),
        "prescreen_last_name": rng.choice(LAST),
        "prescreen_state": state,
        "prescreen_zip_code": zip_code,
        "prescreen_business_zip_code": zip_code,
    }


if __name__ == "__main__":
    print("Generating fixtures...")
    build()
    print("Done.")
