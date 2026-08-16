from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable
import math


LEGAL_SUFFIXES = {
    "llc",
    "inc",
    "corp",
    "corporation",
    "ltd",
    "lp",
    "llp",
    "pllc",
    "pc",
    "co",
}

STOPWORDS = {
    "the",
    "and",
    "group",
    "services",
    "service",
    "solutions",
    "solution",
    "enterprise",
    "enterprises",
    "company",
    "consulting",
    "consultants",
    "counseling",
    "entertainment",
    "technology",
    "technologies",
    "logistics",
    "medical",
    "health",
    "wellness",
    "transport",
    "transportation",
    "courier",
    "couriers",
    "investment",
    "investments",
    "capital",
    "holdings",
    "ventures",
    "properties",
    "property",
    "management",
    "financial",
    "finance",
    "partners",
    "industries",
    "international",
}

WHOLE_WORD_REPLACEMENTS = {
    "4": "for",
    "u": "you",
    "&": "and",
    "+": "and",
}

DBA_PATTERNS = (
    r"\bdba\b",
    r"\bdoing business as\b",
)


def ascii_fold(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", str(value))
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def strip_parenthetical_notes(value: str) -> str:
    return re.sub(r"\([^)]*\)", " ", str(value))


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _replace_whole_words(value: str) -> str:
    tokens = value.split()
    return " ".join(WHOLE_WORD_REPLACEMENTS.get(token, token) for token in tokens)


def _collapse_dotted_initialisms(value: str) -> str:
    """Turn "l.l.c." into "llc" before punctuation is stripped.

    Without this, removing punctuation first leaves "l l c", which no longer
    matches the "llc" entry in LEGAL_SUFFIXES. The same applies to "p.c." and
    "l.l.p.". Worth handling, because whether someone punctuates the legal
    suffix is exactly the sort of difference that should not affect a match.
    """
    return re.sub(r"\b(?:[a-z]\.){2,}", lambda match: match.group(0).replace(".", ""), value)


def normalize_text(value: str) -> str:
    text = ascii_fold(value or "").lower()
    text = strip_parenthetical_notes(text)
    text = _collapse_dotted_initialisms(text)
    text = text.replace("/", " ")
    text = re.sub(r"[^\w\s&+]", " ", text)
    text = _replace_whole_words(text)
    return normalize_whitespace(text)


def strip_legal_suffixes(value: str) -> str:
    tokens = [token for token in normalize_text(value).split() if token not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def remove_stopwords(value: str) -> str:
    tokens = [token for token in value.split() if token not in STOPWORDS]
    return " ".join(tokens)


def split_dba_variants(value: str) -> list[str]:
    text = strip_parenthetical_notes("" if value is None else value)
    variants = [text]
    for pattern in DBA_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            left = text[: match.start()].strip(" ,-/")
            right = text[match.end() :].strip(" ,-/")
            if left:
                variants.append(left)
            if right:
                variants.append(right)
    return [variant for variant in variants if variant]


def build_name_variants(*values: str) -> list[str]:
    variants: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        if str(value).strip().lower() == "nan":
            continue
        for segment in split_dba_variants(value):
            normalized = normalize_text(segment)
            if normalized:
                variants.append(normalized)
            stripped = strip_legal_suffixes(segment)
            if stripped:
                variants.append(stripped)
                without_stopwords = remove_stopwords(stripped)
                if without_stopwords:
                    variants.append(without_stopwords)
    deduped = []
    seen = set()
    for variant in variants:
        cleaned = normalize_whitespace(variant)
        if cleaned and cleaned not in seen:
            deduped.append(cleaned)
            seen.add(cleaned)
    return deduped


def significant_tokens(value: str) -> set[str]:
    text = remove_stopwords(strip_legal_suffixes(value))
    return {token for token in text.split() if token}


def stable_hash(parts: Iterable[object]) -> str:
    joined = "||".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def looks_like_identifier(value: str) -> bool:
    normalized = normalize_text(value)
    return bool(re.fullmatch(r"[a-z]?\d{3,}", normalized))
