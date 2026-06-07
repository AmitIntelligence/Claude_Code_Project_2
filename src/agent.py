"""OIC Invoice Quality Agent — main monitoring loop.

Implements the run cycle described in instructions.md:
    load guardrails+config -> poll sources (read-only) -> parse -> validate
    missing required fields -> persist exceptions (CSV+SQL) -> heartbeat.

The agent is READ-ONLY against OIC/Cleo/staging and observe-and-report only
(instructions.md §5). It never mutates payloads or the production flow.

Usage:
    python src/agent.py            # run forever on the configured cadence
    python src/agent.py --once     # single run (for cron/orchestrators/CI)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import config
from exception_store import ExceptionStore
from models import InvoiceException, utcnow_iso
from sources import SourceUnavailable, build_sources
from validator import validate

ROOT = Path(__file__).resolve().parent.parent


def _logger(settings: dict[str, Any]) -> logging.Logger:
    log_dir = ROOT / settings["logging"]["dir"]
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("oic-invoice-quality-agent")
    if logger.handlers:
        return logger
    logger.setLevel(settings["logging"]["level"])
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_dir / f"agent_{time.strftime('%Y%m%d')}.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _enforce_read_only(settings: dict[str, Any]) -> None:
    """Hard guardrail: refuse to run if read_only has been turned off (§5.1)."""
    if not settings["agent"].get("read_only", True):
        raise SystemExit(
            "Refusing to start: agent.read_only is false. This agent must remain "
            "read-only (instructions.md §5.1)."
        )


class HeartbeatTracker:
    """Tracks per-source health across runs for the dashboard liveness tile."""

    def __init__(self, settings: dict[str, Any]):
        self.path = ROOT / settings["heartbeat"]["path"]
        self.alert_after = settings["heartbeat"]["degraded_alert_after_consecutive_runs"]
        self.consecutive_degraded: dict[str, int] = {}

    def write(self, run_id: str, source_health: dict[str, str],
              processed: int, exceptions: int, errors: int, cfg_hash: str) -> None:
        for name, status in source_health.items():
            if status == "DEGRADED":
                self.consecutive_degraded[name] = self.consecutive_degraded.get(name, 0) + 1
            else:
                self.consecutive_degraded[name] = 0
        alert = any(v >= self.alert_after for v in self.consecutive_degraded.values())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "agent": "oic-invoice-quality-agent",
            "last_run_id": run_id,
            "last_run_at": utcnow_iso(),
            "config_hash": cfg_hash,
            "records_processed": processed,
            "exceptions_found": exceptions,
            "errors": errors,
            "source_health": source_health,
            "consecutive_degraded": self.consecutive_degraded,
            "alert": alert,
        }, indent=2), encoding="utf-8")


def run_once(settings: dict[str, Any], required: dict[str, Any],
             store: ExceptionStore, heartbeat: HeartbeatTracker,
             logger: logging.Logger) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    cfg_hash = config.config_hash("config/settings.yaml", "config/required_fields.yaml")
    logger.info("Run %s starting (config_hash=%s)", run_id, cfg_hash)

    exceptions: list[InvoiceException] = []
    source_health: dict[str, str] = {}
    processed = 0

    for source in build_sources(settings):
        try:
            for invoice in source.fetch():
                processed += 1
                if not invoice.edi_standard:
                    logger.warning("Skipping doc with undetermined EDI standard: %s",
                                   invoice.locator())
                    continue
                try:
                    schema = config.resolve_schema(
                        required, invoice.edi_standard, invoice.customer_code)
                except KeyError as exc:
                    logger.error("Schema resolution failed: %s", exc)
                    continue
                findings = validate(invoice, schema)
                if findings:
                    exceptions.append(InvoiceException(
                        run_id=run_id,
                        detected_at=utcnow_iso(),
                        source=invoice.source.value,
                        integration_id=invoice.integration_id,
                        integration_name=invoice.integration_name,
                        integration_version=invoice.integration_version,
                        instance_id=invoice.instance_id,
                        document_id=invoice.document_id,
                        customer_code=invoice.customer_code,
                        customer_name=invoice.customer_name,
                        edi_standard=invoice.edi_standard,
                        document_type=invoice.document_type,
                        transmission_status=invoice.transmission_status,
                        findings=findings,
                    ))
            source_health[source.name] = "OK"
        except SourceUnavailable as exc:
            source_health[source.name] = "DEGRADED"
            logger.error("Source %s DEGRADED: %s", source.name, exc)
        except Exception as exc:  # noqa: BLE001 — never let one source crash the run
            source_health[source.name] = "DEGRADED"
            logger.error("Source %s unexpected error: %s", source.name, exc)

    persist_result = store.persist(exceptions, logger)
    errors = len(persist_result["errors"]) + sum(
        1 for v in source_health.values() if v == "DEGRADED")

    heartbeat.write(run_id, source_health, processed, len(exceptions), errors, cfg_hash)
    logger.info(
        "Run %s done: processed=%d exceptions=%d sinks=%s health=%s",
        run_id, processed, len(exceptions), persist_result, source_health,
    )
    return {
        "run_id": run_id, "processed": processed,
        "exceptions": len(exceptions), "health": source_health,
        "persist": persist_result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OIC Invoice Quality Agent")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args(argv)

    settings = config.load_settings()
    required = config.load_required_fields()
    _enforce_read_only(settings)
    logger = _logger(settings)
    logger.info("Loaded guardrails from %s", settings["agent"]["instructions_file"])

    store = ExceptionStore(settings)
    heartbeat = HeartbeatTracker(settings)

    if args.once:
        run_once(settings, required, store, heartbeat, logger)
        return 0

    interval = settings["schedule"]["poll_interval_seconds"]
    logger.info("Entering monitoring loop (interval=%ds). Ctrl-C to stop.", interval)
    while True:
        try:
            run_once(settings, required, store, heartbeat, logger)
        except Exception as exc:  # noqa: BLE001 — run-boundary safety net (§9)
            logger.exception("Run failed: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
