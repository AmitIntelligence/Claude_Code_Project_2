"""Persist exceptions to both sinks: CSV and SQL (instructions.md §3.5, §5.6).

Both sinks are append/upsert ONLY. The store never TRUNCATEs or DROPs. On
re-detection the existing row is updated (status, last_seen_at) so there are no
duplicates (§3.2 idempotency).
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any, Iterable

from models import EXCEPTION_COLUMNS, InvoiceException, utcnow_iso

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# CSV sink
# ---------------------------------------------------------------------------
class CsvSink:
    def __init__(self, settings: dict[str, Any]):
        self.path = ROOT / settings["sinks"]["csv"]["path"]
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        with open(self.path, newline="", encoding="utf-8") as fh:
            return {r["exception_id"]: r for r in csv.DictReader(fh)}

    def upsert(self, exceptions: Iterable[InvoiceException]) -> int:
        rows = self._load()
        count = 0
        for exc in exceptions:
            row = exc.to_row()
            existing = rows.get(row["exception_id"])
            if existing:
                row["first_seen_at"] = existing.get("first_seen_at", row["first_seen_at"])
                row["status"] = existing.get("status", "OPEN")  # preserve manual triage
            row["last_seen_at"] = utcnow_iso()
            rows[row["exception_id"]] = {k: str(row.get(k, "")) for k in EXCEPTION_COLUMNS}
            count += 1
        # Atomic-ish write via temp file then replace (never destructive in place).
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=EXCEPTION_COLUMNS)
            w.writeheader()
            for r in rows.values():
                w.writerow(r)
        os.replace(tmp, self.path)
        return count


# ---------------------------------------------------------------------------
# SQL sink (Azure SQL / SQL Server via pyodbc) — MERGE/upsert only
# ---------------------------------------------------------------------------
DDL = """
IF OBJECT_ID(N'{table}', N'U') IS NULL
CREATE TABLE {table} (
    exception_id         VARCHAR(64)  NOT NULL PRIMARY KEY,
    run_id               VARCHAR(64),
    detected_at          DATETIME2,
    source               VARCHAR(32),
    integration_id       VARCHAR(128),
    integration_name     NVARCHAR(256),
    integration_version  VARCHAR(32),
    instance_id          VARCHAR(128),
    document_id          VARCHAR(128),
    customer_code        VARCHAR(64),
    customer_name        NVARCHAR(256),
    edi_standard         VARCHAR(32),
    document_type        VARCHAR(16),
    missing_fields       NVARCHAR(2000),
    missing_field_count  INT,
    severity             VARCHAR(16),
    transmission_status  VARCHAR(32),
    status               VARCHAR(16),
    first_seen_at        DATETIME2,
    last_seen_at         DATETIME2
);
"""

MERGE = """
MERGE {table} AS tgt
USING (SELECT ? AS exception_id) AS src
ON (tgt.exception_id = src.exception_id)
WHEN MATCHED THEN UPDATE SET
    last_seen_at = ?, missing_fields = ?, missing_field_count = ?,
    severity = ?, transmission_status = ?, detected_at = ?, run_id = ?
WHEN NOT MATCHED THEN INSERT
    (exception_id, run_id, detected_at, source, integration_id, integration_name,
     integration_version, instance_id, document_id, customer_code, customer_name,
     edi_standard, document_type, missing_fields, missing_field_count, severity,
     transmission_status, status, first_seen_at, last_seen_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
"""


class SqlSink:
    def __init__(self, settings: dict[str, Any]):
        cfg = settings["sinks"]["sql"]
        self.table = cfg["table"]
        self.conn_str = os.environ.get(cfg["conn_str_env"], "")

    def available(self) -> bool:
        if not self.conn_str:
            return False
        try:
            import pyodbc  # noqa: F401
            return True
        except Exception:
            return False

    def upsert(self, exceptions: Iterable[InvoiceException]) -> int:
        import pyodbc
        count = 0
        with pyodbc.connect(self.conn_str, autocommit=False) as conn:
            cur = conn.cursor()
            cur.execute(DDL.format(table=self.table))
            for exc in exceptions:
                r = exc.to_row()
                cur.execute(
                    MERGE.format(table=self.table),
                    r["exception_id"],
                    # UPDATE params
                    r["last_seen_at"], r["missing_fields"], r["missing_field_count"],
                    r["severity"], r["transmission_status"], r["detected_at"], r["run_id"],
                    # INSERT params
                    r["exception_id"], r["run_id"], r["detected_at"], r["source"],
                    r["integration_id"], r["integration_name"], r["integration_version"],
                    r["instance_id"], r["document_id"], r["customer_code"], r["customer_name"],
                    r["edi_standard"], r["document_type"], r["missing_fields"],
                    r["missing_field_count"], r["severity"], r["transmission_status"],
                    r["status"], r["first_seen_at"], r["last_seen_at"],
                )
                count += 1
            conn.commit()
        return count


class ExceptionStore:
    """Writes to all enabled sinks; one sink's failure never drops the other."""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.csv = CsvSink(settings) if settings["sinks"]["csv"]["enabled"] else None
        self.sql = SqlSink(settings) if settings["sinks"]["sql"]["enabled"] else None

    def persist(self, exceptions: list[InvoiceException], logger) -> dict[str, Any]:
        result = {"csv": None, "sql": None, "errors": []}
        if self.csv:
            try:
                result["csv"] = self.csv.upsert(exceptions)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"csv:{exc}")
                logger.error("CSV sink write failed: %s", exc)
        if self.sql:
            if self.sql.available():
                try:
                    result["sql"] = self.sql.upsert(exceptions)
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"sql:{exc}")
                    logger.error("SQL sink write failed: %s", exc)
            else:
                logger.warning("SQL sink enabled but unavailable (no conn str / pyodbc). Skipping.")
        return result
