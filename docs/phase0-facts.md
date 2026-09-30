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
| Private-app token for the jobs | Open | To be created by Harry before 26 Oct, with the SPEC 13 scopes |

## Clay (workspace "Spill", 1336346)

| Fact | Status | Value |
| :- | :- | :- |
| Existing functions | Confirmed | Work Email `t_0tk0v4lhJ895hhhhTHJ`, Company Latest Funding `t_0tk0v4ehpQ6WaeuCoQf`, Website Technology Stack `t_0tk0v4lEmZDggj3wRwB`, Website Traffic `t_0tk0v4lJQkpANSDTxf9` |
| "US Outbound" folder and functions | Open | Not created yet |
| Functions callable programmatically | Partly | Yes through Clay's MCP (a run needs a subroutine id, task id and field mapping). A REST endpoint the Python jobs can call with an API key is not yet confirmed, so `clients/clay.py` marks it PHASE0-CONFIRM and keeps the CSV fallback |
| Plan tier, monthly credits and actions, spend limits | Open | Not exposed. `clay_monthly_credits` stays 0 (no Clay calls) until Harry gives the pool |

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

## Google Cloud / BigQuery

**Decision (Harry, 30 Sep):** the system runs in its own Google Cloud project, "Columbus" (id `columbus-510209`, number 548271199497, in the spill.chat organization), separate from the warehouse project `spill-warehouse-test`. SPEC 3 said "Spill's existing Google Cloud project". A separate project keeps the outbound system's service account, secrets and costs apart from the warehouse. The jobs never read the warehouse datasets.

| Fact | Status | Value |
| :- | :- | :- |
| Project | Created | `columbus-510209` (display name Columbus), in the spill.chat organization. Check that it is linked to Spill's billing account |
| Dataset location | Decided | **EU**, like the warehouse's 24 datasets (SPEC 6) |
| Region for Cloud Run, Scheduler, secrets and registry | Default | `europe-west2` (London) |
| Dataset us_outbound | Not created yet | Created by `us-outbound bq apply` |
| Service account | Not created yet | `us-outbound@columbus-510209.iam.gserviceaccount.com`, created by `deploy/setup.sh`. The settings sheet is shared with it |

## Website (Webflow site 60b75255186ee4cfc87b1cc0)

| Fact | Status | Value |
| :- | :- | :- |
| US locale | Confirmed | en-US subdirectory `/us` (locale 6a7b1a45465079bd9e1fe40f) |
| Book-demo page | Confirmed | `/us/book-demo` is live. Its US SEO title still reads "The UK's Highest Rated EAP" |
| US privacy and opt-out page | **Open: blocks sending** | `/us/legals/privacy-notice` exists only as a draft, and there is no opt-out page. `privacy_url` must point at a live page before any send |

## Not reachable from here

- **Instantly:** there is no connector and no key in this session. Still to check: the plan, email and uploaded-contact caps and current use, whether the emails, reply, forward and accounts endpoints exist, the custom-variable length limit, same-address follow-ups, and the warmup status of the four mailboxes.
- **Secrets:** they will live in Secret Manager. None exist yet.
