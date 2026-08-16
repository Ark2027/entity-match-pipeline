"""Tests for the LLM adjudicator's guardrails.

No network. Everything here exercises the layer between the model's answer and
the pipeline accepting it, which is where the safety actually lives — a model
that returns something unexpected should produce a refusal, never a silent
match.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from entity_match_pipeline.llm_adjudicator import (  # noqa: E402
    NO_MATCH_ID,
    Candidate,
    Request,
    StubModel,
    _from_payload,
    adjudicate_all,
    scrub,
)


def _request() -> Request:
    return Request(
        live_business_name="NW Ironwood Supply Co",
        state="OR",
        source_name="Northwest Partners",
        postal_code="50102",
        amount=47000,
        origination_date="2026-05-22",
        candidates=(
            Candidate(1020, "Northwest Ironwood Supply", "OR", "50102", "$28,200 - $75,200", "2026-02-01", 95.0),
            Candidate(1001, "Northwest Iron Works", "OR", "50102", "$28,200 - $75,200", "2026-02-01", 78.0),
        ),
    )


def _payload(**overrides) -> dict:
    base = {"matched_candidate_id": 1020, "confidence": "high", "reasoning": "shared distinctive term"}
    base.update(overrides)
    return base


class ScrubbingTests(unittest.TestCase):
    def test_api_key_is_removed(self) -> None:
        # Deliberately unmistakable so automated secret scanners do not flag it.
        out = scrub("401 unauthorized for sk-ant-NOT_A_REAL_KEY_FOR_TESTS_ONLY")
        self.assertIn("[redacted-api-key]", out)
        self.assertNotIn("NOT_A_REAL_KEY", out)

    def test_bearer_and_header_forms(self) -> None:
        self.assertIn("[redacted]", scrub("Authorization: Bearer abc.def.ghi123"))
        self.assertIn("[redacted]", scrub("x-api-key: sekret-value-here"))

    def test_output_is_capped(self) -> None:
        self.assertLessEqual(len(scrub("x" * 5000)), 600)


class PayloadGroundingTests(unittest.TestCase):
    def test_valid_high_confidence_resolves(self) -> None:
        d = _from_payload(_payload(), _request())
        self.assertTrue(d.resolved)
        self.assertEqual(d.application_id, 1020)

    def test_zero_means_no_match(self) -> None:
        d = _from_payload(_payload(matched_candidate_id=NO_MATCH_ID), _request())
        self.assertFalse(d.resolved)
        self.assertIn("no matching candidate", d.refused_reason)
        self.assertFalse(d.error, "declining is a valid answer, not an error")

    def test_id_that_was_never_offered_is_refused(self) -> None:
        # A hallucinated id must never be treated as a match.
        d = _from_payload(_payload(matched_candidate_id=9999), _request())
        self.assertFalse(d.resolved)
        self.assertIn("not among the candidates", d.refused_reason)

    def test_low_confidence_stays_deferred(self) -> None:
        d = _from_payload(_payload(confidence="low"), _request())
        self.assertFalse(d.resolved)
        self.assertEqual(d.application_id, 1020, "the choice is kept for review")
        self.assertIn("confidence too low", d.refused_reason)

    def test_medium_confidence_resolves(self) -> None:
        self.assertTrue(_from_payload(_payload(confidence="medium"), _request()).resolved)

    def test_missing_field_is_an_error_not_a_refusal(self) -> None:
        # The bug this exists to prevent: an absent key silently reading as
        # "no match", so every adjudication declines and the run looks fine.
        payload = _payload()
        del payload["matched_candidate_id"]
        d = _from_payload(payload, _request())
        self.assertFalse(d.resolved)
        self.assertTrue(d.error, "a malformed response must surface as an error")
        self.assertIn("omitted", d.error)

    def test_non_integer_id_is_an_error(self) -> None:
        d = _from_payload(_payload(matched_candidate_id="not-a-number"), _request())
        self.assertFalse(d.resolved)
        self.assertTrue(d.error)

    def test_unknown_confidence_degrades_to_low(self) -> None:
        d = _from_payload(_payload(confidence="extremely sure"), _request())
        self.assertEqual(d.confidence, "low")
        self.assertFalse(d.resolved)

    def test_reasoning_is_captured_and_capped(self) -> None:
        d = _from_payload(_payload(reasoning="y" * 900), _request())
        self.assertLessEqual(len(d.reasoning), 400)


class PromptTests(unittest.TestCase):
    def test_prompt_contains_source_and_every_candidate(self) -> None:
        prompt = _request().prompt()
        self.assertIn("NW Ironwood Supply Co", prompt)
        self.assertIn("id=1020", prompt)
        self.assertIn("id=1001", prompt)

    def test_absent_optional_fields_are_omitted(self) -> None:
        bare = Request(live_business_name="A", state="OR", source_name="S", candidates=())
        prompt = bare.prompt()
        self.assertNotIn("postal=", prompt)
        self.assertNotIn("amount=", prompt)


class BatchTests(unittest.TestCase):
    def test_stub_declines_by_default(self) -> None:
        results = adjudicate_all(StubModel(), [_request(), _request()])
        self.assertEqual(len(results), 2)
        self.assertFalse(any(r.resolved for r in results))

    def test_row_cap_is_enforced(self) -> None:
        model = StubModel()
        adjudicate_all(model, [_request()] * 10, max_rows=3)
        self.assertEqual(len(model.calls), 3, "must not exceed the row cap")

    def test_stub_can_simulate_resolution(self) -> None:
        results = adjudicate_all(StubModel(always="top"), [_request()])
        self.assertTrue(results[0].resolved)
        self.assertEqual(results[0].application_id, 1020)


if __name__ == "__main__":
    unittest.main()
