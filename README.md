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

- **Jobs** (`run <job>`, `sync`) and `start` are live
  only with `--live` **and** `live_sending = yes` in the synced settings. Jobs read the copy of the
  sheet that `settings_sync` brought in: at 02:00 UK, at 11:30 UK on weekdays, or now with
  `us-outbound sync` (`start --live` syncs first by itself).
- **Operator commands** whose writes never reach a prospect (`stop`, `mailbox`, `unenrol`,
  `erase`, `test start`, `settings load`, `campaigns ensure`, `report capture`,
  `handcheck show|approve`, `killrules clear`, `replies skip`) are live
  with `--live` alone, so the setup and the kill switch work while `live_sending` is still
  `no`. `replies send` sends to a prospect, so it needs both, like a job.
- **The brake** is `us-outbound stop --live`: `live_sending` = no stops nothing already active in
  Instantly. Harry's day-to-day is in [docs/daily.md](docs/daily.md).

## Commands

`us-outbound --help` lists every command: one table in `us_outbound/ops/cli.py` (`COMMANDS`, W2-4, 20 Oct 2026).
`tests/test_cli.py` checks that every command an alert or a doc names is one the parser has.

```
us-outbound status                                 live_sending, when settings synced, what waits, jobs that need a look, retention, mailboxes, campaigns, and the go-live checks (Copy, Campaigns, Watchdog)
us-outbound accounts [DOMAIN] [--csv]              the companies and contacts we hold (read-only): a summary and the list, one company, a CSV
us-outbound accounts [--status S] [--tier T] [--industry X] [--limit N]   the list, narrowed (default 25; --limit 0 lists all)
us-outbound sync [--live]                          bring the sheet's edits into force now
us-outbound start | stop [--no-db] [--live]        resume (syncing first) / pause every US Outbound campaign and enrollment; stop --no-db pauses the campaigns while the database is down
us-outbound replies list                           reply items waiting for a human: class, account, role, excerpt, draft
us-outbound replies send <id> [--text "..."] [--live]   send the draft (or that text) (= replies approve [--edit])
us-outbound replies skip <id> [--live]             handled, nothing sent
us-outbound handcheck show|approve [--pull ID ...] [--live]   this week's hand-check without Slack
us-outbound handcheck export|migrate-tab [--live]   every decided Hand-check row into the database; or move the tab to its new columns once
us-outbound killrules show|clear <item> [--live]   the kill-rule holds in force; lift one once checked
us-outbound mailbox add <address> --owner "Name" [--domain D] [--daily-cap N] [--live]
us-outbound mailbox pause|retire <address> [--live]
us-outbound mailbox check [--fix] [--live]         mailbox_health by hand; --fix also sets each sender name to its owner's full name and runs campaigns ensure --fix
us-outbound campaigns ensure [--fix] [--in-flight] [--live]   the sender campaigns (created paused) and drift; drift in the steps, delays or text_only waits while leads are in flight unless --in-flight
us-outbound cohorts changes [A B] | in-flight      what changed between two config versions (default the last two) | each campaign's leads in flight
us-outbound test start|read <test_id> [--live]     a test on the Tests tab: ab, variant (one change to one email) or holdout
us-outbound labels set|show <domain> [label] [--live] | labels eval [--from-corrections] [--from-sample] [--live]   a company's label set or shown; the label check scored on the gold set
us-outbound copy check|preview|qa|draft [...]      the copy desk: check every row, preview one, QA it, draft one
us-outbound seed send ADDRESS --owner NAME [--subject personal|copy] [--live] | seed check   the seed-inbox test of the unsubscribe link
us-outbound settings load [--tab T] [--set K=V] [--take note] [--live]   the build's tabs (or the General notes) into the sheet
us-outbound report cohorts [--cut CUT] [--age N] [--weeks N]   each enrolment week at 7, 14, 21 and 28 days after email 1 (read-only)
us-outbound report signals | readout | pages | daily [--full]   which signals predict replies; the Monday readout; the page reader's coverage; the daily post (read-only)
us-outbound report lookalikes [--top N] [--all]    the lookalike cells from Spill's HubSpot customers (read-only)
us-outbound report capture [--live]                the reply read's weekly canary, checked the day after it went
us-outbound erase --email <address> [--live]       an erasure request
us-outbound unenrol --month YYYY-MM [--live]       a month's leads out of their campaigns, each recorded as stopped (the accounts are never enrolled again by themselves)
us-outbound verify migrate-parked [--live]         once after W1-4: the companies left out while their industry was off, parked
us-outbound db apply [--live]                      the DDL in sql/ against DATABASE_URL (prints it unless --live)
us-outbound schedule                               the job table, UK times and next runs (also scheduler --list)
us-outbound run <job> [--live] [--due MINUTE]      one job (what the scheduler starts, with the minute it was due); `run score` rescores
us-outbound scheduler                              the always-on worker: starts every job on its schedule
```

Folded or removed in W2-4 (20 Oct 2026; git history keeps them): `daily`, `readout`, `signals review`, `pages show`,
`lookalikes show` and `capture check` are `report ...`; `rescore`, `dry-run <job>` and `suppression load` are
`run score`, `run <job>` and `run suppression_load`; `settings sync` is `sync`. Removed: `relabel` and `labels audit`
(verify_accounts asks the label check about 150 companies each weekday, and again when a definition changes),
`labels sample` (the `label_sample_monthly` job), `signals value`, `lookalikes fit`, `campaigns show`, `hubspot ids`,
`settings bootstrap` and `data show`.

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
| 1 Universe | Built: scoring, the Apollo universe, job postings, the careers and benefits page reader (`read_pages`), Apollo's organization enrich (`apollo_enrich`), People leaders at every account (`apollo_people`), lookalikes, site visits (`site_visits`, 5 Oct), and `verify_accounts`. (Public signals and `verify_in_clay` were never built, and went with Clay in W2-1, 20 Oct 2026) |
| 2 First sends | Built; the pilot sends from Mon 5 Oct 2026: enrollment (send approvals in Slack until W2-1, 20 Oct 2026, removed them with `auto_send`), the sending ramp, the go-live checks in `status`, reply ingest (`sync_outcomes`, `poll_replies`), the reply desk (`poll_approvals`, `replies` commands), the reply HubSpot writes and `hubspot_readback`; `retention` (7 Oct 2026: SPEC 6's retention rules and SPEC 13's 31 days) |
| 3 Learning loop | Kill rules and the daily post (with its "Needs you" line) built early (Harry, 1 Oct 2026); the Monday readout, the signal table, tests read at pre-registered looks, UTM tags and demo bookings read back (6 Oct 2026; docs/pipeline.md "How the system learns"); cohorts by enrolment week with the config version each contact was enrolled under (7 Oct 2026; docs/developing-while-live.md) |

## Deviations from the SPEC 13 layout

Files added beyond the SPEC 13 tree:

- `clients/guard.py` (the one guard every call passes), `clients/http.py` (the one HTTP path),
  `clients/public.py` (public feeds and the one-redirect domain check)
- `settings/model.py` (typed settings), `settings/defaults.py` (the sheet as first created)
- `context.py` (what every job receives), `logs.py` (hashed emails, clipped bodies)
- `suppression.py` (the one hashed suppression list)
- `ops/bootstrap.py` (the production context), `ops/ddl.py` (applies `sql/`), `__main__.py`
- `ops/schedule.py` (the job table) and `ops/scheduler.py` (the always-on worker that runs it)
- `base/` (9 Oct 2026): the alert poster (`notify.py`), the plain words of the alerts (`say.py`) and the kill-rule
  holds (`holds.py`), one layer below enrolment, learning and operations
- `ops/health.py` (W2-4, 20 Oct 2026): job health in one module, merging `ops/heartbeat.py`, `base/heartbeats.py`,
  `ops/job_errors.py` and `ops/watchdog.py`: what a run records (`run_job`, the only writer of heartbeats rows, with
  `duration_s` and `exit_code`), the operator stop, the `heartbeat_check` job and the outside watchdog's ping
- `copy/` (9 Oct 2026): the copy rules, the Copy tab's markup, its variables and the variant-text check, moved
  out of `enrol/` so settings_sync checks copy without importing enrolment
- `deploy/slack-app-manifest.yaml` (the Slack app)
- `replies/outcomes.py` (sync_outcomes), `replies/optout.py` (one opt-out path for links and replies),
  `replies/draft.py` (reply drafts); `replies/poll.py` (poll_replies, the one reader of received mail) holds the
  hitl_items contract with the reply desk: `replies/desk.py` (the items and their close path),
  `replies/poll_desk.py` (the poll_approvals job), `replies/escalation.py`, and `replies/follow_up.py` (the not-now
  dates kept, daily at 08:45)

Infrastructure (Harry, 30 Sep 2026): Railway instead of Google Cloud. Railway PostgreSQL
replaces BigQuery (SPEC 3, 6). Sealed Railway variables replace Secret Manager (SPEC 1.7).
One worker with its own scheduler replaces Cloud Run Jobs and Cloud Scheduler (SPEC 3).
See [docs/railway-setup.md](docs/railway-setup.md#where-this-differs-from-spec-harry-30-sep-2026).

Tables beyond SPEC 6: `heartbeats`, `credit_ledger`, `hitl_items`, `domain_aliases`,
`partners`, `lookalike_cells`, `lookalike_growth`, `config_versions` and `config_log` (Harry, 7 Oct 2026: what
each cohort was enrolled under, and each change to what the leads in flight share; `config_version.py`).
`suppression` gains `expires_at` (only Suppress-signal domains expire) and
`contacts` gains `last_step_at` and `lead_deleted_at` (for retention: when its Instantly lead was deleted, 31 days
after its last step), and `config_version`, `code_sha` and `copy_hash` (what the
contact's emails were rendered under; NULL, "unstamped", before 8 Oct 2026). `settings` also holds one `_order` row per
tab, its keys in sheet order, so the jobs keep the sheet's order. General keys added by the build:
`dev_channel`, `hubspot_pipeline_id`, `hubspot_deal_stage_id`, `hubspot_owner_id`, `stop_rule_bounce_rate` and
`stop_rule_complaint_rate` (the stop rule's account-level thresholds) and `optout_tested` (the
seed-inbox test of the unsubscribe link, which enrol's live gate reads), and `email1_subject` and `email1_subject_share`
(Harry, 5 Oct 2026: email 1's personal subject, "support for the {{company}} team", for half the
companies by a hash of the account id; the rest keep the Copy row's `s1_subject`, and
`contacts.subject_arm` records which, so `us-outbound report signals` can compare the two).

The reply desk (decision D11, Harry, 1 Oct 2026): approvers are `approver_slack_ids` plus a
mailbox's owner for replies to that mailbox, from an optional `slack_id` column on the Mailboxes
tab; approvals are ✅ or "send", "send: <text>", "edit: <text>", ❌ or "skip"; unanswered alerts
are re-posted until 23:00 UK. `us-outbound replies list|approve|skip` does the same without Slack.

Removed in W2-1 (20 Oct 2026; Harry's D6, D18 and his ruling on second contacts; git history keeps the code): the
send approvals (`auto_send`: each email a card in #us-outbound for an approver's ✅; the live sheet had `auto_send` =
yes, so `enrol` adds each day's leads straight to Instantly, as it did), every Clay path (the email waterfall, the
cross-check, `verify_in_clay`, the Clay client and budget), second contacts (`second_contact`: one contact per
company; `contacts.contact_slot` stays in the DDL), the one-off tools (`golive`, whose Copy, Campaigns and Watchdog
checks now print in `status`; `phase0 check`; `clay check-email`; `labels crosswalk`), the Named accounts tab, the job
board reader, the unbanded universe search and its size-band backfill, and the never-confirmed out-of-office
re-timing. The retired General keys are in `settings/validate.RETIRED_GENERAL`: a sheet row for one warns and is not
read. The `poll_approvals` job keeps its name; it runs the reply desk and the hand-check tab
(`replies/poll_desk.poll_desk`), and closes any send approval left open, as expired.

Jobs beyond SPEC 9: `retention` (daily 00:40, live: SPEC 6's retention rules and SPEC 13's 31 days, which SPEC 9
gave to sync_outcomes; `ops/retention.py`, built 7 Oct 2026): it deletes each Instantly lead more than 31 days after
its last step (dated as the send forecast dates it, or from its stop, or the conversation's last email; never in
flight, never while a reply waits for a person or an opt-out or bounce is still to record; at most 200 a run),
clears reply text from events and the reply cards 90 days after the reply, deletes contacts who never replied 12
months after their last step and company rows no source has refreshed in 12 months (only those nothing else
holds), and never touches suppression. Dry-run counts and changes nothing,
the database included. The daily post has one line when it deleted anything, and `us-outbound status` says what it
last did and what waits; `us-outbound run retention` shows what is due now. Apollo deletion notices have no API:
Harry honours each within 30 days with `us-outbound erase --email ADDRESS --live` (docs/daily.md).
`heartbeat_check` (hourly: a missed heartbeat alerts in Slack, and so do the
errors a job caught and a key a service rejected; it then pings the outside
watchdog, Healthchecks.io, which emails Harry when the worker, the database or Slack is down;
`ops/health.py`; Harry, 7 Oct 2026),
`blackout` (hourly at :40: every US Outbound campaign paused over the General `blackout_dates`, in US Eastern dates,
and exactly the ones it paused started again after them, as Instantly's schedule knows weekdays only; a stop, a kill
rule or an empty sending list keeps one paused; each pause and restart is a `config_log` row; `registry/blackout.py`,
Harry, 7 Oct 2026), `suppression_load` (daily: HubSpot opt-outs and bounces, and Spill's customers' domains, so a new
customer is kept out and its leads in flight stopped by the next night; Harry, 7 Oct 2026), `lookalikes` (the 1st of each month, from
5 Oct 2026: Spill's HubSpot customers as lookalike cells, their growth from Apollo in aggregate, the
lookalike fit and an early exclusion; `sources/lookalikes.py`, Harry, 1 and 5 Oct 2026;
`us-outbound report lookalikes` lists the cells) and `hand_check_draw` (Mondays
05:45, in place of `hand_check_post` at 08:00 since W1-5, 19 Oct 2026: SPEC 11's weekly hand-check, drawn from the
accounts the coming send days would take, `hand_check_sample` of them, written to the Hand-check tab. With
`hand_check_gate` = yes enrol waits for the whole check, as before; with `hand_check_gate` = no,
Harry's D5, only the sampled companies wait, until judged or for 7 days). `stop` and `start` record the
enrollment pause as heartbeats rows (`operator_stop` / `operator_start`). Each kill rule that
fires is a `hitl_items` row (kind `kill_rule`) that holds the mailbox, source, industry group or
enrollment until it is cleared (`base/holds.py`). The sending ramp (`registry/ramp.py`: 10 a day
in a mailbox's first sending week, 20 in its second, then its cap) sets the forecast, each
campaign's daily limit and each Instantly account's own limit.

Developing while live (Harry, 7 Oct 2026; [docs/developing-while-live.md](docs/developing-while-live.md)):
`campaigns ensure --fix` holds drift in a campaign's steps, delays or text_only while it has leads in flight
(they are shared by every lead in it), and `--in-flight` applies it to them too, as a `config_log` row with
the count. `mailbox_health` and `mailbox check --fix` never apply it. A lead whose address or domain is
suppressed while it is in flight (a HubSpot opt-out from `suppression_load`, or a company that has become a Spill
customer, which `suppression_load` reads nightly) is stopped by `sync_outcomes`.

The sender name (Harry, 5 Oct 2026): each Instantly account's first and last name, which make the
From name prospects see, are its owner's full name from the Mailboxes tab ("Hannah Spalding", not
"Hannah at Spill"). The morning mailbox check reports a name that differs, and
`us-outbound mailbox check --fix --live` sets it (`Instantly.set_sender_name`, guard action
`account.update_name`: a registry account, a mailbox owner's name, those two fields only).

For the 5 Oct pilot (1 Oct 2026): `verify_accounts` (weekdays 04:30) verifies accounts on their
Apollo data and HubSpot (Clay was never built, and went with W2-1). `source_universe` and `apollo_signals` run each
weekday (03:00; 05:10, after `verify_accounts`, since 19 Oct) rather than monthly and on Mondays, to keep
the queue two weeks deep in companies that can be emailed ("contactable supply", W1-3a), with Apollo
credits paced by the weekday (`us_outbound/sources/`).

`read_pages` (weekdays 03:45; Harry, 2 Oct 2026) is our own careers and benefits page reader: each queued account's
own careers, jobs and benefits pages, with no Clay credits (`sources/pages.py`, source `careers_pages`), and the
home-page pass the label check uses. `us-outbound report pages` and the daily post report its coverage for the decision
on enhancing it. Its job-board reader (`sources/job_posts.py`) found no candidate in any run and went with W2-1.

Funding from Apollo's organization enrich (Harry, 2 Oct 2026): Apollo's search rows carry no
funding and no employee count, so `apollo_enrich` (weekdays 05:00, after `verify_accounts` since 19 Oct)
enriches the verified accounts in the industry groups of `settings/model.APOLLO_ENRICH_GROUPS` (Technology &
Startups; a General key until W2-4), 1 credit per company found, within 15% of `apollo_monthly_credits`, each again after
180 days (`sources/apollo_enrich.py`). Its facts fire the funding signals, and its exact employee
count replaces the searched size band unless an Overrides row or Clay says otherwise; a count outside
the General size range leaves the company out (`status_reason` `size`).

People leaders at every account (Harry, 5 Oct 2026): `apollo_people` (weekdays 04:20, before
`verify_accounts`' rescore) searches Apollo's free people search for the People leaders at each
queue account, with no email filter, and for how many people Apollo holds there, so "New People
leader" and "People leader in place" score before the queue is sorted rather than only for accounts
`pick_contacts` reaches. A count of 0 is written only where Apollo holds at least half the headcount,
which the new "First People hire (likely)" row reads (`sources/apollo_people.py`).

`sync_outcomes` runs every 15 minutes inside Instantly's US Eastern send window (13:00 to 22:59 UK on weekdays)
and hourly otherwise (W2-4; SPEC 9: 01:00 daily), so the kill rules, the send forecast and same-day opt-outs (SPEC
13) see today's sends, bounces and unsubscribes. It lists no received mail: `poll_replies` (every 15 minutes) is the
one reader of it, and records each reply's event before it classifies it.

The scheduler (W2-4, 20 Oct 2026; `ops/scheduler.py`): one runs. It holds a Postgres advisory lock before it starts
anything, so over a deploy the new worker waits (logging `scheduler_lock_wait`) until the old one's SIGTERM frees it;
a daily job whose time fell in the 90 minutes before it started, with no heartbeat since, is caught up once; and a
start for a due minute claims the heartbeats row `<job>:<minute>`, so a second start for that minute runs nothing.
