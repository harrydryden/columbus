# us-outbound

Spill's account-based outbound engine for US organizations of 10 to 249 employees.
Python jobs on Cloud Run find and score companies, pick one contact, enroll them in one
Instantly campaign per sender, classify replies, and hand warm leads to HubSpot and to
Harry in Slack. [SPEC.md](SPEC.md) is the complete brief; this file is only the map.

## Dry-run and live

Everything runs in **dry-run by default** (SPEC 0.3): it computes, logs and writes to
BigQuery, but writes nothing to HubSpot, Instantly or the settings sheet, and posts to
Slack only in `#us-outbound-dev`. Every outbound call goes through one guard
(`us_outbound/clients/guard.py`), which also enforces the guardrails of SPEC 1 in any mode.

- **Jobs** (`run <job>`, `rescore`, `settings sync`, `suppression load`) and `start` are live
  only with `--live` **and** `live_sending = yes` in the settings sheet.
- **Operator commands** whose writes never reach a prospect (`stop`, `mailbox`, `unenrol`,
  `erase`, `test start`, `settings bootstrap`, `hubspot setup`, `campaigns ensure`) are live
  with `--live` alone, so the phase-0 setup and the kill switch work while `live_sending` is
  still `no`.

## Commands

```
us-outbound status                                 settings, heartbeats, mailboxes, campaigns
us-outbound stop | start [--live]                  pause / resume every US Outbound campaign and enrollment
us-outbound mailbox add <address> --owner "Name" [--domain D] [--daily-cap N] [--live]
us-outbound mailbox pause|retire <address> [--live]
us-outbound mailbox check [--live]                 mailbox_health by hand
us-outbound unenrol --month YYYY-MM [--live]       remove that month's leads from their campaigns
us-outbound rescore [--live]
us-outbound dry-run <job>
us-outbound run <job> [--live]                     the Cloud Run entrypoint
us-outbound erase --email <address> [--live]       an erasure request (SPEC 6)
us-outbound test start|read <test_id> [--live]     the copy test (SPEC 12)
us-outbound settings sync|bootstrap [--live]
us-outbound bq apply [--live]                      the DDL in sql/ (prints it unless --live)
us-outbound hubspot setup|ids [--live]             the six properties; ids for the General tab
us-outbound campaigns ensure [--fix] [--live]      the sender campaigns (created paused) and drift
us-outbound suppression load [--live]              HubSpot opt-outs and bounces, hashed
us-outbound deploy plan                            the job list deploy/deploy.sh reads
```

Jobs are listed in `us_outbound/ops/cli.py` (`JOBS`) and scheduled in `deploy/jobs.yaml`
(UK times). A job of a later phase exits with "not built yet (phase N)".

## Tests

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Tests never touch a real system: `tests/fakes.py` records every HTTP request and
`MemoryStore` stands in for BigQuery.

## Deploying

`deploy/setup.sh` (once: service account, roles, empty secrets, image repository), then
`deploy/deploy.sh` (image, one Cloud Run Job per job, Cloud Scheduler in Europe/London).
There is no public endpoint. Secrets are added by hand with `gcloud secrets versions add`
and never live in this repository. See [docs/phase0-runbook.md](docs/phase0-runbook.md).

## Phase status

| Phase | State |
| :- | :- |
| 0 Foundations | Being built: clients, guard, settings sync, DDL, registry, heartbeats, suppression, HubSpot setup, CLI, deploy |
| 1 Universe | Scoring built early; sources and Clay verification not built |
| 2 First sends | Enrollment being built; replies, approvals and readback not built |
| 3 Learning loop | Not built |

## Deviations from the SPEC 13 layout

Files added beyond the SPEC 13 tree:

- `clients/guard.py` (the one guard every call passes), `clients/http.py` (the one HTTP path),
  `clients/public.py` (public feeds and the one-redirect domain check)
- `settings/model.py` (typed settings), `settings/defaults.py` (the sheet as first created)
- `context.py` (what every job receives), `logs.py` (hashed emails, clipped bodies)
- `suppression.py` (the one hashed suppression list)
- `ops/bootstrap.py` (the production context), `ops/ddl.py` (applies `sql/`), `__main__.py`
- `deploy/` (Dockerfile companions: `jobs.yaml`, `setup.sh`, `deploy.sh`, the Slack app manifest)

Tables beyond SPEC 6: `heartbeats`, `credit_ledger`, `hitl_items`, `domain_aliases`,
`partners`. `suppression` gains `expires_at` (only Suppress-signal domains expire) and
`contacts` gains `last_step_at` (for retention). General keys added by the build:
`dev_channel`, `hubspot_pipeline_id`, `hubspot_deal_stage_id`, `hubspot_owner_id`, the two
Clay function ids, and the credits-per-account estimates.

Jobs beyond SPEC 9: `heartbeat_check` (hourly: a missed heartbeat alerts in Slack) and
`suppression_load` (daily: HubSpot opt-outs and bounces). `stop` and `start` record the
enrollment pause as heartbeats rows (`operator_stop` / `operator_start`).
