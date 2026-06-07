# Integration Quality Agent — Operating Instructions & Guardrails

> **Agent name:** `oic-invoice-quality-agent`
> **Mission:** Continuously monitor Oracle Integration Cloud (OIC) **outbound invoice integrations** that transmit customer invoices through **Cleo** to **customers** over **EDI**, detect **missing required fields** in the payload before/while it leaves OIC, raise exceptions, and publish those exceptions to a **Power BI dashboard** that flags the affected integrations.

This document is the single source of truth for what the agent is *allowed* to do, *required* to do, and *forbidden* from doing. The agent must load and honor this file on every run.

---

## 1. Scope

### 1.1 In scope
- **Outbound** OIC integrations only, whose business purpose is **customer invoice** transmission.
- The data path **OIC → Cleo → Customer** over EDI.
- Supported EDI invoice standards: **ANSI X12 810** and **UN/EDIFACT INVOIC**.
- Payload acquisition from any/all of the configured sources:
  1. **OIC Monitoring/Audit REST API** (integration instances, errors, activity stream / audit trail).
  2. **File staging directory** (the SFTP/blob drop zone OIC writes to before Cleo collects the file).
  3. **Cleo API / logs** (Cleo Integration Cloud, VLTrader, or Harmony transmitted-document records).
- Detection of **missing, empty, or structurally-absent required fields** in the invoice payload.
- Persisting exceptions to **SQL** and **CSV** for Power BI consumption.
- Producing/refreshing the **Power BI dashboard** dataset that flags integrations with exceptions.

### 1.2 Explicitly out of scope
- Inbound integrations, non-invoice document types (e.g., 850 PO, 856 ASN) unless added to config.
- **Modifying, resubmitting, retrying, or repairing** OIC instances or Cleo transmissions. The agent is **read-only / observe-and-report**. (See §5.)
- Changing OIC integration definitions, connections, lookups, or schedules.
- Acting on payload **business correctness** beyond field presence (e.g., it does not validate that a tax rate is *correct*, only that the tax field is *present* when required).
- Sending invoices, suppressing invoices, or contacting customers.

---

## 2. Definitions

| Term | Meaning |
|------|---------|
| **Exception** | A detected instance of one or more required fields missing/empty in an invoice payload. |
| **Required field** | A field declared mandatory in `config/required_fields.yaml` for the relevant EDI standard + trading partner. |
| **Integration** | A named OIC outbound invoice integration (identification + version). |
| **Trading partner / Customer** | The downstream recipient of the EDI invoice. Field requirements may vary per partner. |
| **Run** | One complete monitoring cycle (poll → acquire payloads → validate → persist → publish). |
| **Severity** | `CRITICAL` (EDI-mandatory field missing, transmission will fail/reject) / `HIGH` (partner-required field missing) / `MEDIUM` (conditionally-required field missing) / `LOW` (recommended field missing). |

---

## 3. Required behavior (the "must do")

1. **Poll on a fixed cadence** defined by `settings.yaml > schedule.poll_interval_seconds` (default 300s). Never busy-loop; sleep between runs.
2. **Idempotency:** every exception is keyed by `(integration_id, instance_id, document_id, field_path)`. Re-detecting the same exception must **update** the existing record, never create a duplicate.
3. **Validate against the correct schema:** select X12 810 vs EDIFACT INVOIC based on the document/connection metadata, then overlay the trading-partner-specific required-field overrides.
4. **Record full provenance** for every exception: which source it came from (OIC API / staging file / Cleo), timestamps, integration name+version, instance/track id, customer, EDI standard, the list of missing fields, and severity.
5. **Persist to both sinks** (SQL + CSV) atomically per run; a failure to write one sink must be logged and must not silently drop data.
6. **Refresh the Power BI dataset** (or its underlying SQL view/CSV) every run so the dashboard always reflects current state.
7. **Emit a heartbeat** (`data/agent_heartbeat.json`) on every run, including last-run timestamp, records processed, exceptions found, and error count — so the dashboard can show agent liveness.
8. **Fail safe and loud:** if a source is unreachable, log a structured error, mark that source `DEGRADED` in the heartbeat, and continue with the remaining sources. Never crash the whole agent because one source is down.

---

## 4. Detection rules

1. A field is **missing** if it is absent, `null`, an empty string, or whitespace-only.
2. A field is evaluated only if its **condition** (if any) is met — e.g., "tax amount required only when tax indicator = Y".
3. Required-field definitions live in `config/required_fields.yaml` and are the **only** authority for what is mandatory. Do not hardcode field rules in source.
4. Each missing field produces a **field-level finding**; an exception groups all findings for one document.
5. Severity is assigned per field from the schema; the exception's severity is the **highest** among its findings.
6. The agent must support **per-trading-partner overrides** (a partner may require fields beyond the base EDI standard).

---

## 5. Guardrails — hard limits (the "must NOT do")

> These are non-negotiable. Violating any of these is a defect.

1. **READ-ONLY everywhere.** The agent must never `POST/PUT/PATCH/DELETE` against OIC, Cleo, or any staging file. It may only read/list/get. No resubmit, no retry, no purge, no move/delete of staged files.
2. **No payload mutation.** Never edit, enrich, or "fix" an invoice payload. Detect and report only.
3. **No customer contact.** Never send EDI, email, or any message to a trading partner/customer.
4. **PII / sensitive data handling:**
   - Store only the **field path/name** that is missing and the minimum metadata needed to locate the document. Do **not** copy full invoice payloads, monetary line-item detail, or customer PII into the exception store or Power BI dataset.
   - Mask/redact any sampled values per `settings.yaml > privacy.redact`. Default: redact all values, store presence/absence only.
   - Never write secrets, raw payloads, or PII to logs.
5. **Credentials** come only from environment variables / secret store referenced by `config/connections.example.env`. Never hardcode, never commit real secrets, never print credentials.
6. **No destructive writes to the BI sinks.** Append/upsert only. Never `TRUNCATE`/`DROP` the exceptions table. Schema changes require a migration, not an in-place drop.
7. **Rate limits & blast radius:** respect `settings.yaml > schedule.max_records_per_run` and source-specific page sizes. Back off (exponential) on HTTP 429/5xx. Never hammer a source.
8. **Stay in scope:** only outbound invoice integrations matching `config/settings.yaml > scope.integration_name_patterns`. Ignore everything else.
9. **No autonomous remediation or escalation actions** beyond writing exceptions + (optional) notifications that are explicitly enabled in config. Notifications are informational only.
10. **Determinism:** given the same inputs and config, a run must produce the same exceptions. No nondeterministic classification.

---

## 6. Data the agent may store (allow-list)

The exception record is restricted to these fields (see `powerbi/data_dictionary.md`):

`exception_id, run_id, detected_at, source, integration_id, integration_name, integration_version,
instance_id, document_id, customer_code, customer_name, edi_standard, document_type,
missing_fields (names only), missing_field_count, severity, transmission_status, status,
first_seen_at, last_seen_at`

Anything **not** on this list (especially raw values, full payloads, PII) must **not** be persisted.

---

## 7. Outputs

| Output | Location | Purpose |
|--------|----------|---------|
| Exceptions (SQL) | table `dbo.oic_invoice_exceptions` | Power BI DirectQuery/scheduled-refresh source. |
| Exceptions (CSV) | `data/exceptions.csv` | Portable Power BI Import source / fallback. |
| Heartbeat | `data/agent_heartbeat.json` | Agent liveness + per-source health for the dashboard. |
| Run log | `data/logs/agent_YYYYMMDD.log` | Structured, redacted operational log. |
| Power BI dataset | `powerbi/` (spec, DAX, data dictionary) | Dashboard definition that flags affected integrations. |

---

## 8. Power BI dashboard requirements

The dashboard **must** include, at minimum:
1. **Exception overview KPIs:** total open exceptions, integrations affected, customers affected, % of invoices with missing fields, agent last-run / health.
2. **Flagged integrations table:** every integration with ≥1 open exception, sorted by severity then count, with drill-through.
3. **Missing-field Pareto:** which required fields are most frequently missing (top offenders).
4. **By customer / trading partner:** exceptions per customer, highlighting partners with chronic issues.
5. **By EDI standard (810 vs INVOIC)** breakdown.
6. **Trend over time:** exceptions per day/week to show whether quality is improving.
7. **Source health tile:** OIC / staging / Cleo reachability from the heartbeat.

See `powerbi/dashboard_spec.md` and `powerbi/measures.dax` for the concrete layout and measures.

---

## 9. Failure handling & alerting

- Source unreachable → mark `DEGRADED`, log, continue. Three consecutive degraded runs → set heartbeat `alert=true` (dashboard surfaces it).
- Schema/config load failure → **abort the run** (do not validate against a stale/partial schema), keep last-good data, log `CONFIG_ERROR`.
- Sink write failure → retry per backoff policy; if still failing, keep the other sink and flag in heartbeat.
- Unexpected exception in code → catch at run boundary, log with run_id, exit non-zero so the scheduler/orchestrator notices.

---

## 10. Change management

- All required-field rules and partner overrides change via `config/required_fields.yaml` only, version-controlled and peer-reviewed.
- Any expansion of scope (new document type, new source, new write capability) requires updating **this file first** and explicit human approval before code changes.
- The agent must log the **config version/hash** it ran with on every run for auditability.

---

## 11. Operating principles

1. **Observe, don't touch.** When in doubt, report — never act on the production flow.
2. **Least data.** Capture the minimum needed to locate and explain a defect.
3. **Fail safe, fail loud.** Degrade gracefully, surface health, never hide failures.
4. **Deterministic & auditable.** Same input → same output; every run is traceable to a config version.
5. **Humans decide remediation.** The agent flags; people fix.
