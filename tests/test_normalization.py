"""Tests for the name normalization layer.

Everything the matcher does rests on turning two differently-written versions of
the same business name into comparable strings, so these are the tests that
matter most.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from entity_match_pipeline.normalization import (  # noqa: E402
    ascii_fold,
    build_name_variants,
    looks_like_identifier,
    normalize_text,
    remove_stopwords,
    significant_tokens,
    split_dba_variants,
    stable_hash,
    strip_legal_suffixes,
    strip_parenthetical_notes,
)


class TextNormalizationTests(unittest.TestCase):
    def test_accents_are_folded(self) -> None:
        self.assertEqual(ascii_fold("Café Motörhead"), "Cafe Motorhead")

    def test_parentheticals_are_removed(self) -> None:
        self.assertEqual(strip_parenthetical_notes("Acme Corp (formerly Beta)").strip(), "Acme Corp")

    def test_punctuation_and_case_are_normalized(self) -> None:
        self.assertEqual(normalize_text("  ACME/Beta, Inc.  "), "acme beta inc")

    def test_shorthand_is_expanded(self) -> None:
        self.assertEqual(normalize_text("Cuts 4 U"), "cuts for you")
        self.assertEqual(normalize_text("Smith & Sons"), "smith and sons")

    def test_ampersand_and_plus_are_equivalent(self) -> None:
        self.assertEqual(normalize_text("A & B"), normalize_text("A + B"))


class LegalSuffixTests(unittest.TestCase):
    def test_suffix_is_stripped(self) -> None:
        self.assertEqual(strip_legal_suffixes("Harbor Point Marine LLC"), "harbor point marine")

    def test_punctuated_suffix_is_stripped(self) -> None:
        self.assertEqual(
            strip_legal_suffixes("Harbor Point Marine, L.L.C."),
            strip_legal_suffixes("Harbor Point Marine LLC"),
        )

    def test_various_suffixes(self) -> None:
        for name in ("Acme Inc", "Acme Corp", "Acme Ltd", "Acme LLP", "Acme PLLC", "Acme Co"):
            self.assertEqual(strip_legal_suffixes(name), "acme", msg=name)


class StopwordTests(unittest.TestCase):
    def test_generic_words_are_dropped(self) -> None:
        self.assertEqual(remove_stopwords("cedar consulting group services"), "cedar")

    def test_a_name_of_only_stopwords_becomes_empty(self) -> None:
        # This is why the matcher needs a separate guard: the distinguishing
        # content of "Capital Investment Group" is nothing at all.
        self.assertEqual(remove_stopwords(strip_legal_suffixes("Capital Investment Group")), "")


class DbaTests(unittest.TestCase):
    def test_both_sides_of_a_dba_are_produced(self) -> None:
        variants = split_dba_variants("Kestrel Holdings LLC dba Kestrel Coffee Roasters")
        joined = " | ".join(variants).lower()
        self.assertIn("kestrel holdings", joined)
        self.assertIn("kestrel coffee roasters", joined)

    def test_doing_business_as_spelled_out(self) -> None:
        variants = split_dba_variants("Acme Holdings doing business as Acme Diner")
        self.assertTrue(any("diner" in v.lower() for v in variants))

    def test_plain_name_yields_itself(self) -> None:
        self.assertEqual(split_dba_variants("Acme Diner"), ["Acme Diner"])


class NameVariantTests(unittest.TestCase):
    def test_shorthand_and_dba_together(self) -> None:
        variants = build_name_variants("Cuts 4 U LLC dba Cuts for You")
        self.assertIn("cuts for you", variants)

    def test_variants_are_deduplicated(self) -> None:
        variants = build_name_variants("Acme LLC")
        self.assertEqual(len(variants), len(set(variants)))

    def test_nan_and_none_are_ignored(self) -> None:
        self.assertEqual(build_name_variants(None), [])
        self.assertEqual(build_name_variants("nan"), [])
        self.assertEqual(build_name_variants(float("nan")), [])

    def test_multiple_inputs_are_combined(self) -> None:
        variants = build_name_variants("Acme Diner", "Acme Restaurant")
        self.assertTrue(any("diner" in v for v in variants))
        self.assertTrue(any("restaurant" in v for v in variants))


class TokenTests(unittest.TestCase):
    def test_distinguishing_tokens_only(self) -> None:
        self.assertEqual(significant_tokens("Cedar Consulting Group LLC"), {"cedar"})

    def test_shared_token_detection(self) -> None:
        a = significant_tokens("Ironwood Custom Cabinetry")
        b = significant_tokens("Custom Cabinetry Ironwood")
        self.assertTrue(a & b)


class IdentifierTests(unittest.TestCase):
    def test_identifier_shapes_are_detected(self) -> None:
        self.assertTrue(looks_like_identifier("A100234"))
        self.assertTrue(looks_like_identifier("100234"))

    def test_real_names_are_not_identifiers(self) -> None:
        self.assertFalse(looks_like_identifier("Acme Diner"))
        self.assertFalse(looks_like_identifier("Studio 54 Salon"))


class HashTests(unittest.TestCase):
    def test_hash_is_stable_and_order_sensitive(self) -> None:
        self.assertEqual(stable_hash(["a", "b"]), stable_hash(["a", "b"]))
        self.assertNotEqual(stable_hash(["a", "b"]), stable_hash(["b", "a"]))

    def test_none_is_handled(self) -> None:
        self.assertEqual(stable_hash([None, "a"]), stable_hash(["", "a"]))


if __name__ == "__main__":
    unittest.main()
