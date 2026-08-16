from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ThresholdConfig:
    min_name_score: float = 55.0
    min_candidate_score: float = 58.0
    possible_score: float = 58.0
    review_score: float = 60.0
    auto_accept_score: float = 94.0
    min_score_gap: float = 5.0
    max_days_origination_before_lead: float = 30.0
    ambiguous_gap: float = 2.0
    max_review_rows: int = 20


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = True


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    database: str
    schema: str
    user: str
    password_env_var: str

    @property
    def password(self) -> str:
        value = os.environ.get(self.password_env_var)
        if not value:
            raise RuntimeError(
                f"Environment variable {self.password_env_var} is not set."
            )
        return value


@dataclass(frozen=True)
class SSHConfig:
    host: str
    user: str
    key_path: Path


@dataclass(frozen=True)
class SourceMapping:
    source_name: str
    source_code: str
    file_markers: tuple[str, ...]


@dataclass(frozen=True)
class QuarterWindow:
    label: str
    start: date
    end: date


@dataclass(frozen=True)
class AppConfig:
    live_data_dir: Path
    output_dir: Path
    history_db_path: Path
    # Both are optional so the pipeline can run against a candidate CSV with no
    # database or SSH access at all, which is how the bundled demo works.
    db: DatabaseConfig | None = None
    ssh: SSHConfig | None = None
    candidates_csv: Path | None = None
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    source_mappings: tuple[SourceMapping, ...] = field(default_factory=tuple)
    workbook_enabled: bool = True

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


# Source workbooks are identified by filename. Each mapping ties a short code to
# the markers that may appear in a filename, so "Q3 originations - NORTHWEST.xlsx"
# and "northwest_q3.xlsx" both resolve to the same source.
#
# These are placeholders. Define your own in settings.json under
# "source_mappings"; anything defined there replaces this list entirely.
DEFAULT_SOURCE_MAPPINGS = (
    SourceMapping("Northwest Partners", "NWP", ("Northwest", "NWP")),
    SourceMapping("Great Lakes Collective", "GLC", ("Great Lakes", "GLC")),
    SourceMapping("Southwest Alliance", "SWA", ("Southwest", "SWA")),
    SourceMapping("Atlantic Community Fund", "ACF", ("Atlantic", "ACF")),
)


def _quarter_end(year: int, quarter: int) -> date:
    if quarter == 1:
        return date(year, 3, 31)
    if quarter == 2:
        return date(year, 6, 30)
    if quarter == 3:
        return date(year, 9, 30)
    return date(year, 12, 31)


def previous_calendar_quarter(as_of: date) -> QuarterWindow:
    current_quarter = ((as_of.month - 1) // 3) + 1
    quarter = current_quarter - 1
    year = as_of.year
    if quarter == 0:
        quarter = 4
        year -= 1
    start_month = ((quarter - 1) * 3) + 1
    start = date(year, start_month, 1)
    end = _quarter_end(year, quarter)
    return QuarterWindow(label=f"{year}Q{quarter}", start=start, end=end)


def quarter_from_label(label: str) -> QuarterWindow:
    normalized = label.strip().upper()
    if len(normalized) != 6 or normalized[4] != "Q":
        raise ValueError(f"Quarter label must look like 2025Q4, got {label!r}")
    year = int(normalized[:4])
    quarter = int(normalized[-1])
    if quarter not in {1, 2, 3, 4}:
        raise ValueError(f"Quarter label must end with Q1-Q4, got {label!r}")
    start_month = ((quarter - 1) * 3) + 1
    return QuarterWindow(
        label=normalized,
        start=date(year, start_month, 1),
        end=_quarter_end(year, quarter),
    )


def latest_due_quarter(as_of: date) -> QuarterWindow:
    checkpoints = []
    for year in (as_of.year - 1, as_of.year):
        checkpoints.extend(
            [
                (date(year, 1, 30), f"{year - 1}Q4"),
                (date(year, 4, 30), f"{year}Q1"),
                (date(year, 7, 30), f"{year}Q2"),
                (date(year, 10, 30), f"{year}Q3"),
            ]
        )
    eligible = [label for checkpoint, label in checkpoints if checkpoint <= as_of]
    if not eligible:
        return quarter_from_label(f"{as_of.year - 1}Q3")
    return quarter_from_label(sorted(eligible)[-1])


def load_config(config_path: Path) -> AppConfig:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    source_mappings = tuple(
        SourceMapping(
            source_name=item["source_name"],
            source_code=item["source_code"],
            file_markers=tuple(item["file_markers"]),
        )
        for item in raw.get("source_mappings", [])
    ) or DEFAULT_SOURCE_MAPPINGS
    thresholds = ThresholdConfig(**raw.get("thresholds", {}))
    notifications = NotificationConfig(**raw.get("notifications", {}))
    candidates_csv = Path(raw["candidates_csv"]) if raw.get("candidates_csv") else None

    db_raw = raw.get("db")
    ssh_raw = raw.get("ssh")
    if candidates_csv is None and (db_raw is None or ssh_raw is None):
        raise ValueError("Config must provide either 'candidates_csv' or both 'db' and 'ssh'.")

    config = AppConfig(
        live_data_dir=Path(raw["live_data_dir"]),
        output_dir=Path(raw["output_dir"]),
        history_db_path=Path(raw["history_db_path"]),
        candidates_csv=candidates_csv,
        db=DatabaseConfig(
            host=db_raw["host"],
            database=db_raw["database"],
            schema=db_raw.get("schema", "public"),
            user=db_raw["user"],
            password_env_var=db_raw["password_env_var"],
        ) if db_raw else None,
        ssh=SSHConfig(
            host=ssh_raw["host"],
            user=ssh_raw["user"],
            key_path=Path(ssh_raw["key_path"]),
        ) if ssh_raw else None,
        thresholds=thresholds,
        notifications=notifications,
        source_mappings=source_mappings,
        workbook_enabled=bool(raw.get("workbook_enabled", True)),
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.history_db_path.parent.mkdir(parents=True, exist_ok=True)
    return config


def resolve_source_mapping(
    filename: str,
    mappings: tuple[SourceMapping, ...],
) -> SourceMapping | None:
    for mapping in mappings:
        if any(marker.lower() in filename.lower() for marker in mapping.file_markers):
            return mapping
    return None


def to_jsonable_summary(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, default=str)
