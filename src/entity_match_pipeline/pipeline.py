from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .history import (
    annotate_matches,
    complete_run,
    import_workbook_decisions,
    load_unresolved_queue,
    reset_history_db,
    start_run,
    upsert_matches,
)
from .live_data import discover_live_quarters, load_live_originations
from .matching import collapse_duplicate_live_rows, match_live_rows, prepare_candidate_frame
from .notifications import (
    queue_pending_notification,
    send_windows_notification,
    show_pending_notification_if_present,
)
from .reporting import build_output_path, write_report
from .settings import AppConfig, QuarterWindow, latest_due_quarter, load_config, previous_calendar_quarter, quarter_from_label


def load_candidates(config: AppConfig) -> pd.DataFrame:
    """Read candidates from a CSV if one is configured, otherwise from the CRM.

    The CSV path exists so the pipeline can be demonstrated end to end against
    fixtures with no database, no SSH access, and without installing paramiko or
    psycopg. The database import is deliberately deferred for the same reason.
    """
    if config.candidates_csv is not None:
        from .db_extract import normalize_candidate_frame

        return normalize_candidate_frame(pd.read_csv(config.candidates_csv))

    from .db_extract import fetch_candidates

    return fetch_candidates(config)


def _build_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "matcher_run_current.log"
    logger = logging.getLogger("entity_match_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger, log_path


def _run_state_path(project_root: Path) -> Path:
    return project_root / "logs" / "run_state.json"


def _load_run_state(project_root: Path) -> dict[str, object]:
    path = _run_state_path(project_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_run_state(
    project_root: Path,
    *,
    quarter_label: str,
    run_label: str,
    workbook_path: Path,
) -> None:
    path = _run_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_completed_quarter": quarter_label,
        "last_completed_run_label": run_label,
        "last_completed_at": datetime.now(timezone.utc).isoformat(),
        "workbook_path": str(workbook_path),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _backup_history_db(config: AppConfig) -> Path | None:
    if not config.history_db_path.exists():
        return None
    backup_dir = config.project_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{config.history_db_path.stem}_backup_{timestamp}{config.history_db_path.suffix}"
    shutil.copy2(config.history_db_path, backup_path)
    return backup_path


def _resolve_quarters(
    config: AppConfig,
    *,
    target_quarter: QuarterWindow,
    backfill_history: bool,
) -> list[QuarterWindow]:
    if not backfill_history:
        return [target_quarter]
    discovered = discover_live_quarters(config, max_quarter=target_quarter)
    return [quarter for quarter in discovered if quarter.label <= target_quarter.label]


def _run_single_quarter(
    *,
    config: AppConfig,
    quarter: QuarterWindow,
    candidates: pd.DataFrame,
    workbook_path: Path,
    logger: logging.Logger,
    log_path: Path,
    imported_decisions: int,
    run_label: str,
    send_notification: bool,
    queue_logon_notification: bool,
) -> dict[str, object]:
    run_id = start_run(config.history_db_path, run_label, quarter.label)
    logger.info("Starting match run for %s", quarter.label)
    live_result = load_live_originations(config, quarter)
    logger.info("Loaded %s live rows for %s", len(live_result.rows), quarter.label)
    collapsed_live, duplicates = collapse_duplicate_live_rows(live_result.rows)
    logger.info(
        "Collapsed %s live rows into %s review items",
        len(live_result.rows),
        len(collapsed_live),
    )
    matches, match_exceptions, not_surfaced = match_live_rows(collapsed_live, candidates, config)
    annotated_matches = annotate_matches(config.history_db_path, matches)
    current_run_auto_accept = (
        annotated_matches.loc[annotated_matches["decision_band"] == "auto_accept"].copy()
        if not annotated_matches.empty
        else pd.DataFrame()
    )
    current_run_review = (
        annotated_matches.loc[annotated_matches["decision_band"] == "review"].copy()
        if not annotated_matches.empty
        else pd.DataFrame()
    )
    current_run_possible = (
        annotated_matches.loc[annotated_matches["decision_band"] == "possible_match"].copy()
        if not annotated_matches.empty
        else pd.DataFrame()
    )
    upsert_matches(
        config.history_db_path,
        annotated_matches,
        run_label=run_label,
        quarter_label=quarter.label,
    )
    unresolved_queue = load_unresolved_queue(config.history_db_path)
    auto_accept = unresolved_queue.loc[
        unresolved_queue["decision_band"] == "auto_accept"
    ].copy()
    reviewable = unresolved_queue.loc[
        unresolved_queue["decision_band"].isin(["review", "possible_match"])
    ].copy()
    review_overflow = pd.DataFrame()
    review = reviewable.loc[reviewable["decision_band"] == "review"].copy()
    possible_matches = reviewable.loc[
        reviewable["decision_band"] == "possible_match"
    ].copy()
    exception_frames = [frame for frame in (live_result.exceptions, match_exceptions) if not frame.empty]
    exceptions = pd.concat(exception_frames, ignore_index=True) if exception_frames else pd.DataFrame()
    summary = {
        "quarter": quarter.label,
        "quarter_start": quarter.start.isoformat(),
        "quarter_end": quarter.end.isoformat(),
        "imported_status_updates": imported_decisions,
        "live_rows_in_quarter": len(live_result.rows),
        "collapsed_review_items": len(collapsed_live),
        "duplicates_collapsed": int(duplicates["duplicate_count"].sum() - len(duplicates)) if not duplicates.empty else 0,
        "crm_candidates": len(candidates),
        "current_run_auto_accept_matches": len(current_run_auto_accept),
        "current_run_review_matches": len(current_run_review),
        "current_run_possible_matches": len(current_run_possible),
        "current_queue_auto_accept": len(auto_accept),
        "current_queue_review": len(review),
        "current_queue_possible_matches": len(possible_matches),
        "current_queue_review_overflow": len(review_overflow),
        "exceptions": len(exceptions),
        "not_surfaced": len(not_surfaced),
        "run_label": run_label,
        "log_path": str(log_path),
    }
    if config.workbook_enabled:
        write_report(
            workbook_path,
            summary=summary,
            auto_accept=auto_accept,
            review=review,
            possible_matches=possible_matches,
            review_overflow=review_overflow,
            exceptions=exceptions,
            duplicates=duplicates,
            not_surfaced=not_surfaced,
        )
        logger.info("Wrote workbook to %s", workbook_path)
    else:
        logger.info("Workbook output disabled; review happens downstream.")
    complete_run(
        config.history_db_path,
        run_id,
        live_row_count=len(live_result.rows),
        duplicate_count=len(duplicates),
        auto_accept_count=len(current_run_auto_accept),
        review_count=len(current_run_review) + len(current_run_possible),
        exception_count=len(exceptions),
    )
    _save_run_state(
        config.project_root,
        quarter_label=quarter.label,
        run_label=run_label,
        workbook_path=workbook_path,
    )
    notification_title = "Quarterly match review is ready"
    queue_size = len(auto_accept) + len(review) + len(possible_matches)
    if config.workbook_enabled:
        notification_message = (
            f"{quarter.label}: {queue_size} unresolved matches in the current queue. "
            f"File saved to {workbook_path}"
        )
    else:
        notification_message = (
            f"{quarter.label}: {queue_size} unresolved matches in the current queue. "
            "Review them in the configured downstream view."
        )
    if queue_logon_notification:
        queue_pending_notification(
            config.project_root,
            title=notification_title,
            message=notification_message,
        )
    if send_notification:
        send_windows_notification(notification_title, notification_message)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to settings.json")
    parser.add_argument("--quarter", help="Quarter label like 2025Q4. Defaults to the previous calendar quarter.")
    parser.add_argument("--backfill-history", action="store_true", help="Run all discovered live-data quarters up to the target quarter.")
    parser.add_argument("--rebuild-history", action="store_true", help="Reset the local SQLite history table before running.")
    parser.add_argument("--disable-notifications", action="store_true")
    parser.add_argument("--queue-logon-notification", action="store_true", help="Write a pending notification for the logon reminder task.")
    parser.add_argument("--show-pending-notification-only", action="store_true", help="Display a pending notification if one exists, then exit.")
    parser.add_argument("--deliver-pending-notification", action="store_true", help="Display any queued notification after work completes.")
    parser.add_argument("--ensure-due-quarter-run", action="store_true", help="If the due quarterly run was missed, run it automatically now.")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    if args.show_pending_notification_only:
        show_pending_notification_if_present(config.project_root)
        return 0
    today = datetime.now().date()
    run_state = _load_run_state(config.project_root)
    target_quarter = quarter_from_label(args.quarter) if args.quarter else previous_calendar_quarter(today)
    if args.ensure_due_quarter_run:
        due_quarter = latest_due_quarter(today)
        if run_state.get("last_completed_quarter") != due_quarter.label:
            target_quarter = due_quarter
        elif args.deliver_pending_notification:
            show_pending_notification_if_present(config.project_root)
            return 0
        else:
            return 0
    workbook_path = build_output_path(config.output_dir)
    logger, log_path = _build_logger(config.project_root / "logs")

    backup_path = None
    if args.rebuild_history:
        backup_path = _backup_history_db(config)
        reset_history_db(config.history_db_path)
        logger.info("Reset local match history database at %s", config.history_db_path)
        if backup_path:
            logger.info("Backed up prior local history DB to %s", backup_path)

    imported_decisions = 0
    if not args.rebuild_history:
        imported_decisions = import_workbook_decisions(config.history_db_path, workbook_path)
        logger.info("Imported %s status updates from the current workbook", imported_decisions)
    else:
        logger.info("Skipped importing workbook decisions because history was rebuilt")

    quarters = _resolve_quarters(
        config,
        target_quarter=target_quarter,
        backfill_history=args.backfill_history,
    )
    if not quarters:
        logger.warning("No live-data quarters were discovered up to %s", target_quarter.label)
        return 0

    logger.info("Loaded quarter plan: %s", ", ".join(quarter.label for quarter in quarters))
    candidates = prepare_candidate_frame(load_candidates(config))
    logger.info("Loaded %s CRM candidates", len(candidates))

    base_run_label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for index, quarter in enumerate(quarters):
        run_label = f"{base_run_label}_{quarter.label}"
        is_final = index == len(quarters) - 1
        summary = _run_single_quarter(
            config=config,
            quarter=quarter,
            candidates=candidates,
            workbook_path=workbook_path,
            logger=logger,
            log_path=log_path,
            imported_decisions=imported_decisions if index == 0 else 0,
            run_label=run_label,
            send_notification=(config.notifications.enabled and not args.disable_notifications and is_final),
            queue_logon_notification=(args.queue_logon_notification and is_final),
        )
        if args.backfill_history and not is_final:
            logger.info(
                "Backfill progress %s/%s complete for %s (%s surfaced, %s exceptions)",
                index + 1,
                len(quarters),
                quarter.label,
                summary["current_run_auto_accept_matches"] + summary["current_run_review_matches"] + summary["current_run_possible_matches"],
                summary["exceptions"],
            )
    if args.deliver_pending_notification:
        show_pending_notification_if_present(config.project_root)
    return 0
