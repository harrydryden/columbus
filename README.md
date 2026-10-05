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

- **Jobs** (`run <job>`, `rescore`, `sync`, `suppression load`) and `start` are live
  only with `--live` **and** `live_sending = yes` in the synced settings. Jobs read the copy of the
  sheet that `settings_sync` brought in: at 02:00 UK, at 11:30 UK on weekdays, or now with
  `us-outbound sync` (`start --live` syncs first by itself).
- **Operator commands** whose writes never reach a prospect (`stop`, `mailbox`, `unenrol`,
  `erase`, `test start`, `settings bootstrap|load`, `hubspot setup`, `campaigns ensure`,
  `handcheck show|approve`, `killrules clear`, `replies skip`, `approvals contact|company`) are live
  with `--live` alone, so the phase-0 setup and the kill switch work while `live_sending` is still
  `no`. `replies send` sends to a prospect and `approvals send` adds one to Instantly, so they need
  both, like a job.
- **The brake** is `us-outbound stop --live`: `live_sending` = no stops nothing already active in
  Instantly. Harry's day-to-day is in [docs/daily.md](docs/daily.md).

## Commands

`us-outbound --help` lists what Harry uses; the build and duplicate commands still work but are left out
of it (marked * below).

```
us-outbound status                                 the switches, when settings synced, what waits, jobs that need a look
us-outbound golive                                 the read-only go/no-go check before sending (exits 1 on a FAIL)
us-outbound accounts [DOMAIN] [--csv]              the companies and contacts we hold (read-only): a summary and the list, one company, a CSV
us-outbound accounts [--status S] [--tier T] [--industry X] [--limit N]   the list, narrowed (default 25; --limit 0 lists all)
us-outbound sync [--live]                          bring the sheet's edits into force now (= settings sync)
us-outbound seed send ADDRESS --owner NAME [--live] | seed check   the seed-inbox test of the unsubscribe link
us-outbound start | stop [--live]                  resume (syncing first) / pause every US Outbound campaign and enrollment
us-outbound approvals list                         emails waiting for a ✅ (auto_send = no): company, contact, sender, subject
us-outbound approvals send <id> [--live]           ✅: add its lead to Instantly (= approvals approve)
us-outbound approvals contact|company <id> [--live]   👤 not this person / 🚫 not this company (= approvals reject --contact|--company)
us-outbound replies list                           reply items waiting for a human: class, account, role, excerpt, draft
us-outbound replies send <id> [--text "..."] [--live]   send the draft (or that text) (= replies approve [--edit])
us-outbound replies skip <id> [--live]             handled, nothing sent
us-outbound killrules show|clear <item> [--live]   the kill-rule holds in force; lift one once checked
us-outbound mailbox add <address> --owner "Name" [--domain D] [--daily-cap N] [--live]
us-outbound mailbox pause|retire <address> [--live]
us-outbound mailbox check [--fix] [--live]         mailbox_health by hand; --fix also runs campaigns ensure --fix
us-outbound campaigns show                         each owner's campaign as Instantly holds it (read-only)
us-outbound campaigns ensure [--fix] [--live]      the sender campaigns (created paused) and drift
us-outbound copy check|preview|qa|draft [...]      the copy desk: check every row, preview one, QA it, draft one
us-outbound settings sync|load|bootstrap [--live]  sync; load the build's tabs (--tab, --set, --take note) into the sheet; create it
us-outbound handcheck show|approve [--pull ID ...] [--live]   this week's hand-check without Slack
us-outbound erase --email <address> [--live]       an erasure request
us-outbound schedule                               the job table, UK times and next runs (also scheduler --list)
us-outbound run <job> [--live]                     one job (what the scheduler starts)
us-outbound unenrol --month YYYY-MM [--live]     * remove that month's leads from their campaigns
us-outbound rescore [--live]                     * the score job
us-outbound dry-run <job>                        * one job, dry-run
us-outbound test start|read <test_id> [--live]   * the copy test
us-outbound db apply [--live]                    * the DDL in sql/ against DATABASE_URL (prints it unless --live)
us-outbound hubspot setup|ids [--live]           * the six properties; ids for the General tab
us-outbound suppression load [--live]            * HubSpot opt-outs and bounces, hashed
us-outbound lookalikes show [--top N] [--all]    * the lookalike cells from Spill's HubSpot customers
us-outbound pages show                           * the careers and benefits page reader's coverage
us-outbound data show                            * what the sources have stored, in aggregate
us-outbound scheduler                            * the always-on worker: starts every job on its schedule
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
| 0 Foundations | Built and running on Railway: clients, guard, settings sync (02:00, and 11:30 on weekdays), DDL, registry, heartbeats, suppression, HubSpot setup, CLI, the scheduler |
| 1 Universe | Built: scoring, the Apollo universe, job postings, the careers and benefits page reader (`read_pages`), Apollo's organization enrich (`apollo_enrich`), People leaders at every account (`apollo_people`), lookalikes, site visits (`site_visits`, 5 Oct), and `verify_accounts` while Clay verification is not built. Not built: public signals, `verify_in_clay` |
| 2 First sends | Built; the pilot sends from Mon 5 Oct 2026: enrollment with send approvals in Slack (`auto_send` = no), the sending ramp, `golive`, reply ingest (`sync_outcomes`, `poll_replies`), the reply desk (`poll_approvals`, `replies` commands), the reply HubSpot writes and `hubspot_readback` |
| 3 Learning loop | Kill rules and the daily post (with its "Needs you" line) built early (Harry, 1 Oct 2026); readout not built |

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
- `replies/outcomes.py` (sync_outcomes), `replies/optout.py` (one opt-out path for links and replies),
  `replies/draft.py` (reply drafts); `replies/poll.py` holds the hitl_items contract with the reply desk

Infrastructure (Harry, 30 Sep 2026): Railway instead of Google Cloud. Railway PostgreSQL
replaces BigQuery (SPEC 3, 6). Sealed Railway variables replace Secret Manager (SPEC 1.7).
One worker with its own scheduler replaces Cloud Run Jobs and Cloud Scheduler (SPEC 3).
See [docs/railway-setup.md](docs/railway-setup.md#where-this-differs-from-spec-harry-30-sep-2026).

Tables beyond SPEC 6: `heartbeats`, `credit_ledger`, `hitl_items`, `domain_aliases`,
`partners`, `lookalike_cells`. `suppression` gains `expires_at` (only Suppress-signal domains expire) and
`contacts` gains `last_step_at` (for retention). `settings` also holds one `_order` row per
tab, its keys in sheet order, so the jobs keep the sheet's order. General keys added by the build:
`dev_channel`, `hubspot_pipeline_id`, `hubspot_deal_stage_id`, `hubspot_owner_id`, the two
Clay function ids, the credits-per-account estimates, `stop_rule_bounce_rate` and
`stop_rule_complaint_rate` (the stop rule's account-level thresholds) and `optout_tested` (the
seed-inbox test of the unsubscribe link, which `golive` checks) and `auto_send` (no: every email
waits for approval in Slack; Harry, 2 Oct 2026).

The reply desk (decision D11, Harry, 1 Oct 2026): approvers are `approver_slack_ids` plus a
mailbox's owner for replies to that mailbox, from an optional `slack_id` column on the Mailboxes
tab; approvals are ✅ or "send", "send: <text>", "edit: <text>", ❌ or "skip"; unanswered alerts
are re-posted until 23:00 UK. `us-outbound replies list|approve|skip` does the same without Slack.

Send approvals (Harry, 2 Oct 2026: "every single message that gets sent out comes to this channel
first for approval"): while the General key `auto_send` is `no` (the default), `enrol` posts each
account as a card in #us-outbound instead of adding its lead to Instantly: the company and its
domain, the recipient linked to Apollo, the sender, email 1's subject and body, the count of emails,
and emails 2 to 4 in the thread. An approver's ✅ (seeded by the bot, so it is one click) adds the
lead; ❌ offers ✏️ edit (a thread reply, re-rendered and checked against the copy rules, then
approved again), 👤 another contact, or 🚫 drop the company. A card not approved by the end of its
next send day expires. With `auto_send` = `yes`, `enrol` adds leads straight away after the weekly
hand-check, as before. `us_outbound/enrol/approvals.py` has the hitl_items and events contract the
daily report reads; `us-outbound approvals list|approve|reject` does the same work without Slack.

Jobs beyond SPEC 9: `heartbeat_check` (hourly: a missed heartbeat alerts in Slack),
`suppression_load` (daily: HubSpot opt-outs and bounces), `lookalikes` (Mondays: Spill's
HubSpot customers as lookalike cells, a signal and an early exclusion; `sources/lookalikes.py`,
Harry, 1 Oct 2026; `us-outbound lookalikes show` lists the cells) and `hand_check_post` (Mondays
08:00: SPEC 11's weekly hand-check. Enrol waits for it only while `auto_send` = yes; with `auto_send`
= no every email is approved in Slack, and the hand-check holds only accounts with doubtful Apollo
facts). `stop` and `start` record the
enrollment pause as heartbeats rows (`operator_stop` / `operator_start`). Each kill rule that
fires is a `hitl_items` row (kind `kill_rule`) that holds the mailbox, source, industry group or
enrollment until it is cleared (`learn/holds.py`). The sending ramp (`registry/ramp.py`: 10 a day
in a mailbox's first sending week, 20 in its second, then its cap) sets the forecast, each
campaign's daily limit and each Instantly account's own limit.

For the 5 Oct pilot (1 Oct 2026): `verify_accounts` (weekdays 04:30) verifies accounts on their
Apollo data and HubSpot while the General key `clay_verification` is `skip` (Harry: go live before
the Clay functions exist; `required` once they do). `source_universe` and `apollo_signals` run each
weekday (03:00, 03:30) rather than monthly and on Mondays, to keep the queue two weeks deep with
Apollo credits paced by the weekday (`us_outbound/sources/`).

Clay narrowed (Harry, 2 Oct 2026): `read_pages` (weekdays 03:45) is our own careers and benefits
page reader, in place of Clay's: each queued account's own careers, jobs and benefits pages and
its public Greenhouse, Lever, Ashby or Workable board, with no Clay credits (`sources/pages.py`,
source `careers_pages`, and `sources/job_posts.py`). `us-outbound pages show` and the daily post
report its coverage for the decision on enhancing it. Clay is kept for the email waterfall: with
the General key `clay_email_fallback` = yes (default no), `pick_contacts` asks Clay's Work Email
for a person Apollo has no verified email for. `verify_accounts` sends accounts with doubtful
Apollo facts (no HQ state or size, a count near a size edge) to the weekly hand-check.

Funding from Apollo's organization enrich (Harry, 2 Oct 2026): Apollo's search rows carry no
funding and no employee count, so `apollo_enrich` (weekdays 04:10, before `verify_accounts`)
enriches the queue accounts in the General key `apollo_enrich_groups` (default Technology &
Startups), 1 credit per company found, within 15% of `apollo_monthly_credits`, each again after
180 days (`sources/apollo_enrich.py`). Its facts fire the funding signals, and its exact employee
count replaces the searched size band unless an Overrides row or Clay says otherwise.

People leaders at every account (Harry, 5 Oct 2026): `apollo_people` (weekdays 04:20, before
`verify_accounts`' rescore) searches Apollo's free people search for the People leaders at each
queue account, with no email filter, and for how many people Apollo holds there, so "New People
leader" and "People leader in place" score before the queue is sorted rather than only for accounts
`pick_contacts` reaches. A count of 0 is written only where Apollo holds at least half the headcount,
which the new "First People hire (likely)" row reads (`sources/apollo_people.py`).

`sync_outcomes` runs every 15 minutes (SPEC 9: 01:00 daily), so the kill rules, the send forecast
and same-day opt-outs (SPEC 13) see today's sends, bounces and unsubscribes.
