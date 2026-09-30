# Phase 0 runbook: Foundations (29 Sep – 9 Oct 2026)

The SPEC 14 phase-0 checklist, in the order to do it, with who does each step and the
command to run. "Harry" means a step in a vendor's UI or a decision; "jobs" means a
`us-outbound` command (run from a laptop or as a Cloud Run Job). Every command is dry-run
unless it says `--live`; run it dry first, read what it would do, then add `--live`.
Facts already confirmed by read-only checks are in [phase0-facts.md](phase0-facts.md).

Everything here is dry-run everywhere (SPEC 14): `live_sending` stays `no` for all of
phase 0. The setup commands below write with `--live` alone because they never reach a
prospect; see README "Dry-run and live".

## 0. Before starting

| Step | Who | How |
| :- | :- | :- |
| Local environment | Harry | `python -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/python -m pytest` |
| Google credentials | Harry | `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets` |
| Environment | Harry | `export US_OUTBOUND_PROJECT=columbus US_OUTBOUND_BQ_LOCATION=EU` (EU, like Spill's warehouse datasets) |
| **[ASK HARRY] Cloud costs** | Harry | Cloud Scheduler is free for 3 jobs per billing account, then $0.10 per job a month (phase 0 schedules 4); Secret Manager is free for 6 secret versions, then $0.06 each a month (7 secrets); Artifact Registry is free to 0.5 GB. SPEC 1.1 allows no new spend without asking. |

## 1. Plan facts (SPEC 14 "Confirm plan facts")

| Fact | Who | Status / how |
| :- | :- | :- |
| Clay tier, monthly credits and actions; functions creatable and callable through the API; spend limits | Harry | Open. Set `clay_monthly_credits` on the General tab once known (0 means no Clay calls). |
| Instantly plan; email and uploaded-contact caps and what other campaigns use; emails, reply and accounts endpoints included | Harry | Open (no Instantly access from the build). |
| HubSpot tier and remaining custom-property allowance; "Spill 3.0" pipeline and stage ids | Harry / jobs | Ids confirmed (phase0-facts.md); `us-outbound hubspot ids` prints them again. Allowance open. |
| Apollo: when credits lapse; visitor discovery through the API | Harry | Credits lapse 2027-08-21. Discovery: one approved call to confirm. |
| Apollo tracker fixes (SPEC 7) | Harry | Change "us/pricing" to "/us/pricing"; add "/us/book-demo" as high intent; confirm data arrives; confirm the script is not on employee-facing pages. |

## 2. Repository and infrastructure

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| BigQuery dataset and tables | jobs | `us-outbound bq apply` (prints the DDL), then `us-outbound bq apply --live` | `bq ls us_outbound` lists the tables and views |
| Service account, roles, empty secrets, image repository | Harry | `DRY_RUN=1 PROJECT=columbus REGION=europe-west2 deploy/setup.sh`, then without `DRY_RUN` | The script ends with "Done" |
| Secret values | Harry | For each secret: `printf '%s' "$VALUE" \| gcloud secrets versions add NAME --data-file=- --project columbus`. Never in the repo, a shell history file or BigQuery. | `gcloud secrets versions list NAME` shows one version |

Secrets (SPEC 13): `us-outbound-apollo-api-key` (master key), `us-outbound-clay-api-key`,
`us-outbound-instantly-api-key`, `us-outbound-hubspot-token`, `us-outbound-slack-bot-token`,
`us-outbound-claude-api-key` (the new key, with a $10 monthly limit set in the Anthropic
console), and `us-outbound-google-service-account` (not read on Cloud Run, where the jobs run
as the service account; leave it empty unless Harry decides otherwise).

## 3. Slack

| Step | Who | How |
| :- | :- | :- |
| The app | Harry | api.slack.com/apps → From an app manifest → `deploy/slack-app-manifest.yaml`; install; bot token into `us-outbound-slack-bot-token` |
| Channels | Harry | Create `#us-outbound` and `#us-outbound-dev`; invite the bot to both |
| Approver | Harry | `approver_slack_ids` on the General tab: `U098X453UAG` (Harry, the only approver) |

## 4. Settings sheet

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Create "US Outbound – Settings" with the SPEC 5 defaults | jobs | `us-outbound settings bootstrap --live` (prints the new sheet id) | The sheet has ten tabs |
| Point the jobs at it | Harry | `export US_OUTBOUND_SETTINGS_SHEET_ID=<id>`; share the sheet with `us-outbound@columbus.iam.gserviceaccount.com` as Editor | |
| First sync | jobs | `us-outbound settings sync` (dry-run still writes BigQuery; errors go to `#us-outbound-dev`) | `us-outbound status` shows "Settings synced" |
| HubSpot ids on the General tab | Harry | `us-outbound hubspot ids`, then paste `hubspot_pipeline_id`, `hubspot_deal_stage_id`, `hubspot_owner_id` into the General tab (never written automatically) | Next sync carries them |

## 5. HubSpot

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Private-app token (before 26 Oct) | Harry | Scopes (SPEC 13): read and write contacts, companies, deals, lists; read owners, meetings, pipelines; write notes, tasks and communication preferences; schema write for the property group | Token in `us-outbound-hubspot-token` |
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
| HubSpot opt-outs and hard bounces, hashed | jobs | `us-outbound suppression load` (BigQuery only, so dry-run is enough) | Its summary shows how many were added; a second run adds 0 |

## 10. Deploy

| Step | Who | How | Done when |
| :- | :- | :- | :- |
| Review | Harry | Read the diff (SPEC 13: Harry reviews before each deploy) | |
| Build and deploy | Harry | `DRY_RUN=1 PROJECT=columbus REGION=europe-west2 SETTINGS_SHEET_ID=<id> deploy/deploy.sh`, then without `DRY_RUN` | `gcloud run jobs list` shows settings_sync, score, mailbox_health, heartbeat_check and suppression_load |
| Heartbeats | jobs | After a day: `us-outbound status` | Every phase-0 job shows ok; `heartbeat_check` alerts `#us-outbound-dev` on a missed one |

Scheduled in phase 0 (UK time): settings_sync 02:00, suppression_load 01:30, mailbox_health
07:00, heartbeat_check hourly at :05. Later-phase jobs are `disabled: true` in
`deploy/jobs.yaml` until their phase.

## 11. For Harry to approve (SPEC 14)

| Item | Where |
| :- | :- |
| Footer text (sender, postal address, advertisement line, "Reply STOP or use this link to opt out", privacy link) | `templates/copy/footer.txt`; `postal_address` on the General tab |
| Privacy page link | `privacy_url` on the General tab. The US privacy notice is still a draft and there is no opt-out page (phase0-facts.md): sends stay blocked until it is live |
| Legitimate-interests text (UK GDPR Article 14, step 1) | `templates/copy/article14.txt` |

## Acceptance (SPEC 14)

| Check | How |
| :- | :- |
| One test record flows end to end in dry-run | The end-to-end dry-run test in `tests/`, and one hand-made account taken through `us-outbound rescore` and the phase-0 jobs; BigQuery has its rows, `#us-outbound-dev` has the posts, HubSpot and Instantly have nothing new |
| Changing a weight in the sheet changes a test account's score after the next sync | Edit one Signals weight; `us-outbound settings sync`; the account's `score` in `accounts` changes |
| A bad row in the sheet is rejected, with a Slack message | Put `weight = ten` on a Signals row; `us-outbound settings sync`; the tab keeps its previous version and `#us-outbound-dev` has the error |
| A test that inspects every client call finds no write outside the allowed containers | `.venv/bin/python -m pytest`: the guard records every call (`Guard.calls`), and the tests assert no write left the allowed containers |
| Kill switch | `us-outbound stop --live` pauses every US Outbound campaign and stops enrollment; `us-outbound status` shows it |
| Erasure | `us-outbound erase --email <seed address>` shows the per-system report and Clay's manual step |
