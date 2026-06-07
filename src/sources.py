"""Read-only payload sources (instructions.md §1.1, §5.1).

Three sources, all strictly read/list/GET only — the agent never POSTs, PUTs,
PATCHes, DELETEs, moves, or deletes anything in OIC, Cleo, or the staging zone.

Each source yields ``NormalizedInvoice`` objects. Network/IO failures are
raised as ``SourceUnavailable`` so the agent can mark the source DEGRADED and
continue (§3.8, §9).
"""
from __future__ import annotations

import glob
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import config
import parsers
from models import NormalizedInvoice, Source

try:  # requests is optional at import time so the module loads without network deps
    import requests
except Exception:  # pragma: no cover
    requests = None


class SourceUnavailable(Exception):
    """Raised when a source cannot be reached; the agent degrades gracefully."""


# Methods this agent is permitted to use. Enforced in _http (§5.1).
_READ_ONLY_METHODS = {"GET", "HEAD"}


def _http(method: str, url: str, settings: dict[str, Any], **kwargs) -> Any:
    """Perform a guarded, read-only HTTP request with backoff on 429/5xx."""
    if method.upper() not in _READ_ONLY_METHODS:
        raise PermissionError(
            f"Guardrail violation: {method} is not allowed. Agent is read-only "
            f"(instructions.md §5.1)."
        )
    if requests is None:
        raise SourceUnavailable("'requests' library not installed")

    bo = settings["schedule"]["backoff"]
    delay = bo["base_seconds"]
    last_err: Exception | None = None
    for attempt in range(bo["max_retries"] + 1):
        try:
            resp = requests.request(method, url, timeout=30, **kwargs)
            if resp.status_code in (429,) or resp.status_code >= 500:
                raise SourceUnavailable(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 — backoff on any transient failure
            last_err = exc
            if attempt >= bo["max_retries"]:
                break
            time.sleep(min(delay, bo["max_seconds"]))
            delay *= 2
    raise SourceUnavailable(str(last_err))


def _lookback_cutoff(settings: dict[str, Any]) -> datetime:
    minutes = settings["schedule"]["lookback_minutes"]
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Oracle Fusion Cloud Financials — Receivables (AR) invoices : SYSTEM OF ORIGIN
# Read-only. Used both as (a) an early-detection source for ERP-origin data gaps
# and (b) the reference index for root-cause attribution of downstream defects.
# ---------------------------------------------------------------------------
class FusionERPSource:
    name = "fusion_erp"

    def __init__(self, settings: dict[str, Any]):
        self.s = settings
        self.cfg = settings["sources"]["fusion_erp"]

    def _headers(self) -> dict[str, str]:
        token = config.env("FUSION_BEARER_TOKEN")
        if token:
            return {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        import base64
        user, pwd = config.env("FUSION_USERNAME"), config.env("FUSION_PASSWORD")
        if not user:
            raise SourceUnavailable("No Fusion credentials (FUSION_BEARER_TOKEN or FUSION_USERNAME)")
        basic = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        return {"Authorization": f"Basic {basic}", "Accept": "application/json"}

    def fetch(self) -> Iterable[NormalizedInvoice]:
        base = config.env(self.cfg["base_url_env"])
        if not base:
            raise SourceUnavailable("FUSION_BASE_URL not set")
        url = base.rstrip("/") + self.cfg["invoices_path"]
        # Read-only query: recent AR invoices, expand lines child resource.
        params = {
            "limit": self.cfg["page_size"],
            "onlyData": "true",
            "expand": self.cfg.get("expand", "receivablesInvoiceLines"),
            "orderBy": "TransactionDate:desc",
        }
        resp = _http("GET", url, self.s, headers=self._headers(), params=params)
        for rec in resp.json().get("items", []):
            payload = parsers.parse_fusion_ar_invoice(rec)
            std = self.cfg.get("default_edi_standard", "x12_810")
            yield NormalizedInvoice(
                source=Source.FUSION_ERP,
                integration_id=str(rec.get("BusinessUnit", "FUSION-AR")),
                integration_name="FUSION_AR_INVOICE",
                instance_id=str(rec.get("CustomerTransactionId", rec.get("TransactionNumber", ""))),
                document_id=str(payload["header"].get("invoice_number") or ""),
                customer_code=str(rec.get("BillToCustomerAccountNumber",
                                          rec.get("BillToCustomerNumber", ""))),
                customer_name=str(rec.get("BillToCustomerName", "")),
                edi_standard=std,
                document_type="810" if std == "x12_810" else "INVOIC",
                transmission_status="ERP_SOURCE",
                payload=payload,
            )


# ---------------------------------------------------------------------------
# OIC Monitoring/Audit REST API
# ---------------------------------------------------------------------------
class OICSource:
    name = "oic_api"

    def __init__(self, settings: dict[str, Any]):
        self.s = settings
        self.cfg = settings["sources"]["oic_api"]

    def _token(self) -> dict[str, str]:
        """Resolve an auth header. OAuth client-credentials preferred."""
        auth_type = config.env("OIC_AUTH_TYPE", "oauth").lower()
        if auth_type == "basic":
            import base64
            user = config.env("OIC_USERNAME")
            pwd = config.env("OIC_PASSWORD")
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        # OAuth client credentials
        token_url = config.env("OIC_OAUTH_TOKEN_URL")
        resp = _http(
            "GET", token_url, self.s  # token endpoints are typically POST; for
        ) if False else None          # real deployments inject a token via env.
        bearer = config.env("OIC_BEARER_TOKEN")
        if not bearer:
            raise SourceUnavailable(
                "No OIC_BEARER_TOKEN provided. Inject an OAuth token via your "
                "secret store (the agent does not POST to token endpoints)."
            )
        return {"Authorization": f"Bearer {bearer}"}

    def fetch(self) -> Iterable[NormalizedInvoice]:
        base = config.env(self.cfg["base_url_env"])
        if not base:
            raise SourceUnavailable("OIC_BASE_URL not set")
        headers = self._token()
        cutoff = _lookback_cutoff(self.s)
        # Filter to outbound invoice integrations + recent window.
        params = {
            "limit": self.cfg["page_size"],
            "q": "{timewindow:'1h'}",
        }
        url = base.rstrip("/") + self.cfg["instances_path"]
        resp = _http("GET", url, self.s, headers=headers, params=params)
        data = resp.json()
        for inst in data.get("items", []):
            if not _matches_scope(inst.get("integrationName", ""), self.s):
                continue
            # Pull the audit record/payload for this instance (read-only).
            payload, std = self._fetch_payload(base, headers, inst)
            yield NormalizedInvoice(
                source=Source.OIC_API,
                integration_id=str(inst.get("integrationId", inst.get("id", ""))),
                integration_name=inst.get("integrationName", ""),
                integration_version=str(inst.get("integrationVersion", "")),
                instance_id=str(inst.get("id", "")),
                document_id=str(inst.get("primaryValue", "")),
                customer_code=inst.get("customerCode", ""),
                customer_name=inst.get("customerName", ""),
                edi_standard=std,
                document_type="810" if std == "x12_810" else "INVOIC",
                transmission_status=inst.get("status", ""),
                payload=payload,
            )

    def _fetch_payload(self, base, headers, inst) -> tuple[dict[str, Any], str]:
        audit_url = base.rstrip("/") + self.cfg["audit_path"]
        try:
            resp = _http(
                "GET", audit_url, self.s, headers=headers,
                params={"q": f"{{instanceId:'{inst.get('id')}'}}"},
            )
            body = resp.json()
            raw = body.get("payload") or body.get("message") or ""
            if isinstance(raw, dict):
                return parsers.parse_structured(raw), inst.get("ediStandard", "")
            std, payload = parsers.parse_raw(raw)
            return payload, std
        except SourceUnavailable:
            # No payload retrievable -> empty payload validates as all-missing,
            # which is itself a flagged exception. Fail safe.
            return {}, inst.get("ediStandard", "")


# ---------------------------------------------------------------------------
# File staging directory (drop zone OIC writes before Cleo collects)
# ---------------------------------------------------------------------------
class StagingSource:
    name = "staging_files"

    def __init__(self, settings: dict[str, Any]):
        self.s = settings
        self.cfg = settings["sources"]["staging_files"]

    def fetch(self) -> Iterable[NormalizedInvoice]:
        directory = config.env(self.cfg["directory_env"])
        if not directory or not os.path.isdir(directory):
            raise SourceUnavailable(f"Staging dir not accessible: {directory!r}")

        max_age = timedelta(minutes=self.cfg["max_age_minutes"])
        now = datetime.now(timezone.utc)
        seen = 0
        for pattern in self.cfg["glob_patterns"]:
            for path in glob.glob(os.path.join(directory, "**", pattern), recursive=True):
                if seen >= self.s["schedule"]["max_records_per_run"]:
                    return
                p = Path(path)
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                if now - mtime > max_age:
                    continue
                # READ ONLY — never move/delete the staged file.
                raw = p.read_text(encoding="utf-8", errors="replace")
                std, payload = parsers.parse_raw(raw)
                seen += 1
                yield NormalizedInvoice(
                    source=Source.STAGING_FILE,
                    integration_id=p.stem,
                    integration_name=p.parent.name,
                    instance_id=p.name,
                    document_id=p.stem,
                    edi_standard=std,
                    document_type="810" if std == "x12_810" else "INVOIC",
                    transmission_status="STAGED",
                    payload=payload,
                )


# ---------------------------------------------------------------------------
# Cleo (Cleo Integration Cloud / VLTrader / Harmony)
# ---------------------------------------------------------------------------
class CleoSource:
    name = "cleo_api"

    def __init__(self, settings: dict[str, Any]):
        self.s = settings
        self.cfg = settings["sources"]["cleo_api"]

    def _headers(self) -> dict[str, str]:
        token = config.env("CLEO_API_TOKEN")
        if token:
            return {"Authorization": f"Bearer {token}"}
        import base64
        user, pwd = config.env("CLEO_USERNAME"), config.env("CLEO_PASSWORD")
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}

    def fetch(self) -> Iterable[NormalizedInvoice]:
        base = config.env(self.cfg["base_url_env"])
        if not base:
            raise SourceUnavailable("CLEO_BASE_URL not set")
        url = base.rstrip("/") + self.cfg["transmissions_path"]
        resp = _http(
            "GET", url, self.s, headers=self._headers(),
            params={"limit": self.cfg["page_size"], "type": "INVOIC,810"},
        )
        for item in resp.json().get("items", []):
            raw = item.get("documentContent", "")
            std, payload = parsers.parse_raw(raw) if isinstance(raw, str) else (
                item.get("ediStandard", ""), parsers.parse_structured(raw)
            )
            yield NormalizedInvoice(
                source=Source.CLEO_API,
                integration_id=item.get("sourceIntegration", item.get("flowId", "")),
                integration_name=item.get("flowName", ""),
                instance_id=str(item.get("transferId", "")),
                document_id=item.get("documentId", ""),
                customer_code=item.get("partnerCode", ""),
                customer_name=item.get("partnerName", ""),
                edi_standard=std,
                document_type="810" if std == "x12_810" else "INVOIC",
                transmission_status=item.get("status", ""),
                payload=payload,
            )


def _matches_scope(integration_name: str, settings: dict[str, Any]) -> bool:
    import fnmatch
    name = (integration_name or "").upper()
    return any(
        fnmatch.fnmatch(name, pat.upper())
        for pat in settings["scope"]["integration_name_patterns"]
    )


def build_sources(settings: dict[str, Any]) -> list[Any]:
    """Instantiate the enabled DOWNSTREAM sources (OIC / staging / Cleo).

    Fusion ERP is handled separately (see build_fusion_source) because it is the
    system of origin: it serves as the reference index for root-cause attribution
    and is validated first for early ERP-origin detection.
    """
    out = []
    src_cfg = settings["sources"]
    if src_cfg["oic_api"]["enabled"]:
        out.append(OICSource(settings))
    if src_cfg["staging_files"]["enabled"]:
        out.append(StagingSource(settings))
    if src_cfg["cleo_api"]["enabled"]:
        out.append(CleoSource(settings))
    return out


def build_fusion_source(settings: dict[str, Any]) -> "FusionERPSource | None":
    """Instantiate the Fusion ERP source if enabled, else None."""
    if settings["sources"].get("fusion_erp", {}).get("enabled"):
        return FusionERPSource(settings)
    return None
