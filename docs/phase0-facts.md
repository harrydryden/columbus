# Phase 0: plan facts

Read-only checks run on 29 Sep 2026 through the connected HubSpot, Clay, Apollo, Slack, Google Drive, Webflow and BigQuery tools. Nothing was created or changed, and no Apollo or Clay credits were spent.

Status key:
- **Confirmed**: seen directly.
- **Open**: needs Harry, or a system we can't reach yet.

## HubSpot (portal 8481055)

| Fact | Status | Value |
| :- | :- | :- |
| Pipeline "Spill 3.0" | Confirmed | id `82002613` |
| First stage | Confirmed | "Demo requested", id `154381888` (then Demo created, Demo held, Onboarding, On trial, Closed won, Closed lost, …) |
| Harry's owner id | Confirmed | `82221891` |
| `lifecyclestage = lead` and `hs_lead_status = CONNECTED` | Confirmed | Both are valid option values |
| `us_outbound_*` properties | Confirmed absent | None exist on companies or contacts |
| "US Outbound" property group | Open | Not visible through the tools; probably absent |
| Hub tier and remaining custom-property allowance | Open | Not exposed. About 114 custom contact properties and 70 custom company properties exist |
| API credential for the jobs | Open | **A service key, not a private app.** HubSpot stops new legacy private apps in existing accounts on 26 Oct 2026, and names service keys (beta) as the replacement for system-to-system use. The key is sent as `Authorization: Bearer`, like a private-app token, so the client needs no change. Service keys don't support webhooks; the jobs poll, so none are needed. Scopes in phase0-runbook.md §5 |
| API versions | Noted 30 Sep | HubSpot ends support for v1–v3 APIs in Sept 2027. The client uses CRM v3, communication preferences v4 and associations v4. v3 is fine for v1 (to 18 Dec 2026); moving to the new API versions is a phase 4 item |

## Clay (workspace "Spill", 1336346)

| Fact | Status | Value |
| :- | :- | :- |
| Existing functions | Confirmed | Work Email `t_0tk0v4lhJ895hhhhTHJ`, Company Latest Funding `t_0tk0v4ehpQ6WaeuCoQf`, Website Technology Stack `t_0tk0v4lEmZDggj3wRwB`, Website Traffic `t_0tk0v4lJQkpANSDTxf9` |
| "US Outbound" folder and functions | Open | Not created yet |
| Functions callable programmatically | Partly | Yes through Clay's MCP (a run needs a subroutine id, task id and field mapping). A REST endpoint the Python jobs can call with an API key is not yet confirmed, so `clients/clay.py` marks it PHASE0-CONFIRM and keeps the CSV fallback |
| Plan tier, monthly credits and actions, spend limits | Open | Not exposed. Harry set `clay_monthly_credits` to 2,000 a month (30 Sep) |

## Apollo (team 6a85cc72550d280018aa9e9f)

| Fact | Status | Value |
| :- | :- | :- |
| Credit cycle | Confirmed | Annual, 2026-08-21 to 2027-08-21; credits lapse or reset on 2027-08-21 |
| Lead credits | Confirmed | 30,380 limit, 30,128 left. The two Apollo tools disagree on usage (252 vs 4) |
| Direct-dial credits | Open | The two tools disagree (all used vs none used). The jobs don't use direct dials |
| Waterfall email | Confirmed | Enabled |
| Tracker 6a85cc77f43ea3001cfcd35b on spill.chat | Confirmed | Intent paths are "/us" (high) and "us/pricing" (high, **still missing the leading slash**). **"/us/book-demo" is absent.** Contact-level tracking is off. `data_received: false` |
| Visitor discovery through the API | Likely yes | Apollo's organisation lookup is described as free and takes `website_visitors_from_domains`, `website_visitors_from_past`, `website_visitors_intent` and page filters. Company search takes the same filters at 1 credit per request. To confirm with one call once Harry approves |

## Slack

| Fact | Status | Value |
| :- | :- | :- |
| #us-outbound, #us-outbound-dev | Confirmed absent | Only the archived #outboundsales matches |
| Harry's user id | Confirmed | `U098X453UAG` (the only approver) |
| Bot app | Open | To be created from `deploy/slack-app-manifest.yaml` |

## Infrastructure: Railway, and one Google service account

**Decision (Harry, 30 Sep 2026):** everything Google Cloud was to do moves to Railway, where Spill already has a paid account:

| What | Railway | Replaces |
| :- | :- | :- |
| Compute | One always-on worker service. It is built from the `Dockerfile` and runs `us-outbound scheduler`, which starts every job on the `ops/schedule.py` table | Cloud Run Jobs and Cloud Scheduler (SPEC 3) |
| Database | Railway PostgreSQL, schema `us_outbound` | BigQuery dataset `us_outbound` (SPEC 3, 6) |
| Secrets | Sealed service variables | Secret Manager (SPEC 1.7) |
| Images | Built by Railway from the repository | Artifact Registry |

An earlier decision the same day put the system in its own Google Cloud project, Columbus (`columbus-510209`, number 548271199497, in the spill.chat organization). That project now holds only a service account. The service account reads and writes the settings sheet through the Sheets API. It needs no billing. The jobs never read the warehouse project `spill-warehouse-test`.

| Fact | Status | Value |
| :- | :- | :- |
| Railway project | Not created yet | "Columbus", in Spill's workspace (docs/railway-setup.md) |
| Region (data residency) | Decided | **EU West (Amsterdam)**, `europe-west4-drams3a`, for the worker and Postgres |
| Database | Not created yet | Railway PostgreSQL; tables created by `us-outbound db apply --live` |
| Secrets | Not added yet | Sealed variables on the us-outbound service: the six keys, the Sheets key, the sheet id and `DATABASE_URL` (`${{Postgres.DATABASE_URL}}`) |
| Google project | Created | `columbus-510209` (Columbus). Only the Google Sheets API is enabled. No billing is needed |
| Sheets service account | Not created yet | `us-outbound-sheets@columbus-510209.iam.gserviceaccount.com`, with no project roles. The settings sheet is shared with it as Editor. Its JSON key goes in `US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON`. The spill.chat organization may block key creation (railway-setup.md, step d) |
| Railway config as code | Checked 30 Sep | `railway.json` and `railway.toml` are deprecated. New services cannot use them, and they stop being read on 1 Dec 2026. The Dockerfile holds the start command; the dashboard settings are listed in railway-setup.md |
| Sealed variables | Checked 30 Sep | They are not passed to `railway run` or `railway shell`. Commands run inside the worker with `railway ssh -- us-outbound …` |

## Website (Webflow site 60b75255186ee4cfc87b1cc0)

| Fact | Status | Value |
| :- | :- | :- |
| US locale | Confirmed | en-US subdirectory `/us` (locale 6a7b1a45465079bd9e1fe40f) |
| Book-demo page | Confirmed | `/us/book-demo` is live. Its SEO title (tab, Google, link previews), inherited by the US locale, reads "Spill \| The UK's Highest Rated EAP \| Book a demo" (checked 1 Oct) |
| US privacy and opt-out page | Not needed (Harry, 1 Oct) | Emails carry no privacy link and no postal address. The opt-out is Instantly's unsubscribe link in every campaign step, plus the List-Unsubscribe header. `/us/legals/privacy-notice` is still a draft |

## Not reachable from here

- **Instantly:** there is no connector and no key in this session. Still to check: the plan, email and uploaded-contact caps and current use, whether the emails, reply, forward and accounts endpoints exist, the custom-variable length limit, same-address follow-ups, and the warmup status of the four mailboxes.
- **Secrets:** they will live in sealed Railway variables. None exist yet.
