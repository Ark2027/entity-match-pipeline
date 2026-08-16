"""Pull candidate records out of a CRM database over an SSH tunnel.

The query below is written against an example schema. Adapt the table and
column names to your own; everything downstream depends only on the output
column aliases, not on the underlying names.

Two deliberate choices worth noting:

* Only columns the matcher actually consumes are selected. It is tempting to
  `select *` and filter later, but every extra column is data you have pulled
  out of a production system for no reason. Personal fields in particular
  should not leave the database unless something downstream needs them.
* The database password is written to the remote shell's stdin rather than
  interpolated into the command string. A command string is visible in the
  remote host's process list while it runs.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .settings import AppConfig

# paramiko is deliberately NOT imported here. normalize_candidate_frame is used
# by the fixture path, which needs no database at all, and a module-level import
# would drag the SSH dependency into a demo that advertises not needing it. CI
# caught exactly that, because it installs core dependencies only.


@dataclass(frozen=True)
class RemotePsqlResult:
    stdout: str
    stderr: str
    exit_code: int


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _validate_identifier(value: str, label: str) -> str:
    """Reject anything that is not a plain SQL identifier.

    The schema name is interpolated into the query because a schema cannot be a
    bind parameter. Whoever edits settings.json already controls the machine, so
    this is not the last line of defence, but interpolating an unchecked string
    into SQL is worth refusing on principle.
    """
    if not _IDENTIFIER.match(value or ""):
        raise ValueError(f"{label} must be a plain identifier, got {value!r}")
    return value


def _build_candidate_query(schema: str) -> str:
    """Candidate records eligible for matching.

    Every column here is consumed downstream. `lead.stage` and `lead.opened_at`
    are referenced in the filter and the coalesce but are deliberately not
    returned, since nothing reads them.
    """
    schema = _validate_identifier(schema, "db.schema")
    return f"""
select distinct on (l.lead_id)
    l.lead_id                as application_id,
    o.organization_id        as business_id,
    p.person_id              as customer_id,
    l.source_code            as candidate_source,
    s.display_name           as candidate_source_name,
    l.status                 as application_status,
    l.size_band              as loan_range,
    l.created_at             as application_created_date,
    coalesce(sc.created_at, l.opened_at, l.created_at) as lead_start_date,
    o.name                   as crm_business_name,
    p.first_name             as customer_first_name,
    p.last_name              as customer_last_name,
    loc.region               as business_state,
    loc.postal_code          as business_zip_code,
    sc.created_at            as prescreen_created_date,
    sc.first_name            as prescreen_first_name,
    sc.last_name             as prescreen_last_name,
    sc.region                as prescreen_state,
    sc.postal_code           as prescreen_zip_code,
    sc.org_postal_code       as prescreen_business_zip_code
from "{schema}".lead l
join "{schema}".organization o
    on o.lead_id = l.lead_id
left join "{schema}".source s
    on s.code = l.source_code
left join "{schema}".person p
    on p.person_id = o.person_id
left join "{schema}".location loc
    on loc.organization_id = o.organization_id
   and loc.location_type = 'BUSINESS'
left join "{schema}".screening sc
    on sc.lead_id = l.lead_id
where l.stage = 'SCREENED'
order by
    l.lead_id,
    loc.created_at desc nulls last,
    sc.created_at desc nulls last
"""


def _connect(config: AppConfig) -> Any:
    """Open an SSH connection, verifying the host key.

    `AutoAddPolicy` silently trusts whatever key the far end presents, which
    defeats the point of host verification. This loads the system known_hosts
    and refuses anything unrecognised.
    """
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "Reading candidates from a database needs the db extras: pip install -e '.[db]'"
        ) from exc

    key = paramiko.RSAKey.from_private_key_file(str(config.ssh.key_path))
    client = paramiko.SSHClient()
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    if known_hosts.exists():
        client.load_host_keys(str(known_hosts))
    else:
        client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(config.ssh.host, username=config.ssh.user, pkey=key, timeout=15)
    return client


def _run_remote_psql_copy(config: AppConfig, query: str) -> RemotePsqlResult:
    """Run the query remotely and stream the result back as CSV.

    The password is read from stdin by the remote shell rather than appearing
    in the command line, so it never shows up in `ps` on the remote host.
    """
    client = _connect(config)
    try:
        command = (
            "read -r PGPASSWORD; export PGPASSWORD; "
            f'psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -q -v ON_ERROR_STOP=1 '
            "<<'SQL'\n"
            "COPY (\n"
            f"{query}\n"
            ") TO STDOUT WITH CSV HEADER;\n"
            "SQL\n"
        )
        wrapper = (
            f'DB_HOST={_sh_quote(config.db.host)} '
            f'DB_USER={_sh_quote(config.db.user)} '
            f'DB_NAME={_sh_quote(config.db.database)} '
            f"sh -c {_sh_quote(command)}"
        )
        stdin, stdout, stderr = client.exec_command(wrapper)
        stdin.write(config.db.password + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8")
        error = stderr.read().decode("utf-8")
        exit_code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    return RemotePsqlResult(stdout=output, stderr=error, exit_code=exit_code)


def _sh_quote(value: str) -> str:
    """Single-quote a value for a POSIX shell."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def fetch_candidates(config: AppConfig) -> pd.DataFrame:
    query = _build_candidate_query(config.db.schema)
    result = _run_remote_psql_copy(config, query)
    if result.exit_code != 0:
        # Never include the query or the connection string in the error; both
        # can carry more detail than belongs in a log line.
        raise RuntimeError(f"Remote query failed with exit code {result.exit_code}: {result.stderr.strip()[:500]}")
    return normalize_candidate_frame(pd.read_csv(io.StringIO(result.stdout)))


def normalize_candidate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce dates and normalize the columns used as hard match filters.

    Split out from the fetch so it can be tested against a fixture without a
    database.
    """
    for column in ("application_created_date", "lead_start_date", "prescreen_created_date"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("candidate_source", "business_state", "prescreen_state"):
        if column in frame.columns:
            frame[column] = frame[column].fillna("").astype(str).str.upper().str.strip()
    return frame
