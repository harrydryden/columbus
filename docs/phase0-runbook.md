# Phase 0 runbook: Foundations (29 Sep – 9 Oct 2026)

The SPEC 14 phase-0 checklist, in the order to do it, with who does each step and the
command to run. "Harry" means a step in a vendor's UI or a decision. "jobs" means a
`us-outbound` command, run inside the Railway worker as `railway ssh -- us-outbound …`
(docs/railway-setup.md, step e). The worker is where the sealed variables and the private
database address are available. Every command is dry-run unless it says `--live`: run it
dry first, read what it would do, then add `--live`.
Facts already confirmed by read-only checks are in [phase0-facts.md](phase0-facts.md).

Infrastructure is on Railway (Harry, 30 Sep 2026): a Postgres database, one always-on
worker whose scheduler starts every job, and sealed variables for the keys. Google keeps
only a Sheets service account in `columbus-510209`. Set it up first with
[railway-setup.md](railway-setup.md).

Everything here is dry-run everywhere (SPEC 14): `live_sending` stays `no` for all of
phase 0. The setup commands below write with `--live` alone because they never reach a
prospect; see README "Dry-run and live".

## 0. Before starting

| Step | Who | How |
| :- | :- | :- |
| Local environment (tests only) | Harry | `python -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/python -m pytest` |
| Railway project, database, worker and variables | Harry | [railway-setup.md](railway-setup.md) steps a–c. Region EU West (Amsterdam); every key in a sealed variable |
| Sheets service account | Harry | [railway-setup.md](railway-setup.md) step d. Project `columbus-510209`, Google Sheets API only, no billing |
| Railway CLI | Harry | `brew install railway`, `railway login`, `railway link` (Columbus → us-outbound) |
| Costs | Harry | Spill's existing Railway plan (about $3–5 a month of usage for the worker and Postgres); no Google Cloud costs (railway-setup.md, step g) |

## 1. Plan facts (SPEC 14 "Confirm plan facts")

| Fact | Who | Status / how |
| :- | :- | :- |
| Clay tier, monthly credits and actions; functions creatable and callable through the API; spend limits | Harry | Open. Set `clay_weekly_credits` on the General tab once known (0 means no Clay calls; budgets run Monday to Sunday, UK time). |
| Instantly plan; email and uploaded-contact caps and what other campaigns use; emails, reply and accounts endpoints included | Harry | Open (no Instantly access from the build). |
| HubSpot tier and remaining custom-property allowance; "Spill 3.0" pipeline and stage ids | Harry / jobs | Ids confirmed (phase0-facts.md); `us-outbound hubspot ids` prints them again. Allowance open. |
| Apollo: when credits lapse; visitor discovery through the API | Harry | Credits lapse 2027-08-21. Discovery: one approved call to confirm. |
| Apollo tracker fixes (SPEC 7) | Harry | Change "us/pricing" to "/us/pricing"; add "/us/book-demo" as high intent; confirm data arrives; confirm the script is not on employee-facing pages. |

## 2. Repository and infrastructure

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Tables and views (schema `us_outbound`) | jobs | `us-outbound db apply` (prints the DDL), then `us-outbound db apply --live` | `us-outbound status` runs without a database error |
| Keys | Harry | Each one in its sealed variable on the us-outbound service (railway-setup.md, step c). Never in the repo, a chat, a shell history file or the database. | `us-outbound status` works; a job with a missing key says `set <VARIABLE>` |
| The scheduler | Railway | The worker starts `us-outbound scheduler` on each deploy (the Dockerfile's default command) | Its log shows `scheduler_start` with the four phase-0 jobs |

Key variables (SPEC 13): `US_OUTBOUND_APOLLO_API_KEY` (master key), `US_OUTBOUND_CLAY_API_KEY`,
`US_OUTBOUND_INSTANTLY_API_KEY`, `US_OUTBOUND_HUBSPOT_TOKEN`, `US_OUTBOUND_SLACK_BOT_TOKEN`,
`US_OUTBOUND_CLAUDE_API_KEY` (the new key, with a $10 monthly limit set in the Anthropic
console), and `US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON` (the Sheets service account's JSON key).

## 3. Slack

| Step | Who | How |
| :- | :- | :- |
| The app | Harry | api.slack.com/apps → From an app manifest → `deploy/slack-app-manifest.yaml`; install; bot token into the sealed variable `US_OUTBOUND_SLACK_BOT_TOKEN` |
| Channels | Harry | Create `#us-outbound` and `#us-outbound-dev`; invite the bot to both |
| Approver | Harry | `approver_slack_ids` on the General tab: `U098X453UAG` (Harry, the only approver) |

## 4. Settings sheet

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Create "US Outbound – Settings" with the SPEC 5 defaults | jobs | `us-outbound settings bootstrap --live` (prints the new sheet id) | The sheet has ten tabs |
| Point the jobs at it | Harry | Put the id in the sealed variable `US_OUTBOUND_SETTINGS_SHEET_ID` and deploy; share the sheet with `us-outbound-sheets@columbus-510209.iam.gserviceaccount.com` as Editor (open question 8: who owns the sheet) | |
| First sync | jobs | `us-outbound settings sync` (dry-run still writes the database; errors go to `#us-outbound-dev`) | `us-outbound status` shows "Settings synced" |
| HubSpot ids on the General tab | Harry | `us-outbound hubspot ids`, then paste `hubspot_pipeline_id`, `hubspot_deal_stage_id`, `hubspot_owner_id` into the General tab (never written automatically) | Next sync carries them |

## 5. HubSpot

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Service key (replaces the SPEC's private-app token; see phase0-facts.md) | Harry (Super Admin) | HubSpot → Development → Keys → Service keys → Create service key, named "US Outbound". Scopes (SPEC 13): `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.companies.read`, `crm.objects.companies.write`, `crm.objects.deals.read`, `crm.objects.deals.write` (deals read also covers deal pipelines), `crm.lists.read`, `crm.lists.write`, `crm.objects.owners.read`, `crm.schemas.contacts.read`, `crm.schemas.contacts.write`, `crm.schemas.companies.read`, `crm.schemas.companies.write`, `communication_preferences.read_write`. If the picker lists separate meetings, notes or tasks scopes, tick meetings read and notes and tasks write; otherwise the contacts scopes cover them. Nothing else: no settings, users, files, marketing or content scopes | Key in the sealed variable `US_OUTBOUND_HUBSPOT_TOKEN`, never in chat |
| The group "US Outbound" and the six properties | jobs | `us-outbound hubspot setup`, then `us-outbound hubspot setup --live`. It creates only what is missing and stops, creating nothing, if one of the six names exists with another type. | Companies: `us_outbound_account_id` (unique), `us_outbound_tier`, `us_outbound_industry_group`, `us_outbound_top_signals`; contacts: `us_outbound_angle`, `us_outbound_reply_class` |
| Apollo–HubSpot native sync stays off | Harry | Check it in Apollo's integrations | |

## 6. Mailboxes and domains

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Warmup status of the four mailboxes | jobs | `us-outbound mailbox check` (posts to `#us-outbound-dev`), then `us-outbound mailbox check --live` to set each status on the sheet: Active if Instantly shows it warm, otherwise Warming (promoted after 21 days) | Mailboxes tab shows each status |
| Domain checks for meetspill.org and tryspill.org (SPEC 13) | Harry | Blacklists and prior use; registered in Spill's name; redirects to spill.chat/us; SPF, DKIM, and DMARC at p=none with reporting | Recorded in phase0-facts.md |
| Seed inboxes | Harry | Four: 2 Google, 2 Microsoft 365 | |

Later mailboxes: `us-outbound mailbox add <address> --owner "Full Name" --live` (connect it in
Instantly first), `mailbox pause <address> --live`, `mailbox retire <address> --live`.

## 7. Instantly

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| The three sender campaigns, paused | jobs | `us-outbound campaigns ensure`, then `--live`. Creates "US Outbound – Hannah Spalding", "US Outbound – Sam Jackson" and "US Outbound – Harry Dryden" (Harry's two addresses) with the SPEC 9 settings, in Draft; nothing activates them but `start`. An owner with no Active mailbox yet waits. | Three campaigns in Draft; `campaigns ensure` reports no drift |
| Custom-variable length limit | Harry | Add one test lead by hand with a long `s1_body`; record the longest value kept intact | `CUSTOM_VARIABLE_LIMIT` set in `clients/instantly.py` |
| Follow-ups stay on the step-1 address | Harry | Two seed leads in Harry's campaign; check steps 2 to 4 come from the step-1 address | Noted in phase0-facts.md |
| Forward endpoint | Harry | Check `POST /emails/forward` works on our plan | Noted in phase0-facts.md |
| Tracking off, workspace untouched | Harry | Open and link tracking off on the three campaigns; no workspace setting changed | |

## 8. Clay

| Step | Who | How |
| :- | :- | :- |
| The folder "US Outbound" and the two functions | Harry | "US Outbound – Accounts" and "US Outbound – Contacts" (SPEC 8), with "API & CLI" ticked; the existing functions called as they are, never modified |
| Function ids on the General tab | Harry | `clay_accounts_function_id`, `clay_contacts_function_id` |
| Tried on 20 hand-picked accounts | Harry and jobs | Check the strict JSON; record credits per account |
| Workbook spend limit | Harry | If the plan offers one |

## 9. Suppression

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| HubSpot opt-outs and hard bounces, hashed | jobs | `us-outbound suppression load` (it writes only the database, so dry-run is enough) | Its summary shows how many were added; a second run adds 0 |

## 10. Deploy

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Review | Harry | Read the diff before merging to `main` (SPEC 13: Harry reviews before each deploy) | |
| Deploy | Railway | Merging to `main` builds the Dockerfile and redeploys the worker. The old worker gets SIGTERM and 30 s to finish (`RAILWAY_DEPLOYMENT_DRAINING_SECONDS`) | The service's Deployments tab shows the new deploy as active |
| Schedule | jobs | `us-outbound schedule` | settings_sync, mailbox_health, heartbeat_check and suppression_load show a next run; later-phase jobs show "disabled" |
| Heartbeats | jobs | After a day: `us-outbound status` | Every phase-0 job shows ok; `heartbeat_check` alerts `#us-outbound-dev` on a missed one |

Scheduled in phase 0 (UK time): settings_sync 02:00, suppression_load 01:30, mailbox_health
07:00, heartbeat_check hourly at :05. Later-phase jobs have `enabled=False` in
`us_outbound/ops/schedule.py` until their phase; turning one on is a reviewed code change.

## 11. For Harry to approve (SPEC 14)

| Item | Where |
| :- | :- |
| Footer text (sender, postal address, advertisement line, "Reply STOP or use this link to opt out", privacy link) | `templates/copy/footer.txt`; `postal_address` on the General tab |
| Privacy page link | `privacy_url` on the General tab. The US privacy notice is still a draft and there is no opt-out page (phase0-facts.md): sends stay blocked until it is live |
| Legitimate-interests text (UK GDPR Article 14, step 1) | `templates/copy/article14.txt` |

## Acceptance (SPEC 14)

| Check | How |
| :- | :- |
| One test record flows end to end in dry-run | The end-to-end dry-run test in `tests/`, and one hand-made account taken through `us-outbound rescore` and the phase-0 jobs; the database has its rows, `#us-outbound-dev` has the posts, HubSpot and Instantly have nothing new |
| Changing a weight in the sheet changes a test account's score after the next sync | Edit one Signals weight; `us-outbound settings sync`; the account's `score` in `accounts` changes |
| A bad row in the sheet is rejected, with a Slack message | Put `weight = ten` on a Signals row; `us-outbound settings sync`; the tab keeps its previous version and `#us-outbound-dev` has the error |
| A test that inspects every client call finds no write outside the allowed containers | `.venv/bin/python -m pytest`: the guard records every call (`Guard.calls`), and the tests assert no write left the allowed containers |
| Kill switch | `us-outbound stop --live` pauses every US Outbound campaign and stops enrollment; `us-outbound status` shows it |
| Erasure | `us-outbound erase --email <seed address>` shows the per-system report and Clay's manual step |
