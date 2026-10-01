# US Outbound: build plan

SPEC.md is the brief. This file says how the build is sequenced, what is done, and what each phase needs from Harry. The build follows SPEC 0.2: build a phase, show the result against its acceptance checks, and wait for Harry's go-ahead before starting the next.

## Timeline

SPEC fixes three dates:
- Phase 0 runs 29 Sep – 9 Oct.
- Technology & Startups and Marketing & Creative Agencies switch on from 26 Oct.
- v1 is complete by 18 Dec.

The other dates are proposals.

| Phase | Proposed dates | Gate to start | Ends with |
| :- | :- | :- | :- |
| 0 Foundations | 29 Sep – 9 Oct | – | SPEC 14 phase 0 acceptance checks, shown to Harry |
| 1 Universe | 12 – 23 Oct | Harry's go-ahead on phase 0; Clay budget set; Clay functions built | Harry's hand-check of 10 accounts per industry group at ≥ 90% right; Clay credits per account measured; mailboxes warm at 30 a day |
| 2 First sends | 26 Oct – 13 Nov | Harry's go-ahead on phase 1; copy approved in the sheet; **[ASK HARRY] live sign-off** | Bounce rate < 3%; positive reply in HubSpot and Slack within 20 min; approved reply sent from the right mailbox; 50/50 split verified |
| 3 Learning loop | 16 Nov – 18 Dec (blackouts 23–27 Nov and from 18 Dec) | Harry's go-ahead on phase 2 | Readout unattended for two weeks; first test on track for its read date |
| 4 Scale | January onwards | v1 signed off | More mailboxes, Professional Services, competitor angle, new sources |

The Thanksgiving blackout (23–27 Nov) falls inside phase 3. Sends pause, but the jobs still run.

## Phase 0: status

### Code (done, in this repository)

| Deliverable (SPEC 14) | Where |
| :- | :- |
| Repository and clients | `us_outbound/clients/`. Every call goes through `guard.py` |
| Secrets (SPEC: Secret Manager; now sealed Railway variables) | `context.Secrets` reads each key from its environment variable; Harry adds the values in Railway |
| Database DDL (SPEC: BigQuery; now Railway Postgres) | `sql/ddl/`, `sql/views/`, `us-outbound db apply` |
| Job schedule (SPEC: Cloud Run Jobs and Cloud Scheduler; now one Railway worker) | `ops/schedule.py` (the SPEC 9 table), `ops/scheduler.py` (`us-outbound scheduler`, the worker's default command) |
| Settings sheet defaults and settings_sync with validation | `settings/defaults.py`, `settings/validate.py`, `settings/sync.py` |
| HubSpot: the six properties | `crm/hubspot_writes.py`, `us-outbound hubspot setup` |
| Instantly: three sender campaigns, created paused | `registry/mailboxes.py`, `us-outbound campaigns ensure` |
| Mailboxes: warmup status | `registry/mailboxes.py` (mailbox_health) |
| Suppression from HubSpot opt-outs and bounces | `suppression.py`, `us-outbound suppression load` |
| Dry-run everywhere | `guard.py`. Live needs `--live` and `live_sending = yes`, also for the scheduler's `--live` jobs |
| Signature and legitimate-interests text for Harry to approve; Instantly's unsubscribe link in every step (no postal address or privacy link, Harry 1 Oct) | `templates/copy/signature.txt`, `templates/copy/article14.txt` (drafts), `clients/instantly.py` |
| All 108 industry pages on the Industries tab; a four-email sequence per industry on the Copy tab, checked and QA'd, for Harry to approve (Harry, 30 Sep 2026) | `settings/data/`, `us-outbound settings load`; the copy desk (`enrol/copy_desk.py`, `us-outbound copy check\|preview\|qa\|draft`); `templates/copy/style.md`, `facts.md` |

To support phase 0, the build also includes the pieces the end-to-end dry run exercises:
- data cleaning
- scoring, tiers and angles
- copy rendering and copy rules
- the enrolment maths and a dry-run-safe enrol job
- the CLI, heartbeats and erase

The source modules (SPEC 7) and the reply and HubSpot-write jobs (SPEC 11) belong to phases 1 and 2 and are not built.

### Acceptance checks (automated, all passing)

| SPEC 14 check | Test |
| :- | :- |
| One test record flows end to end in dry-run | `tests/test_e2e_dry_run.py` |
| Changing a weight in the sheet changes a test account's score after the next sync | `tests/test_e2e_dry_run.py` |
| A bad row in the sheet is rejected, with a Slack message | `tests/test_e2e_dry_run.py`, `tests/test_settings_sync.py` |
| A test that inspects every client call finds no write outside the allowed containers | `tests/test_guardrails.py` |

These run against fakes. The same checks are repeated against the real systems once the setup steps below are done.

Infrastructure (Harry, 30 Sep 2026): everything Google Cloud did moves to Railway, on Spill's existing plan:
- Postgres replaces BigQuery.
- Sealed variables replace Secret Manager.
- One always-on worker with its own scheduler replaces Cloud Run Jobs and Cloud Scheduler.

Google keeps only a Sheets service account in `columbus-510209`. This departs from SPEC 1.7, 3 and 6; see [railway-setup.md](railway-setup.md).

### Setup steps (need Harry or access we don't have yet)

See `docs/phase0-runbook.md` for commands, and `docs/phase0-facts.md` for what has already been confirmed.

1. **Railway:** follow `docs/railway-setup.md`.
   - Create the project Columbus in EU West (Amsterdam), with a Postgres database and the worker service from `harrydryden/columbus` (`main`).
   - `railway ssh -- us-outbound db apply --live` creates schema `us_outbound`.
2. **Google:** in `columbus-510209`, enable the Google Sheets API only. Create the service account `us-outbound-sheets` and its JSON key. No billing is needed.
3. **Keys:** put each key in its sealed Railway variable (`railway-setup.md`, step c): Apollo, Clay, Instantly, HubSpot (service key, SPEC 13 scopes), Slack, the Sheets key, and the Claude key with a $10 monthly limit.
4. **Settings sheet:** create "US Outbound – Settings" from the defaults and share it with the service account. The phase-0 ids go in the General tab:
   - pipeline `82002613`
   - stage `154381888`
   - owner `82221891`
   - approver `U098X453UAG`
5. **Slack:** create the app from `deploy/slack-app-manifest.yaml`, then #us-outbound and #us-outbound-dev, and invite the bot.
6. **HubSpot:** `us-outbound hubspot setup --live` creates the property group and the six properties.
7. **Instantly:**
   - Confirm the plan facts.
   - `us-outbound mailbox …` / `campaigns ensure --live` sets up the registry and the three paused campaigns.
   - Test the custom-variable length limit, that follow-ups stay on the step-1 address, and whether a forward endpoint exists.
   - Confirm the step days (0, 7, 14, 21) and what the analytics and sending-status endpoints return.
8. **Clay:** create the "US Outbound" folder and the two functions (SPEC 8). Enable API access, set the budget, and try them on 20 hand-picked accounts.
9. **Apollo tracker:** change "us/pricing" to "/us/pricing", add "/us/book-demo" as high intent, then confirm data arrives and the script is not on employee-facing pages.
10. **Signature:** approve the signature and Article 14 text. No privacy page or postal address is needed (Harry, 1 Oct); the opt-out is Instantly's unsubscribe link.
11. **Suppression:** `us-outbound suppression load --live`.
12. **Mailboxes:** check domain authentication (SPF, DKIM, DMARC p=none) for meetspill.org and tryspill.org.

Open decisions are in `docs/open-questions.md`.

## Before phase 1: how accounts are found and enriched

[pipeline.md](pipeline.md) sets out which vendor does which job, where the ICP lives, the order of the funnel and which value wins when sources disagree. Its eight proposed changes need Harry's answer before the phase 1 source modules are written.
