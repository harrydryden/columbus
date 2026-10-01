# us-outbound

Spill's account-based outbound engine for US organizations of 10 to 249 employees.
Python jobs find and score companies, pick one contact, enroll them in one Instantly
campaign per sender, classify replies, and hand warm leads to HubSpot and to Harry in
Slack. They run on Railway: one always-on worker whose scheduler starts each job, and a
Postgres database. [SPEC.md](SPEC.md) is the complete brief; this file is only the map.

## Dry-run and live

Everything runs in **dry-run by default** (SPEC 0.3): it computes, logs and writes to the
database, but writes nothing to HubSpot, Instantly or the settings sheet, and posts to
Slack only in `#us-outbound-dev`. Every outbound call goes through one guard
(`us_outbound/clients/guard.py`), which also enforces the guardrails of SPEC 1 in any mode.

- **Jobs** (`run <job>`, `rescore`, `settings sync`, `suppression load`) and `start` are live
  only with `--live` **and** `live_sending = yes` in the settings sheet.
- **Operator commands** whose writes never reach a prospect (`stop`, `mailbox`, `unenrol`,
  `erase`, `test start`, `settings bootstrap`, `hubspot setup`, `campaigns ensure`,
  `handcheck show|approve`, `killrules clear`) are live
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
us-outbound run <job> [--live]                     one job (what the scheduler starts)
us-outbound erase --email <address> [--live]       an erasure request (SPEC 6)
us-outbound test start|read <test_id> [--live]     the copy test (SPEC 12)
us-outbound settings sync|bootstrap [--live]
us-outbound db apply [--live]                      the DDL in sql/ against DATABASE_URL (prints it unless --live)
us-outbound hubspot setup|ids [--live]             the six properties; ids for the General tab
us-outbound campaigns ensure [--fix] [--live]      the sender campaigns (created paused) and drift
us-outbound suppression load [--live]              HubSpot opt-outs and bounces, hashed
us-outbound golive                                 the read-only go/no-go check before sending (exits 1 on a FAIL)
us-outbound handcheck show|approve [--pull ID ...] [--live]   this week's hand-check without Slack (SPEC 11)
us-outbound killrules show|clear <item> [--live]   the kill-rule holds in force; lift one once checked (SPEC 12)
us-outbound scheduler                              the always-on worker: starts every job on its schedule
us-outbound schedule                               the job table, UK times and next runs (also scheduler --list)
```

Jobs are listed in `us_outbound/ops/cli.py` (`JOBS`) and scheduled in `us_outbound/ops/schedule.py`
(cron in UK time; later-phase jobs disabled). A job of a later phase exits with "not built
yet (phase N)". On Railway, run any command inside the worker with
`railway ssh -- us-outbound <command>`.

## Tests

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Tests never touch a real system: `tests/fakes.py` records every HTTP request and
`MemoryStore` stands in for the Postgres database.

## Deploying (Railway)

One Railway project, **Columbus**, in the EU West (Amsterdam) region:

- a PostgreSQL database;
- one worker service built from the `Dockerfile`, whose default command is
  `us-outbound scheduler`. It has no public domain, because there is no public endpoint
  (SPEC 2);
- sealed service variables: `DATABASE_URL` (`${{Postgres.DATABASE_URL}}`),
  `US_OUTBOUND_SETTINGS_SHEET_ID`, `US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON` and the six keys
  in `context.SECRET_NAMES`.

Every push to `main` redeploys the worker, so Harry reviews before merging (SPEC 13). The
only Google piece is a Sheets-only service account in the project `columbus-510209`. Keys
never live in this repository. Step by step: [docs/railway-setup.md](docs/railway-setup.md);
then [docs/phase0-runbook.md](docs/phase0-runbook.md).

## Phase status

| Phase | State |
| :- | :- |
| 0 Foundations | Being built: clients, guard, settings sync, DDL, registry, heartbeats, suppression, HubSpot setup, CLI, the Railway scheduler |
| 1 Universe | Scoring built early; sources and Clay verification not built |
| 2 First sends | Enrollment, the sending ramp, the weekly hand-check and `golive` built; replies, approvals and readback not built |
| 3 Learning loop | Kill rules and the daily post built early (Harry, 1 Oct 2026); readout not built |

## Deviations from the SPEC 13 layout

Files added beyond the SPEC 13 tree:

- `clients/guard.py` (the one guard every call passes), `clients/http.py` (the one HTTP path),
  `clients/public.py` (public feeds and the one-redirect domain check)
- `settings/model.py` (typed settings), `settings/defaults.py` (the sheet as first created)
- `context.py` (what every job receives), `logs.py` (hashed emails, clipped bodies)
- `suppression.py` (the one hashed suppression list)
- `ops/bootstrap.py` (the production context), `ops/ddl.py` (applies `sql/`), `__main__.py`
- `ops/schedule.py` (the job table) and `ops/scheduler.py` (the always-on worker that runs it)
- `deploy/slack-app-manifest.yaml` (the Slack app)

Infrastructure (Harry, 30 Sep 2026): Railway instead of Google Cloud. Railway PostgreSQL
replaces BigQuery (SPEC 3, 6). Sealed Railway variables replace Secret Manager (SPEC 1.7).
One worker with its own scheduler replaces Cloud Run Jobs and Cloud Scheduler (SPEC 3).
See [docs/railway-setup.md](docs/railway-setup.md#where-this-differs-from-spec-harry-30-sep-2026).

Tables beyond SPEC 6: `heartbeats`, `credit_ledger`, `hitl_items`, `domain_aliases`,
`partners`. `suppression` gains `expires_at` (only Suppress-signal domains expire) and
`contacts` gains `last_step_at` (for retention). `settings` also holds one `_order` row per
tab, its keys in sheet order, so the jobs keep the sheet's order. General keys added by the build:
`dev_channel`, `hubspot_pipeline_id`, `hubspot_deal_stage_id`, `hubspot_owner_id`, the two
Clay function ids, the credits-per-account estimates, `stop_rule_bounce_rate` and
`stop_rule_complaint_rate` (the stop rule's account-level thresholds) and `optout_tested` (the
seed-inbox test of the unsubscribe link, which `golive` checks).

Jobs beyond SPEC 9: `heartbeat_check` (hourly: a missed heartbeat alerts in Slack),
`suppression_load` (daily: HubSpot opt-outs and bounces) and `hand_check_post` (Mondays 08:00:
SPEC 11's weekly hand-check, which enrol waits for). `stop` and `start` record the
enrollment pause as heartbeats rows (`operator_stop` / `operator_start`). Each kill rule that
fires is a `hitl_items` row (kind `kill_rule`) that holds the mailbox, source, industry group or
enrollment until it is cleared (`learn/holds.py`). The sending ramp (`registry/ramp.py`: 10 a day
in a mailbox's first sending week, 20 in its second, then its cap) sets the forecast, each
campaign's daily limit and each Instantly account's own limit.
