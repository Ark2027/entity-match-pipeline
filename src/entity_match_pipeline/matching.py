from __future__ import annotations

import math
import re

import pandas as pd
from rapidfuzz import fuzz

from .normalization import (
    build_name_variants,
    looks_like_identifier,
    remove_stopwords,
    significant_tokens,
    stable_hash,
    strip_legal_suffixes,
)
from .settings import AppConfig


def _join_name_parts(*parts: object) -> str:
    values: list[str] = []
    for part in parts:
        if pd.isna(part):
            continue
        text = str(part).strip()
        if text:
            values.append(text)
    return " ".join(values)


def prepare_candidate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    prepared["candidate_state"] = (
        prepared["business_state"].fillna("").replace("", pd.NA).fillna(prepared["prescreen_state"])
    )
    prepared["candidate_state"] = prepared["candidate_state"].fillna("").astype(str).str.upper().str.strip()
    prepared["candidate_name_variants"] = prepared.apply(
        lambda row: build_name_variants(
            row.get("crm_business_name", ""),
            _join_name_parts(row.get("customer_first_name", ""), row.get("customer_last_name", "")),
            _join_name_parts(row.get("prescreen_first_name", ""), row.get("prescreen_last_name", "")),
        ),
        axis=1,
    )
    prepared["candidate_key"] = prepared.apply(
        lambda row: stable_hash((row["application_id"], row["business_id"], row["customer_id"])),
        axis=1,
    )
    return prepared


def prepare_live_frame(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    prepared["live_name_variants"] = prepared["live_business_name"].apply(build_name_variants)
    prepared["dedupe_name_key"] = prepared["live_name_variants"].apply(
        lambda variants: variants[0] if variants else ""
    )
    prepared["is_identifier_only"] = prepared["live_business_name"].apply(looks_like_identifier)
    prepared["live_record_key"] = prepared.apply(
        lambda row: stable_hash(
            (
                row["source_code"],
                row["state"],
                row["dedupe_name_key"],
                row["origination_date"].date().isoformat() if pd.notna(row["origination_date"]) else "",
                f"{row['disbursed_amount']:.2f}" if pd.notna(row["disbursed_amount"]) else "",
            )
        ),
        axis=1,
    )
    return prepared


def collapse_duplicate_live_rows(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if frame.empty:
        return frame, pd.DataFrame()
    prepared = prepare_live_frame(frame)
    group_cols = ["source_code", "state", "dedupe_name_key"]
    duplicate_counts = prepared.groupby(group_cols, dropna=False).size().rename("duplicate_count")
    aggregated = prepared.groupby(group_cols, dropna=False).agg(
        source_name=("source_name", "first"),
        live_business_name=("live_business_name", "first"),
        live_name_variants=("live_name_variants", "first"),
        is_identifier_only=("is_identifier_only", "first"),
        live_record_key=("live_record_key", "first"),
        source_files=("source_file", lambda series: "; ".join(sorted(series.astype(str).unique()))),
        source_sheets=("source_sheet", lambda series: "; ".join(sorted(series.astype(str).unique()))),
        source_row_numbers=("source_row_number", lambda series: ", ".join(str(int(value)) for value in series)),
        origination_dates=("origination_date", lambda series: "; ".join(sorted(value.date().isoformat() for value in series.dropna()))),
        primary_origination_date=("origination_date", "min"),
        origination_amounts=("disbursed_amount", lambda series: "; ".join(f"{value:,.2f}" for value in sorted(series.dropna().unique()))),
        primary_origination_amount=("disbursed_amount", "sum"),
        live_zip_codes=("live_zip_code", lambda series: "; ".join(sorted({str(value) for value in series if str(value).strip()}))),
    ).reset_index()
    aggregated = aggregated.merge(
        duplicate_counts.reset_index(),
        on=group_cols,
        how="left",
    )
    duplicates = aggregated.loc[aggregated["duplicate_count"] > 1].copy()
    return aggregated, duplicates


def _parse_loan_range(raw_value: object) -> tuple[float | None, float | None]:
    if raw_value is None or (isinstance(raw_value, float) and math.isnan(raw_value)):
        return None, None
    text = str(raw_value).replace(",", "")
    multipliers = {"k": 1_000.0, "m": 1_000_000.0}
    values = [
        float(match.group(1)) * multipliers.get(match.group(2).lower(), 1.0)
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*([kKmM]?)", text)
    ]
    if not values:
        return None, None
    if len(values) == 1:
        # Open-ended ranges like "+ $250K" have no upper bound.
        if "+" in text:
            return values[0], math.inf
        return values[0], values[0]
    return min(values), max(values)


def _score_name_variants(live_variants: list[str], candidate_variants: list[str]) -> tuple[float, str, str]:
    best_score = 0.0
    best_live = ""
    best_candidate = ""
    for live_variant in live_variants:
        live_tokens = significant_tokens(live_variant)
        live_core = remove_stopwords(strip_legal_suffixes(live_variant))
        for candidate_variant in candidate_variants:
            exact = 100.0 if live_variant == candidate_variant else 0.0
            ratio = float(fuzz.ratio(live_variant, candidate_variant))
            token_set = float(fuzz.token_set_ratio(live_variant, candidate_variant))
            token_sort = float(fuzz.token_sort_ratio(live_variant, candidate_variant))
            partial = float(fuzz.partial_ratio(live_variant, candidate_variant))
            token_overlap = 0.0
            candidate_tokens = significant_tokens(candidate_variant)
            candidate_core = remove_stopwords(strip_legal_suffixes(candidate_variant))
            core_ratio = float(fuzz.ratio(live_core, candidate_core)) if live_core or candidate_core else ratio
            union = live_tokens | candidate_tokens
            if union:
                token_overlap = (len(live_tokens & candidate_tokens) / len(union)) * 100.0
            token_combo = (token_set * 0.45) + (token_sort * 0.20) + (token_overlap * 0.35)
            containment = bool(live_tokens and candidate_tokens and (live_tokens <= candidate_tokens or candidate_tokens <= live_tokens))
            score = max(exact, ratio, token_combo)
            if token_overlap >= 50 or containment or exact == 100.0:
                score = max(score, partial * 0.92)
            if token_overlap == 0 and not containment and exact != 100.0:
                score = min(score, core_ratio)
            if score > best_score:
                best_score = score
                best_live = live_variant
                best_candidate = candidate_variant
    return round(best_score, 2), best_live, best_candidate


def _supporting_adjustments(live_row: pd.Series, candidate_row: pd.Series) -> tuple[float, list[str]]:
    adjustment = 0.0
    reasons = ["source matched", "state matched"]
    live_zip = str(live_row.get("live_zip_codes", "")).strip()
    candidate_zips = {
        str(candidate_row.get("business_zip_code", "")).strip(),
        str(candidate_row.get("prescreen_zip_code", "")).strip(),
        str(candidate_row.get("prescreen_business_zip_code", "")).strip(),
    }
    candidate_zips.discard("")
    if live_zip and any(zip_code in live_zip for zip_code in candidate_zips):
        adjustment += 4.0
        reasons.append("zip matched")
    low, high = _parse_loan_range(candidate_row.get("loan_range"))
    amount = live_row.get("primary_origination_amount")
    if low is not None and high is not None and pd.notna(amount):
        if low <= float(amount) <= high:
            adjustment += 3.0
            reasons.append("amount within loan range")
        elif float(amount) >= low * 0.5 and float(amount) <= high * 1.5:
            adjustment += 1.0
            reasons.append("amount roughly aligned with loan range")
    lead_start = candidate_row.get("lead_start_date")
    origination_date = live_row.get("primary_origination_date")
    if pd.notna(lead_start) and pd.notna(origination_date):
        delta_days = (origination_date.date() - lead_start.date()).days
        if delta_days >= 0:
            reasons.append("origination after lead created")
        else:
            reasons.append("origination slightly before lead date")
    return adjustment, reasons


def _origination_predates_lead(
    live_row: pd.Series,
    candidate_row: pd.Series,
    grace_days: float,
) -> bool:
    lead_start = candidate_row.get("lead_start_date")
    origination_date = live_row.get("primary_origination_date")
    if pd.isna(lead_start) or pd.isna(origination_date):
        return False
    return (origination_date.date() - lead_start.date()).days < -grace_days


def _has_meaningful_name_signal(
    best_live_variant: str,
    best_candidate_variant: str,
    name_score: float,
) -> bool:
    live_tokens = significant_tokens(best_live_variant)
    candidate_tokens = significant_tokens(best_candidate_variant)
    if live_tokens & candidate_tokens:
        return True
    live_core = remove_stopwords(strip_legal_suffixes(best_live_variant))
    candidate_core = remove_stopwords(strip_legal_suffixes(best_candidate_variant))
    if not live_core or not candidate_core:
        # Names made entirely of stopwords (e.g. "Capital Investment Group") fall
        # back to comparing the full normalized strings.
        return float(fuzz.ratio(best_live_variant, best_candidate_variant)) >= 90.0
    compact_live = live_core.replace(" ", "")
    compact_candidate = candidate_core.replace(" ", "")
    if compact_live == compact_candidate:
        return True
    if compact_live and compact_candidate and (
        compact_live in compact_candidate or compact_candidate in compact_live
    ):
        return True
    core_ratio = float(fuzz.ratio(live_core, candidate_core))
    partial_core = float(fuzz.partial_ratio(live_core, candidate_core))
    if core_ratio >= 85.0 or partial_core >= 92.0:
        return True
    return name_score >= 80.0 and max(core_ratio, partial_core) >= 80.0


def _classify_match(score: float, gap: float, name_score: float, config: AppConfig) -> str:
    thresholds = config.thresholds
    if (
        score >= thresholds.auto_accept_score
        and name_score >= thresholds.auto_accept_score - 2
        and gap >= thresholds.min_score_gap
    ):
        return "auto_accept"
    if score >= thresholds.review_score:
        return "review"
    if score >= thresholds.possible_score:
        return "possible_match"
    return "discard"


def match_live_rows(
    live_rows: pd.DataFrame,
    candidates: pd.DataFrame,
    config: AppConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    blocked: dict[tuple[str, str], pd.DataFrame] = {}
    for key, group in candidates.groupby(["candidate_source", "candidate_state"], dropna=False):
        blocked[key] = group
    matches: list[dict[str, object]] = []
    exceptions: list[dict[str, object]] = []
    no_candidate: list[dict[str, object]] = []
    for _, live_row in live_rows.iterrows():
        if live_row["is_identifier_only"]:
            exceptions.append(
                {
                    "exception_type": "identifier_only_name",
                    "source_code": live_row["source_code"],
                    "state": live_row["state"],
                    "live_business_name": live_row["live_business_name"],
                    "origination_amounts": live_row["origination_amounts"],
                    "origination_dates": live_row["origination_dates"],
                    "detail": "Business name looks like a source-side identifier; no alternate identifier source is available yet.",
                }
            )
            continue
        candidate_group = blocked.get((live_row["source_code"], live_row["state"]))
        if candidate_group is None or candidate_group.empty:
            no_candidate.append(
                {
                    "source_code": live_row["source_code"],
                    "state": live_row["state"],
                    "live_business_name": live_row["live_business_name"],
                    "reason": "No candidates for this source/state.",
                }
            )
            continue
        scored_rows: list[dict[str, object]] = []
        excluded_for_date = 0
        for _, candidate_row in candidate_group.iterrows():
            if _origination_predates_lead(
                live_row,
                candidate_row,
                config.thresholds.max_days_origination_before_lead,
            ):
                excluded_for_date += 1
                continue
            name_score, best_live_variant, best_candidate_variant = _score_name_variants(
                live_row["live_name_variants"],
                candidate_row["candidate_name_variants"],
            )
            if name_score < config.thresholds.min_name_score:
                continue
            if not _has_meaningful_name_signal(
                best_live_variant,
                best_candidate_variant,
                name_score,
            ):
                continue
            adjustment, reasons = _supporting_adjustments(live_row, candidate_row)
            overall_score = round(min(100.0, name_score + adjustment), 2)
            if overall_score < config.thresholds.min_candidate_score:
                continue
            scored_rows.append(
                {
                    **candidate_row.to_dict(),
                    "overall_score": overall_score,
                    "name_score": name_score,
                    "supporting_signals": "; ".join(reasons),
                    "best_live_variant": best_live_variant,
                    "best_candidate_variant": best_candidate_variant,
                }
            )
        if not scored_rows:
            no_candidate.append(
                {
                    "source_code": live_row["source_code"],
                    "state": live_row["state"],
                    "live_business_name": live_row["live_business_name"],
                    "reason": (
                        "No candidate cleared the score threshold."
                        + (
                            f" ({excluded_for_date} candidate(s) excluded because the record predates the lead.)"
                            if excluded_for_date
                            else ""
                        )
                    ),
                }
            )
            continue
        scored_rows.sort(key=lambda item: item["overall_score"], reverse=True)
        top = scored_rows[0]
        second_score = scored_rows[1]["overall_score"] if len(scored_rows) > 1 else 0.0
        gap = round(top["overall_score"] - second_score, 2)
        decision_band = _classify_match(
            top["overall_score"],
            gap,
            top["name_score"],
            config,
        )
        if decision_band == "discard":
            no_candidate.append(
                {
                    "source_code": live_row["source_code"],
                    "state": live_row["state"],
                    "live_business_name": live_row["live_business_name"],
                    "reason": "Top candidate still fell below review threshold.",
                }
            )
            continue
        matches.append(
            {
                "decision_band": decision_band,
                "source_code": live_row["source_code"],
                "source_name": live_row["source_name"],
                "state": live_row["state"],
                "live_business_name": live_row["live_business_name"],
                "live_record_key": live_row["live_record_key"],
                "crm_source_code": top["candidate_source"],
                "crm_source_name": top.get("candidate_source_name", ""),
                "origination_dates": live_row["origination_dates"],
                "primary_origination_date": live_row["primary_origination_date"],
                "origination_amounts": live_row["origination_amounts"],
                "primary_origination_amount": live_row["primary_origination_amount"],
                "source_files": live_row["source_files"],
                "source_sheets": live_row["source_sheets"],
                "source_row_numbers": live_row["source_row_numbers"],
                "duplicate_count": live_row["duplicate_count"],
                "crm_business_name": top["crm_business_name"],
                "candidate_application_id": top["application_id"],
                "candidate_business_id": top["business_id"],
                "candidate_customer_id": top["customer_id"],
                "candidate_status": top["application_status"],
                "candidate_loan_range": top["loan_range"],
                "candidate_business_state": top["candidate_state"],
                "candidate_business_zip": top["business_zip_code"],
                "application_created_date": top["application_created_date"],
                "prescreen_created_date": top["prescreen_created_date"],
                "lead_start_date": top["lead_start_date"],
                "match_score": top["overall_score"],
                "name_score": top["name_score"],
                "score_gap": gap,
                "matched_live_variant": top["best_live_variant"],
                "matched_candidate_variant": top["best_candidate_variant"],
                "supporting_signals": top["supporting_signals"],
                "alternative_candidate_name": scored_rows[1]["crm_business_name"] if len(scored_rows) > 1 else "",
                "alternative_candidate_score": second_score if len(scored_rows) > 1 else "",
            }
        )
    match_frame = pd.DataFrame(matches)
    if not match_frame.empty:
        match_frame = match_frame.sort_values(
            by=["decision_band", "match_score", "name_score"],
            ascending=[True, False, False],
        ).reset_index(drop=True)
    return match_frame, pd.DataFrame(exceptions), pd.DataFrame(no_candidate)
