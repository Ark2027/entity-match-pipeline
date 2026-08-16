from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .normalization import stable_hash


VALID_STATUSES = ("new", "confirmed", "rejected", "billed")
TABLE_NAME = "loan_matches"

TABLE_COLUMNS = (
    "match_key",
    "live_record_key",
    "candidate_application_id",
    "candidate_business_id",
    "candidate_customer_id",
    "decision_band",
    "status",
    "status_note",
    "match_score",
    "name_score",
    "score_gap",
    "billed_quarter",
    "billed_amount",
    "invoice_date",
    "first_seen_at",
    "last_seen_at",
    "status_updated_at",
    "quarter_first_seen",
    "first_run_label",
    "last_run_label",
    "times_seen",
    "source_code",
    "source_name",
    "state",
    "live_business_name",
    "crm_source_code",
    "crm_source_name",
    "crm_business_name",
    "origination_dates",
    "primary_origination_date",
    "origination_amounts",
    "primary_origination_amount",
    "source_files",
    "source_sheets",
    "source_row_numbers",
    "duplicate_count",
    "candidate_status",
    "candidate_loan_range",
    "candidate_business_state",
    # Postal code is kept because it carries a real scoring adjustment. Email and
    # phone are deliberately not persisted: they were only ever reviewer context,
    # contribute nothing to the score, and a match history is a bad place to
    # accumulate contact details. Look them up in the CRM if you need them.
    "candidate_business_zip",
    "application_created_date",
    "prescreen_created_date",
    "lead_start_date",
    "matched_live_variant",
    "matched_candidate_variant",
    "supporting_signals",
    "alternative_candidate_name",
    "alternative_candidate_score",
)

SCHEMA = f"""
create table if not exists {TABLE_NAME} (
    match_key text primary key,
    live_record_key text not null,
    candidate_application_id integer not null,
    candidate_business_id integer,
    candidate_customer_id integer,
    decision_band text not null,
    status text not null default 'new',
    status_note text not null default '',
    match_score real not null,
    name_score real,
    score_gap real,
    billed_quarter text,
    billed_amount real,
    invoice_date text,
    first_seen_at text not null,
    last_seen_at text not null,
    status_updated_at text,
    quarter_first_seen text not null,
    first_run_label text not null,
    last_run_label text not null,
    times_seen integer not null default 1,
    source_code text,
    source_name text,
    state text,
    live_business_name text,
    crm_source_code text,
    crm_source_name text,
    crm_business_name text,
    origination_dates text,
    primary_origination_date text,
    origination_amounts text,
    primary_origination_amount real,
    source_files text,
    source_sheets text,
    source_row_numbers text,
    duplicate_count integer,
    candidate_status text,
    candidate_loan_range text,
    candidate_business_state text,
    candidate_business_zip text,
    application_created_date text,
    prescreen_created_date text,
    lead_start_date text,
    matched_live_variant text,
    matched_candidate_variant text,
    supporting_signals text,
    alternative_candidate_name text,
    alternative_candidate_score real
);

create index if not exists idx_loan_matches_status
    on {TABLE_NAME}(status);
"""


def _normalize_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_STATUSES else "new"


def _clean_note(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _match_key(row: pd.Series | sqlite3.Row | dict[str, object]) -> str:
    return stable_hash((row["live_record_key"], int(row["candidate_application_id"])))


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        """
        select 1
        from sqlite_master
        where type = 'table'
          and name = ?
        """,
        (name,),
    ).fetchone()
    return row is not None


def _column_value(row: pd.Series | dict[str, object], column: str) -> object:
    value = row.get(column) if hasattr(row, "get") else row[column]
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _legacy_payload_to_record(legacy_row: sqlite3.Row) -> dict[str, object]:
    payload = {}
    payload_json = legacy_row["payload_json"]
    if payload_json:
        payload = json.loads(payload_json)
    record: dict[str, object] = {}
    for column in TABLE_COLUMNS:
        if column in payload:
            record[column] = payload[column]
        else:
            record[column] = legacy_row[column] if column in legacy_row.keys() else None
    record["status"] = _normalize_status(record.get("status", legacy_row["status"]))
    record["status_note"] = str(record.get("status_note") or legacy_row["status_note"] or "").strip()
    record["first_seen_at"] = record.get("first_seen_at") or legacy_row["first_seen_at"]
    record["last_seen_at"] = record.get("last_seen_at") or legacy_row["last_seen_at"]
    record["quarter_first_seen"] = record.get("quarter_first_seen") or legacy_row["quarter_first_seen"]
    record["first_run_label"] = record.get("first_run_label") or legacy_row["first_run_label"]
    record["last_run_label"] = record.get("last_run_label") or legacy_row["last_run_label"]
    record["times_seen"] = int(record.get("times_seen") or legacy_row["times_seen"] or 1)
    record["match_key"] = record.get("match_key") or legacy_row["match_key"]
    record["live_record_key"] = record.get("live_record_key") or legacy_row["live_record_key"]
    record["candidate_application_id"] = int(
        record.get("candidate_application_id") or legacy_row["candidate_application_id"]
    )
    record["decision_band"] = record.get("decision_band") or legacy_row["decision_band"]
    record["match_score"] = float(record.get("match_score") or legacy_row["match_score"])
    return record


def _insert_record(connection: sqlite3.Connection, record: dict[str, object]) -> None:
    column_sql = ", ".join(TABLE_COLUMNS)
    placeholder_sql = ", ".join(["?"] * len(TABLE_COLUMNS))
    values = [record.get(column) for column in TABLE_COLUMNS]
    connection.execute(
        f"""
        insert or replace into {TABLE_NAME} ({column_sql})
        values ({placeholder_sql})
        """,
        values,
    )


def _migrate_legacy_schema(connection: sqlite3.Connection) -> None:
    has_legacy = any(
        _table_exists(connection, name)
        for name in ("match_records", "match_history", "runs", "__codex_probe")
    )
    connection.executescript(SCHEMA)
    if not has_legacy:
        return
    if _table_exists(connection, "match_records"):
        legacy_rows = connection.execute("select * from match_records").fetchall()
        for legacy_row in legacy_rows:
            _insert_record(connection, _legacy_payload_to_record(legacy_row))
    for table_name in ("match_records", "match_history", "runs", "__codex_probe"):
        if _table_exists(connection, table_name):
            connection.execute(f'drop table "{table_name}"')
    connection.commit()
    connection.execute("vacuum")


def _ensure_current_columns(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, TABLE_NAME):
        return
    existing_columns = {
        row["name"]
        for row in connection.execute(f"pragma table_info({TABLE_NAME})").fetchall()
    }
    column_definitions = {
        "application_created_date": "text",
        "billed_quarter": "text",
        "billed_amount": "real",
        "invoice_date": "text",
    }
    changed = False
    for column_name, definition in column_definitions.items():
        if column_name not in existing_columns:
            connection.execute(
                f"alter table {TABLE_NAME} add column {column_name} {definition}"
            )
            changed = True
    if changed:
        connection.commit()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    _migrate_legacy_schema(connection)
    _ensure_current_columns(connection)
    return connection


def _empty_queue_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=TABLE_COLUMNS)


def reset_history_db(history_db_path: Path) -> None:
    with _connect(history_db_path) as conn:
        conn.execute(f"delete from {TABLE_NAME}")
        conn.commit()
        conn.execute("vacuum")


def start_run(
    history_db_path: Path,
    run_label: str,
    quarter_label: str,
) -> int:
    with _connect(history_db_path):
        return 0


def complete_run(
    history_db_path: Path,
    run_id: int,
    *,
    live_row_count: int,
    duplicate_count: int,
    auto_accept_count: int,
    review_count: int,
    exception_count: int,
) -> None:
    with _connect(history_db_path):
        return None


def import_workbook_decisions(history_db_path: Path, workbook_path: Path) -> int:
    if not workbook_path.exists():
        return 0
    workbook = pd.ExcelFile(workbook_path)
    decision_sheets = [sheet for sheet in ("auto_accept", "review", "possible_matches") if sheet in workbook.sheet_names]
    if not decision_sheets:
        return 0
    decision_rows: list[tuple[str, str, str]] = []
    for sheet_name in decision_sheets:
        frame = pd.read_excel(workbook_path, sheet_name=sheet_name)
        if "match_key" not in frame.columns or "status" not in frame.columns:
            continue
        for _, row in frame.iterrows():
            status = _normalize_status(row.get("status"))
            if status == "new":
                continue
            decision_rows.append(
                (
                    str(row["match_key"]).strip(),
                    status,
                    _clean_note(row.get("status_note", "")),
                )
            )
    if not decision_rows:
        return 0
    updated_at = datetime.now(timezone.utc).isoformat()
    imported = 0
    with _connect(history_db_path) as conn:
        for match_key, status, note in decision_rows:
            cursor = conn.execute(
                f"""
                update {TABLE_NAME}
                set status = ?,
                    status_note = ?,
                    status_updated_at = ?
                where match_key = ?
                  and status != ?
                """,
                (status, note, updated_at, match_key, status),
            )
            imported += cursor.rowcount
        conn.commit()
    return imported


def annotate_matches(history_db_path: Path, matches: pd.DataFrame) -> pd.DataFrame:
    if matches.empty:
        return matches
    annotated = matches.copy()
    annotated["match_key"] = annotated.apply(_match_key, axis=1)
    with _connect(history_db_path) as conn:
        existing_rows = conn.execute(
            f"""
            select
                match_key,
                first_seen_at,
                last_seen_at,
                times_seen,
                status,
                status_note
            from {TABLE_NAME}
            """
        ).fetchall()
    existing_index = {row["match_key"]: row for row in existing_rows}
    annotated["first_seen_at"] = annotated["match_key"].map(
        lambda key: existing_index[key]["first_seen_at"] if key in existing_index else ""
    )
    annotated["last_seen_at"] = annotated["match_key"].map(
        lambda key: existing_index[key]["last_seen_at"] if key in existing_index else ""
    )
    annotated["times_seen"] = annotated["match_key"].map(
        lambda key: existing_index[key]["times_seen"] if key in existing_index else 0
    )
    annotated["is_new_match"] = annotated["match_key"].map(lambda key: key not in existing_index)
    annotated["status"] = annotated["match_key"].map(
        lambda key: existing_index[key]["status"] if key in existing_index else "new"
    )
    annotated["status_note"] = annotated["match_key"].map(
        lambda key: existing_index[key]["status_note"] if key in existing_index else ""
    )
    return annotated


def upsert_matches(
    history_db_path: Path,
    matches: pd.DataFrame,
    *,
    run_label: str,
    quarter_label: str,
) -> None:
    if matches.empty:
        return
    seen_at = datetime.now(timezone.utc).isoformat()
    with _connect(history_db_path) as conn:
        for _, row in matches.iterrows():
            match_key = row["match_key"]
            existing = conn.execute(
                f"""
                select
                    status,
                    status_note,
                    status_updated_at,
                    billed_quarter,
                    billed_amount,
                    invoice_date,
                    times_seen,
                    first_seen_at,
                    quarter_first_seen,
                    first_run_label
                from {TABLE_NAME}
                where match_key = ?
                """,
                (match_key,),
            ).fetchone()
            record = {column: _column_value(row, column) for column in TABLE_COLUMNS if column in row.index}
            record["match_key"] = match_key
            record["live_record_key"] = row["live_record_key"]
            record["candidate_application_id"] = int(row["candidate_application_id"])
            record["candidate_business_id"] = _column_value(row, "candidate_business_id")
            record["candidate_customer_id"] = _column_value(row, "candidate_customer_id")
            record["decision_band"] = row["decision_band"]
            record["status"] = _normalize_status(row.get("status", "new"))
            record["status_note"] = _clean_note(row.get("status_note", ""))
            record["match_score"] = float(row["match_score"])
            record["name_score"] = _column_value(row, "name_score")
            record["score_gap"] = _column_value(row, "score_gap")
            if existing is None:
                record["first_seen_at"] = seen_at
                record["last_seen_at"] = seen_at
                record["status_updated_at"] = None
                record["billed_quarter"] = None
                record["billed_amount"] = None
                record["invoice_date"] = None
                record["quarter_first_seen"] = quarter_label
                record["first_run_label"] = run_label
                record["last_run_label"] = run_label
                record["times_seen"] = 1
            else:
                record["first_seen_at"] = existing["first_seen_at"]
                record["last_seen_at"] = seen_at
                record["status_updated_at"] = existing["status_updated_at"]
                record["quarter_first_seen"] = existing["quarter_first_seen"]
                record["first_run_label"] = existing["first_run_label"]
                record["last_run_label"] = run_label
                record["times_seen"] = int(existing["times_seen"]) + 1
                record["status"] = existing["status"]
                record["status_note"] = existing["status_note"]
                record["billed_quarter"] = existing["billed_quarter"]
                record["billed_amount"] = existing["billed_amount"]
                record["invoice_date"] = existing["invoice_date"]
            _insert_record(conn, record)
        conn.commit()


def load_unresolved_queue(history_db_path: Path) -> pd.DataFrame:
    with _connect(history_db_path) as conn:
        rows = conn.execute(
            f"""
            select *
            from {TABLE_NAME}
            where status = 'new'
            order by
                case decision_band
                    when 'auto_accept' then 0
                    when 'review' then 1
                    when 'possible_match' then 2
                    else 9
                end,
                case candidate_status
                    when 'Approved' then 0
                    when 'SentForEducation' then 1
                    when 'InProgress' then 2
                    when 'Unassigned' then 3
                    when 'Denied' then 4
                    when 'Discarded' then 5
                    when 'Archived' then 6
                    else 9
                end,
                match_score desc,
                first_seen_at asc
            """
        ).fetchall()
    if not rows:
        return _empty_queue_frame()
    frame = pd.DataFrame([dict(row) for row in rows])
    for column in (
        "application_created_date",
        "primary_origination_date",
        "prescreen_created_date",
        "lead_start_date",
    ):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame.reset_index(drop=True)
