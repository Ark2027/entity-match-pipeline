"""Tests for scoring, classification, quarter maths and output safety.

The cases that matter here are the ones where the matcher should *refuse* to
decide. Getting an obvious pair right is easy; declining an ambiguous one is
what makes the output trustworthy enough to act on.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from entity_match_pipeline.matching import (  # noqa: E402
    _classify_match,
    _has_meaningful_name_signal,
    _parse_loan_range,
    _supporting_adjustments,
    collapse_duplicate_live_rows,
)
from entity_match_pipeline.reporting import drop_sensitive_columns  # noqa: E402
from entity_match_pipeline.settings import (  # noqa: E402
    AppConfig,
    SourceMapping,
    ThresholdConfig,
    latest_due_quarter,
    previous_calendar_quarter,
    quarter_from_label,
    resolve_source_mapping,
)

try:
    from entity_match_pipeline.db_extract import _build_candidate_query, normalize_candidate_frame
    HAS_DB_EXTRAS = True
except ImportError:  # paramiko is an optional extra
    HAS_DB_EXTRAS = False


def _config(**overrides) -> AppConfig:
    return AppConfig(
        live_data_dir=Path("."),
        output_dir=Path("."),
        history_db_path=Path("./x.sqlite3"),
        candidates_csv=Path("./candidates.csv"),
        thresholds=ThresholdConfig(**overrides),
    )


class ClassificationTests(unittest.TestCase):
    """A pair must be strong *and* unambiguous to be accepted automatically."""

    def test_strong_and_clear_is_auto_accepted(self) -> None:
        self.assertEqual(_classify_match(98.0, 20.0, 98.0, _config()), "auto_accept")

    def test_strong_but_close_runner_up_falls_to_review(self) -> None:
        # Same score, but another candidate is within the gap. Refusing here is
        # the whole point: a confident wrong answer is worse than a queued one.
        self.assertEqual(_classify_match(98.0, 1.0, 98.0, _config()), "review")

    def test_high_overall_but_weak_name_falls_to_review(self) -> None:
        self.assertEqual(_classify_match(98.0, 20.0, 70.0, _config()), "review")

    def test_middling_scores_land_in_their_bands(self) -> None:
        self.assertEqual(_classify_match(75.0, 20.0, 75.0, _config()), "review")
        self.assertEqual(_classify_match(59.0, 20.0, 59.0, _config()), "possible_match")

    def test_weak_pairs_are_discarded(self) -> None:
        self.assertEqual(_classify_match(30.0, 20.0, 30.0, _config()), "discard")

    def test_thresholds_are_configurable(self) -> None:
        loose = _config(auto_accept_score=50.0, min_score_gap=0.0)
        self.assertEqual(_classify_match(60.0, 0.0, 60.0, loose), "auto_accept")


class NameSignalGuardTests(unittest.TestCase):
    """Two names can score well on raw similarity while sharing nothing real."""

    def test_shared_distinguishing_token_passes(self) -> None:
        self.assertTrue(_has_meaningful_name_signal("ironwood cabinetry", "cabinetry ironwood", 95.0))

    def test_stopwords_only_in_common_is_rejected(self) -> None:
        # "Capital Investment Group" vs "Capital Ventures Group": every shared
        # word is a stopword, so the real content is "investment" vs "ventures".
        self.assertFalse(_has_meaningful_name_signal("capital investment group", "capital ventures group", 78.0))

    def test_identical_cores_pass_despite_spacing(self) -> None:
        self.assertTrue(_has_meaningful_name_signal("harborpoint marine", "harbor point marine", 90.0))

    def test_substring_core_passes(self) -> None:
        self.assertTrue(_has_meaningful_name_signal("cedar bakery", "cedar bakery downtown", 88.0))


class LoanRangeTests(unittest.TestCase):
    def test_range_is_parsed(self) -> None:
        self.assertEqual(_parse_loan_range("$10,000 - $50,000"), (10000.0, 50000.0))

    def test_open_ended_and_junk_are_handled(self) -> None:
        low, high = _parse_loan_range("$100,000+")
        self.assertEqual(low, 100000.0)
        self.assertEqual(_parse_loan_range(None), (None, None))
        self.assertEqual(_parse_loan_range("not a range"), (None, None))


class SupportingSignalTests(unittest.TestCase):
    def _rows(self, live_zip="48201", cand_zip="48201", amount=50000, loan_range="$30,000 - $80,000"):
        live = pd.Series({
            "live_zip_codes": live_zip,
            "primary_origination_amount": amount,
            "primary_origination_date": pd.Timestamp("2026-05-01"),
        })
        cand = pd.Series({
            "business_zip_code": cand_zip,
            "prescreen_zip_code": "",
            "prescreen_business_zip_code": "",
            "loan_range": loan_range,
            "lead_start_date": pd.Timestamp("2026-03-01"),
        })
        return live, cand

    def test_matching_zip_adds_credit(self) -> None:
        adj, reasons = _supporting_adjustments(*self._rows())
        self.assertGreaterEqual(adj, 4.0)
        self.assertIn("zip matched", reasons)

    def test_mismatched_zip_earns_nothing(self) -> None:
        adj_match, _ = _supporting_adjustments(*self._rows())
        adj_miss, reasons = _supporting_adjustments(*self._rows(cand_zip="99999"))
        self.assertLess(adj_miss, adj_match)
        self.assertNotIn("zip matched", reasons)

    def test_amount_inside_range_adds_more_than_roughly_aligned(self) -> None:
        inside, _ = _supporting_adjustments(*self._rows(amount=50000))
        rough, _ = _supporting_adjustments(*self._rows(amount=110000))
        self.assertGreater(inside, rough)

    def test_origination_before_lead_is_flagged(self) -> None:
        live, cand = self._rows()
        cand["lead_start_date"] = pd.Timestamp("2026-08-01")
        _, reasons = _supporting_adjustments(live, cand)
        self.assertIn("origination slightly before lead date", reasons)


class DuplicateCollapseTests(unittest.TestCase):
    def test_identical_rows_collapse_into_one(self) -> None:
        rows = pd.DataFrame([
            {"source_file": "a.xlsx", "source_sheet": "S", "source_row_number": 2, "source_name": "N",
             "source_code": "NWP", "live_business_name": "Cedar Bakery LLC", "state": "OR",
             "origination_date": pd.Timestamp("2026-05-01"), "disbursed_amount": 1000.0, "live_zip_code": "97201"},
            {"source_file": "a.xlsx", "source_sheet": "S", "source_row_number": 3, "source_name": "N",
             "source_code": "NWP", "live_business_name": "Cedar Bakery LLC", "state": "OR",
             "origination_date": pd.Timestamp("2026-05-01"), "disbursed_amount": 1000.0, "live_zip_code": "97201"},
        ])
        collapsed, duplicates = collapse_duplicate_live_rows(rows)
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(int(collapsed.iloc[0]["duplicate_count"]), 2)
        self.assertEqual(len(duplicates), 1)

    def test_different_states_do_not_collapse(self) -> None:
        rows = pd.DataFrame([
            {"source_file": "a.xlsx", "source_sheet": "S", "source_row_number": 2, "source_name": "N",
             "source_code": "NWP", "live_business_name": "Cedar Bakery LLC", "state": "OR",
             "origination_date": pd.Timestamp("2026-05-01"), "disbursed_amount": 1000.0, "live_zip_code": ""},
            {"source_file": "a.xlsx", "source_sheet": "S", "source_row_number": 3, "source_name": "N",
             "source_code": "NWP", "live_business_name": "Cedar Bakery LLC", "state": "WA",
             "origination_date": pd.Timestamp("2026-05-01"), "disbursed_amount": 1000.0, "live_zip_code": ""},
        ])
        collapsed, _ = collapse_duplicate_live_rows(rows)
        self.assertEqual(len(collapsed), 2)


class QuarterTests(unittest.TestCase):
    def test_previous_quarter_wraps_the_year(self) -> None:
        self.assertEqual(previous_calendar_quarter(date(2026, 2, 14)).label, "2025Q4")
        self.assertEqual(previous_calendar_quarter(date(2026, 8, 15)).label, "2026Q2")

    def test_quarter_bounds(self) -> None:
        q = quarter_from_label("2026Q2")
        self.assertEqual((q.start, q.end), (date(2026, 4, 1), date(2026, 6, 30)))

    def test_bad_labels_are_rejected(self) -> None:
        for bad in ("2026", "26Q2", "2026Q5", "2026X2"):
            with self.assertRaises(ValueError, msg=bad):
                quarter_from_label(bad)

    def test_due_quarter_lags_the_calendar(self) -> None:
        # Reporting is only expected a month after a quarter closes.
        self.assertEqual(latest_due_quarter(date(2026, 8, 15)).label, "2026Q2")
        self.assertEqual(latest_due_quarter(date(2026, 4, 15)).label, "2025Q4")


class SourceMappingTests(unittest.TestCase):
    MAPPINGS = (
        SourceMapping("Northwest Partners", "NWP", ("northwest", "nwp")),
        SourceMapping("Great Lakes Collective", "GLC", ("great-lakes",)),
    )

    def test_marker_matches_case_insensitively(self) -> None:
        m = resolve_source_mapping("Q2-NORTHWEST-originations.xlsx", self.MAPPINGS)
        self.assertIsNotNone(m)
        self.assertEqual(m.source_code, "NWP")

    def test_unmapped_file_returns_none(self) -> None:
        self.assertIsNone(resolve_source_mapping("mystery-file.xlsx", self.MAPPINGS))


class OutputSafetyTests(unittest.TestCase):
    def test_personal_columns_are_removed_not_hidden(self) -> None:
        frame = pd.DataFrame([{
            "crm_business_name": "Cedar Bakery",
            "candidate_email": "someone@example.com",
            "candidate_phone": "555-0100",
            "owner_address_line1": "1 Main St",
            "match_score": 98,
        }])
        cleaned = drop_sensitive_columns(frame)
        self.assertIn("crm_business_name", cleaned.columns)
        self.assertIn("match_score", cleaned.columns)
        for column in ("candidate_email", "candidate_phone", "owner_address_line1"):
            self.assertNotIn(column, cleaned.columns)

    def test_clean_frame_is_untouched(self) -> None:
        frame = pd.DataFrame([{"crm_business_name": "Cedar Bakery", "match_score": 98}])
        self.assertEqual(list(drop_sensitive_columns(frame).columns), ["crm_business_name", "match_score"])


@unittest.skipUnless(HAS_DB_EXTRAS, "database extras not installed")
class QuerySafetyTests(unittest.TestCase):
    def test_query_selects_no_personal_columns(self) -> None:
        query = _build_candidate_query("public").lower()
        for banned in ("ssn", "email", "phone", "address_line", "date_of_birth"):
            self.assertNotIn(banned, query, msg=f"{banned} should not be selected")

    def test_normalization_uppercases_filter_columns(self) -> None:
        frame = normalize_candidate_frame(pd.DataFrame([{
            "candidate_source": " nwp ", "business_state": "or", "prescreen_state": None,
            "lead_start_date": "2026-03-01",
        }]))
        self.assertEqual(frame.iloc[0]["candidate_source"], "NWP")
        self.assertEqual(frame.iloc[0]["business_state"], "OR")
        self.assertEqual(frame.iloc[0]["prescreen_state"], "")


if __name__ == "__main__":
    unittest.main()
