from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .settings import AppConfig, QuarterWindow, quarter_from_label, resolve_source_mapping


REQUIRED_COLUMNS = ("Business Name", "State", "Origination Date", "$ disbursed")


@dataclass(frozen=True)
class LiveLoadResult:
    rows: pd.DataFrame
    exceptions: pd.DataFrame


def _find_origination_sheet(path: Path) -> str:
    workbook = pd.ExcelFile(path)
    for sheet_name in workbook.sheet_names:
        if "origination" in sheet_name.lower():
            return sheet_name
    raise ValueError(f"No origination sheet found in {path.name}")


def _coerce_excel_date(value: object) -> pd.Timestamp | pd.NaT:
    if pd.isna(value):
        return pd.NaT
    return pd.to_datetime(value, errors="coerce")


def discover_live_quarters(
    config: AppConfig,
    *,
    max_quarter: QuarterWindow | None = None,
) -> list[QuarterWindow]:
    quarter_labels: set[str] = set()
    for workbook_path in sorted(config.live_data_dir.glob("*.xlsx")):
        mapping = resolve_source_mapping(workbook_path.name, config.source_mappings)
        if mapping is None:
            continue
        try:
            sheet_name = _find_origination_sheet(workbook_path)
            frame = pd.read_excel(workbook_path, sheet_name=sheet_name)
        except Exception:
            continue
        if "Origination Date" not in frame.columns:
            continue
        dates = frame["Origination Date"].apply(_coerce_excel_date).dropna()
        for value in dates:
            quarter = ((value.month - 1) // 3) + 1
            label = f"{value.year}Q{quarter}"
            if max_quarter and label > max_quarter.label:
                continue
            quarter_labels.add(label)
    return [quarter_from_label(label) for label in sorted(quarter_labels)]


def load_live_originations(config: AppConfig, quarter: QuarterWindow) -> LiveLoadResult:
    rows: list[pd.DataFrame] = []
    exceptions: list[dict[str, object]] = []
    for workbook_path in sorted(config.live_data_dir.glob("*.xlsx")):
        mapping = resolve_source_mapping(workbook_path.name, config.source_mappings)
        if mapping is None:
            exceptions.append(
                {
                    "exception_type": "unmapped_source_file",
                    "source_file": workbook_path.name,
                    "detail": "No source mapping matched the workbook filename.",
                }
            )
            continue
        sheet_name = _find_origination_sheet(workbook_path)
        frame = pd.read_excel(workbook_path, sheet_name=sheet_name)
        missing_columns = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing_columns:
            exceptions.append(
                {
                    "exception_type": "missing_required_columns",
                    "source_file": workbook_path.name,
                    "detail": ", ".join(missing_columns),
                }
            )
            continue
        frame = frame.copy()
        frame["source_file"] = workbook_path.name
        frame["source_sheet"] = sheet_name
        frame["source_row_number"] = frame.index + 2
        frame["source_name"] = mapping.source_name
        frame["source_code"] = mapping.source_code
        frame["live_business_name"] = frame["Business Name"].fillna("").astype(str).str.strip()
        frame["state"] = frame["State"].fillna("").astype(str).str.upper().str.strip()
        frame["origination_date"] = pd.to_datetime(
            frame["Origination Date"].apply(_coerce_excel_date), errors="coerce"
        )
        frame["disbursed_amount"] = pd.to_numeric(frame["$ disbursed"], errors="coerce")
        frame["live_zip_code"] = (
            frame["Zip Code"].fillna("").astype(str).str.strip()
            if "Zip Code" in frame.columns
            else ""
        )
        in_quarter = frame["origination_date"].dt.date.between(quarter.start, quarter.end)
        quarter_frame = frame.loc[in_quarter].copy()
        missing_state = quarter_frame["state"] == ""
        if missing_state.any():
            for _, row in quarter_frame.loc[missing_state].iterrows():
                exceptions.append(
                    {
                        "exception_type": "missing_state",
                        "source_file": row["source_file"],
                        "source_row_number": row["source_row_number"],
                        "live_business_name": row["live_business_name"],
                        "detail": "State is required for matching.",
                    }
                )
            quarter_frame = quarter_frame.loc[~missing_state].copy()
        missing_name = quarter_frame["live_business_name"] == ""
        if missing_name.any():
            for _, row in quarter_frame.loc[missing_name].iterrows():
                exceptions.append(
                    {
                        "exception_type": "missing_business_name",
                        "source_file": row["source_file"],
                        "source_row_number": row["source_row_number"],
                        "detail": "Business name is blank.",
                    }
                )
            quarter_frame = quarter_frame.loc[~missing_name].copy()
        rows.append(
            quarter_frame[
                [
                    "source_file",
                    "source_sheet",
                    "source_row_number",
                    "source_name",
                    "source_code",
                    "live_business_name",
                    "state",
                    "origination_date",
                    "disbursed_amount",
                    "live_zip_code",
                ]
            ]
        )
    live_rows = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    exception_frame = pd.DataFrame(exceptions)
    return LiveLoadResult(rows=live_rows, exceptions=exception_frame)
