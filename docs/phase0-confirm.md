# PHASE0-CONFIRM: what the live system has confirmed, and how to settle the rest

Written 7 Oct 2026 (roadmap §4, week 1, item 1). The code marked 116 API details `PHASE0-CONFIRM`: endpoint shapes,
field names, codes and costs taken from vendor docs that the build machine could not check against the live APIs.
This page lists every one, with its status and the evidence for it, gates first. The ids match
`us-outbound phase0 check` (`us_outbound/ops/phase0.py`), which settles the Probe and Seed probe items; how to run it
and what to paste back is in [phase0-runbook.md](phase0-runbook.md#12-the-phase0-confirm-check-7-oct-2026).

| Status | Markers | What it means |
| :- | -: | :- |
| Confirmed live | 37 | A live run already showed it (a log line, a commit, the seed test, or a read through the HubSpot connector). The tag is gone from the code, replaced by "Confirmed live <date>: how". Three keep a tagged remainder: the unsubscribe link in a text-only email, the lead codes not seen yet, and the List-Unsubscribe header's own -2 |
| Probe | 39 | A read-only call settles it: `phase0 check --live` (three need `--apollo-credits`, 1 credit each; the Clay items need `clay check-email --live`) |
| Seed probe | 9 | A write on Harry's own seed lead settles it: `phase0 check --seed ADDRESS --live` |
| Watch | 22 | Only production can show it. The code handles either answer; the row names the log line that settles it |
| Moot | 9 | The path is unused, already past, or not an API detail; the comment now says so |
| **All** | **116** | |

Paths are under `us_outbound/`; line numbers are as of this page's commit. Where several markers settle together they
share an id. `grep -rn --include=*.py PHASE0-CONFIRM us_outbound | grep -v ops/phase0.py` lists the 74 markers still
open.

## The gates: sending, opt-outs and stops

What roadmap §4 week 1 item 1 put first, and what else would let an email go out, or fail to stop one, if it were
wrong.

| Gate | Id | Status | Evidence, or how it is settled |
| :- | :- | :- | :- |
| Instantly's unsubscribe link (the placeholder, not `{{unsubscribe}}`) | `INST-UNSUB-LINK` | Confirmed live | Confirmed live 6 Oct by the seed test (below); the text-only form waits until `email_format` is text |
| A click sets the lead to -2, which `sync_outcomes` reads | `INST-LEAD-UNSUB` | Confirmed live | Confirmed live 6 Oct; `phase0 check` re-reads the unsubscribed seed lead |
| Pausing a lead and setting it going again (`LEAD_PAUSE_CONFIRMED`: the out-of-office pause, the account stop's pause) | `SEED-PAUSE`, `SEED-RESUME` | Seed probe | `--seed` on a seed lead still in its sequence, then a run after its next step's day. `LEAD_PAUSE_CONFIRMED` stays False until both say CONFIRMED |
| A booking stops the account's leads (`stop_lead`, "Meeting booked") | `SEED-INTEREST` | Seed probe | `--seed` on a finished seed lead; "no further steps" then Watch |
| The forward endpoint (escalations) | `SEED-FORWARD` | Seed probe | `--seed`: one forward to escalation_email |
| The reply endpoint: a reply carries its campaign_id | `INST-REPLY-CAMPAIGN` | Probe | Reply to a seed email from its inbox, then `phase0 check --live` |
| The reply endpoint: reply_to_uuid | `INST-REPLY-TO` | Watch | The first approved reply (`desk_sent`) |
| Replies Instantly flags as automatic (`stop_on_auto_reply` is off by design; the jobs read the flag) | `INST-AUTO-REPLY` | Probe | `phase0 check --live`, once an email has been received |
| The campaigns are found by name | `INST-SEARCH-DASH` | Confirmed live | Confirmed live 3 Oct |
| Sending days Monday to Friday | `INST-SCHEDULE-DAYS` | Confirmed live | Confirmed live 5 Oct (the Monday seed sends) |
| Follow-ups 7 days apart | `INST-STEP-DELAYS` | Watch | Email 2 of the 5 Oct seeds on Mon 12 Oct |
| A full plan keeps contacts | `INST-PLAN-FULL` | Watch | `instantly_plan_full` the first time it fills |
| An empty campaign goes "completed"; the next add resumes it | `INST-EVERGREEN` | Watch | Seen 6 Oct and handled; `campaign_resumed` on the first add |
| The ramp's daily limit counts campaign emails only | `INST-DAILY-LIMIT` | Watch | `sent_by_day` from 12 Oct |
| An address already blocklisted is still opted out | `INST-BLOCKLIST-DUP` | Watch | `opt_out_pending` if it happens |
| Kill rules: what Instantly reports for bounces, complaints and account health | `INST-BOUNCE-REPORT`, `INST-VITALS` | Watch | The first bounce and the first unhealthy account |
| No email to a company with an open deal (verify, enrol) | `HS-DEALS-ASSOC` | Probe | `phase0 check --live` |
| No email to a domain with an opted-out contact | `HS-EMAIL-DOMAIN` | Confirmed live | Confirmed live 2 Oct |
| A booking through the meetings link is seen (and stops the leads) | `HS-MEETING-SOURCE` | Confirmed live | Confirmed 7 Oct |
| A booking through spill.chat/us/book-demo is seen | `HS-DEMO-DEAL` | Watch | The first US booking |

**The seed test of 5 and 6 Oct**, which confirmed the opt-out. The step template took Instantly's placeholder on 5 Oct
(commit 80b283d), after `{{unsubscribe}}` went out as `href=""`; the morning check of 7 Oct found no drift. A seed
email went out at about 13:30 UTC on 6 Oct (`sync_outcomes`: one "sent: no contact of ours"), and its hourly lead
read at 14:37 counted a lead outside our contacts at status -1 or -2 ("lead: no contact of ours", counted only for
those two). `enrol` was held at 11:00 UTC on 6 Oct for `optout_tested = no` and posted 14 send approvals at 11:00 on
7 Oct, so Harry had set `optout_tested = yes`, which the runbook has him do only after `seed check` says PASS (status
-2); he had caught the empty link of 5 Oct the same way. The evidence is Railway's logs, read-only; no write was
made to any service in writing this page.

## Every marker, by vendor

Gates first within each vendor, then by status (confirmed, seed probe, probe, watch, moot).

### Instantly (45: 13 confirmed live, 8 seed probe, 8 probe, 14 watch, 2 moot)

| Id | Where | What | Status | Evidence, or how it is settled |
| :- | :- | :- | :- | :- |
| `INST-EVERGREEN` (gate) | `clients/instantly.py:62` | is_evergreen is never returned by GET /campaigns/{id} | Confirmed live | 2 Oct: `campaigns show` (commit 3f53d77, 889410a; docs/phase0-facts.md "Settings in GET"). |
| `INST-LEAD-UNSUB` (gate) | `clients/instantly.py:157` | Lead status codes, and an unsubscribe click sets -2 | Confirmed live | -2 on a click: as INST-UNSUB-LINK (6 Oct), and `phase0 check` re-reads it (INST-LEAD-UNSUB). Still tagged: 2 (SEED-PAUSE), 3, -1 and -3 (Watch: the first completed lead and the first bounce, `sync_outcomes` `bounced`), and the List-Unsubscribe header's own -2 (Watch: an unsubscribe with no click is indistinguishable in the logs). |
| `INST-LEAD-UNSUB` (gate) | `ops/golive.py:46` | golive's opt-out line | Confirmed live | As INST-LEAD-UNSUB. |
| `INST-LEAD-UNSUB` (gate) | `ops/seed.py:7` | `seed check`: -2 is the pass | Confirmed live | As INST-LEAD-UNSUB. |
| `INST-LEAD-UNSUB` (gate) | `replies/outcomes.py:27` | A click (and the List-Unsubscribe header) marks the lead | Confirmed live | The click: as INST-LEAD-UNSUB. The header keeps its marker (Watch). |
| `INST-SCHEDULE-DAYS` (gate) | `clients/instantly.py:255` | The schedule's day keys run 0 = Sunday | Confirmed live | The seed emails went out on Monday 5 Oct, from about 12:30 ET (`sync_outcomes` saw them at 16:37 UTC; mailbox_health `sent_by_day` 2 each on 2026-10-05). Monday is key "1" in what we send; under 0 = Monday it would be off. |
| `INST-SEARCH-DASH` (gate) | `clients/instantly.py:35` | The campaign name search matches the en dash in "US Outbound –" | Confirmed live | 3 Oct: `ensure_campaigns` (live) listed both campaigns it had created on 2 Oct as `ok`, not missing; 7 Oct: all four owners' campaigns `ok`. A search that missed the dash would find none and create them again. |
| `INST-UNSUB-LINK` (gate) | `clients/instantly.py:71` | The placeholder https://UNSUBSCRIBE_INSTANTLY.ai is swapped for the lead's own link | Confirmed live | Harry's seed test of 6 Oct, in the HTML anchor. The template took the placeholder on 5 Oct (commit 80b283d; the morning check of 7 Oct read it in every campaign's steps). A seed email went out at about 13:30 UTC on 6 Oct, and the hourly lead read at 14:37 counted a lead outside our contacts at status -1 or -2 (`sync_outcomes`: `lead: no contact of ours`, counted only for those two). enrol was held for `optout_tested = no` at 11:00 UTC on 6 Oct and posted 14 cards at 11:00 on 7 Oct, so Harry had set `optout_tested = yes`, which he does once the link works and `seed check` says PASS (-2); he caught the empty link of 5 Oct the same way. The text-only form keeps its marker: it matters only if `email_format` becomes text (a seed send in text format first). |
| `SEED-FORWARD` (gate) | `clients/instantly.py:950` | The forward endpoint works on our plan | Seed probe | SEED-FORWARD forwards the seed lead's email 1 to escalation_email only. Until then a failed forward falls back to a HubSpot task and a Slack DM (replies/desk.py). |
| `SEED-FORWARD` (gate) | `replies/desk.py:38` | Escalation by forward | Seed probe | As SEED-FORWARD. |
| `SEED-FORWARD` (gate) | `replies/desk.py:623` | The forward's fallback | Seed probe | As SEED-FORWARD. |
| `SEED-INTEREST` (gate) | `clients/instantly.py:166` | INTEREST_MEETING_BOOKED is 2 and a lead so marked gets no further steps | Seed probe | SEED-INTEREST on a finished seed lead: marks it 2, reads `lt_interest_status` back, sets it back. "No further steps" stays Watch: the first booking (`hubspot_readback` `meeting_booked`) followed by no `sent` event for that contact; if one comes, `delete_lead` is the stop that is certain. |
| `SEED-INTEREST` (gate) | `clients/instantly.py:812` | stop_lead: the endpoint, its fields, and no further steps | Seed probe | As SEED-INTEREST above. |
| `SEED-PAUSE` (gate) | `clients/instantly.py:161` | LEAD_PAUSE_CONFIRMED: PATCH takes status 2 and 1, and a lead set active again goes on | Seed probe | `phase0 check --seed ADDRESS --live` on a seed lead still in its sequence: SEED-PAUSE (pause, read back, set active, read back), then SEED-RESUME on a later run (its next step went out). Both CONFIRMED: set `LEAD_PAUSE_CONFIRMED = True`, which turns on the out-of-office pause (replies/poll.py) and the account stop's pause instead of delete (replies/account_stop.py). |
| `SEED-PAUSE` (gate) | `clients/instantly.py:794` | set_lead_paused | Seed probe | As SEED-PAUSE above. |
| `SEED-PAUSE` (gate) | `replies/account_stop.py:19` | A deleted lead leaves Instantly before 31 days | Seed probe | Goes with LEAD_PAUSE_CONFIRMED: once SEED-PAUSE and SEED-RESUME confirm, the stop pauses instead of deleting and this no longer applies. Until then Instantly's own unsubscribe list holds a later click. |
| `INST-AUTO-REPLY` (gate) | `replies/outcomes.py:168` | is_auto_reply and its values | Probe | INST-AUTO-REPLY, once any email has been received. If Instantly gives none, away messages go to poll_replies' classifier as now. |
| `INST-REPLY-CAMPAIGN` (gate) | `clients/instantly.py:917` | A reply carries campaign_id; reply_to_uuid is the id of the email answered | Probe | INST-REPLY-CAMPAIGN reads a reply from a seed inbox (Harry replies to a seed email first). reply_to_uuid stays Watch (INST-REPLY-TO): the first approved reply, log `desk_sent` or `desk_send_failed`. |
| `INST-BLOCKLIST-DUP` (gate) | `replies/optout.py:40` | What the blocklist answers for an address already on it | Watch | Any other status is reported ("Instantly blocklist: HTTP N") and the opt-out stays pending for a retry; the suppression and HubSpot steps still run. Settles on the first such opt-out: log `opt_out_pending`. |
| `INST-BOUNCE-REPORT` (gate) | `learn/kill_rules.py:8` | What Instantly reports for bounces and complaints | Watch | No bounce yet (`kill_rules` `checked`: 0 sends, 0 bounces on 7 Oct). The first bounce shows in `sync_outcomes` `bounced`; the bounce reason is not kept yet (system review, week 2). |
| `INST-DAILY-LIMIT` (gate) | `registry/ramp.py:28` | An account's daily_limit counts every campaign email, not warmup | Watch | Settles once follow-ups go out (from 12 Oct): mailbox_health's `sent_by_day` never above the cap while warmup runs on; a day above it means warmup or another campaign counts. |
| `INST-EVERGREEN` (gate) | `clients/instantly.py:57` | is_evergreen keeps the campaign open to new leads | Watch | 6 Oct (commit a4c5c27): with is_evergreen sent, Instantly still marked the two empty active campaigns `completed` straight after `start`. The code handles it: `enrol/capacity.resume_if_completed` activates a completed campaign after a lead goes in. Settles on the first add into a completed campaign: log `campaign_resumed` (works) or `campaign_resume_failed` (does not). |
| `INST-PLAN-FULL` (gate) | `clients/instantly.py:133` | What /leads/add answers when the plan is full | Watch | The code handles each form: `remaining_in_plan` 0, a non-"success" status naming the limit (`plan_full`), or a 402 / plan-worded 4xx (`plan_full_error`); each keeps the contact and holds the card. Settles the first time the plan fills: log `instantly_plan_full`, or a send approval held with "Instantly plan limit". |
| `INST-PLAN-FULL` (gate) | `clients/instantly.py:187` | plan_full: the status words | Watch | As INST-PLAN-FULL above. |
| `INST-PLAN-FULL` (gate) | `clients/instantly.py:202` | plan_full_error: a full plan as an HTTP error | Watch | As INST-PLAN-FULL above. |
| `INST-REPLY-TO` (gate) | `replies/items.py:141` | The reply-to id | Watch | The first approved reply: `desk_sent`, or `desk_send_failed` with the status. |
| `INST-STEP-DELAYS` (gate) | `clients/instantly.py:277` | A step's delay is the wait before the next email (7, 7, 7, 0) | Watch | Partly seen: email 1 went out the day the seed lead was added (5 Oct) though step 1's delay is 7, so it is not the wait before step 1. The spacing shows on Mon 12 Oct, when the 5 Oct seeds should get email 2 (seed inbox; `seed check`'s last emailed time); emails 2 and 3 together would mean the other reading. |
| `INST-VITALS` (gate) | `learn/kill_rules.py:28` | The account states that mean error or a banned warmup | Watch | The vitals read works (`kill_rules` `vitals_error` null hourly since 5 Oct); only healthy states seen. The first error state fires the `vitals` rule with its state in the alert. |
| `INST-DAILY-SENDS` | `clients/instantly.py:508` | GET /accounts/analytics/daily: the emails parameter and the row fields | Confirmed live | mailbox_health 6 Oct: `sent_by_day` {hannah: {2026-10-05: 2}, sam: {2026-10-05: 2}}, the seed sends, on the right mailboxes and day. |
| `INST-SENDER-GET` | `clients/instantly.py:482` | The account GET returns first_name and last_name | Confirmed live | mailbox_health (live, 6 and 7 Oct): `name_drift` {} on every mailbox; a missing field reads as "" and would be drift. |
| `INST-STEP-HTML` | `registry/mailboxes.py:234` | Step bodies come back as HTML | Confirmed live | 3 Oct: `campaigns show` read the steps back as `<div>{{sN_body}}</div><p>…` (commit 889410a); drift compares text and links since 5 Oct (80b283d). |
| `INST-WARM` | `registry/mailboxes.py:41` | What "warm" reads from Instantly | Confirmed live | mailbox_health (live) promoted hannah@ and sam@ on 2 Oct from the accounts' warmup status and score; the morning checks of 4 and 5 Oct found Harry's two warm the same way (`promoted`). |
| `INST-WARM` | `registry/mailboxes.py:99` | The 0–100 score that counts as warm | Confirmed live | The score is read live since 2 Oct. 90 itself is Harry's threshold (open question 51), not an API detail. |
| `INST-EMAIL-FIELDS` | `replies/outcomes.py:142` | email_time: timestamp_email, else timestamp_created | Probe | INST-EMAIL-FIELDS. |
| `INST-EMAIL-FIELDS` | `replies/outcomes.py:173` | The ue_type codes | Probe | INST-EMAIL-FIELDS (ue_type on campaign sends). Partly seen: the seed sends of 5 and 6 Oct passed `is_campaign_send` (they reached "sent: no contact of ours"), so they were neither 2 nor 3. |
| `INST-EMAILS-UNTIL` | `clients/instantly.py:837` | GET /emails applies max_timestamp_created | Probe | INST-EMAILS-UNTIL reads one mailbox's sends, then again cut at a time between them. If ignored, sync_outcomes' catch-up reads up to now, which is safe (events are idempotent). |
| `INST-FROM-NAME` | `clients/instantly.py:548` | The From name is built from first_name and last_name; PATCH /accounts takes those two | Probe | INST-FROM-NAME reads the From name on the latest email each mailbox sent since 6 Oct. The PATCH stays Watch: the next `mailbox check --fix --live` with drift (`names_set`, then `name_drift` {} the next morning). |
| `INST-FROM-NAME` | `registry/mailboxes.py:39` | The sender name (pointer) | Probe | As INST-FROM-NAME. |
| `INST-FROM-NAME` | `replies/poll.py:171` | The From display name is in from_address_json | Probe | INST-FROM-NAME (sent emails) and INST-REPLY-CAMPAIGN (a seed reply). |
| `INST-ERASE-EMAILS` | `ops/erase.py:38` | Whether deleting a lead removes its emails | Watch | The erase report already tells Harry to check the Unibox by hand; the first real erasure settles it. |
| `INST-NOT-SENDING` | `clients/instantly.py:144` | The full list of not_sending_status codes | Watch | Every campaign read `null` from 2 to 7 Oct, draft, paused, active and completed alike (mailbox_health `campaign_status`). An unknown code reads "code N (see Instantly)" there and in the daily post; that log line names it when it comes. |
| `INST-VAR-LIMIT` | `clients/instantly.py:138` | The custom-variable length limit (CUSTOM_VARIABLE_LIMIT = None) | Watch | None means no check. The seed emails of 5 and 6 Oct arrived whole (Harry read them), so values of an email's length are kept. Measure by hand only if a much longer body is planned (docs/phase0-runbook.md §7). |
| `INST-VAR-LIMIT` | `clients/instantly.py:348` | The limit's stub | Watch | As INST-VAR-LIMIT above. |
| `INST-CAMPAIGNS-SHOW` | `ops/cli.py:1141` | `campaigns show` prints what Instantly kept | Moot | A pointer to the markers, now to this page. |
| `INST-DOC` | `clients/instantly.py:17` | The module's note on the marker | Moot | Describes the convention; now points here. Kept as the one line that names the tag. |

### Apollo (44: 18 confirmed live, 17 probe, 6 watch, 3 moot)

| Id | Where | What | Status | Evidence, or how it is settled |
| :- | :- | :- | :- | :- |
| `APO-BULK-ENRICH` | `clients/apollo.py:47` | Bulk enrich's body {"domains": [...]} | Confirmed live | apollo_enrich 5, 6 and 7 Oct: `bulk_calls` 2, `found` 15 of 15, `bulk_unmatched` 0, `single_calls` 0. |
| `APO-BULK-ENRICH` | `sources/apollo_enrich.py:20` | The bulk answer's shape | Confirmed live | As APO-BULK-ENRICH. |
| `APO-CUSTOMER-DOMAINS` | `sources/lookalikes.py:150` | How many customer domains HubSpot has | Confirmed live | lookalikes 5 Oct: 588 active and 582 churned customers, 1,161 domains excluded; under the 1,500 the cap allows. |
| `APO-DAYS-IN-TITLE` | `sources/apollo_people.py:281` | REST honours person_days_in_current_title_range | Confirmed live | apollo_people 6 Oct: `leader_new_in_title` 3 of 18 with a leader; 7 Oct: 0 of 17. An ignored filter would make every leader new. |
| `APO-IDS-JOBS` | `sources/apollo_jobs.py:163` | organization_ids and organization_num_jobs_range together | Confirmed live | apollo_signals 2, 5, 6 and 7 Oct: each screen of 100 returned 50, 57, 66 and 59 as hiring (an ignored filter returns all 100 or strangers). |
| `APO-IDS-SIZE` | `sources/apollo_universe.py:704` | organization_ids with organization_num_employees_ranges | Confirmed live | source_universe 5 Oct: `size_bands_backfilled` 138 from 8 band searches (8 credits). |
| `APO-JOB-POSTINGS` | `clients/apollo.py:51` | The job postings REST path | Confirmed live | apollo_signals 2, 5, 6 and 7 Oct: `postings_read` 22 a run, `errors` []. |
| `APO-JSON-BODY` | `clients/apollo.py:84` | Search filters as a JSON body | Confirmed live | site_visits 6 and 7 Oct returned 57 and 72 companies (a filter ignored would return the database); the postings screen split each 100 ids into 50–66 hiring; the size-band backfill placed 138 accounts (5 Oct). |
| `APO-LEAD-CREDITS` | `clients/apollo.py:172` | Which credit type holds the lead credits | Confirmed live | Apollo's usage tool showed 30,380 lead credits (29 Sep, docs/phase0-facts.md); the source jobs read its left_over every run from 2 Oct and none logged `apollo_balance_unknown`. APO-USAGE re-reads it. |
| `APO-LEAD-CREDITS` | `sources/apollo_credits.py:36` | The balance's credit type | Confirmed live | As APO-LEAD-CREDITS. |
| `APO-NAICS-DIGITS` | `sources/apollo_universe.py:95` | organization_naics_codes takes 2 to 5 digits | Confirmed live | Every active industry searches by NAICS prefixes, 6-digit codes sent as 5; those searches found the 432 accounts of 2 Oct. Six digits are untried and not needed. |
| `APO-PEOPLE-FILTERS` | `contacts/pick.py:126` | "United States" as the whole country; contact_email_status honoured | Confirmed live | pick_contacts 2 Oct: people in CA or WA came back and were left out after (4), and every reveal kept was verified (no "not verified" reason in 64 reveals). |
| `APO-PEOPLE-ROWS` | `contacts/pick.py:264` | People search rows: people[], id, title, state | Confirmed live | pick_contacts 2 Oct: 48 picked from search rows, their ids revealed (64 reveals), states read. |
| `APO-PEOPLE-TOTAL` | `clients/apollo.py:149` | People search's total_entries at the top level | Confirmed live | apollo_people 6 Oct: a coverage figure for each of 134 accounts searched (75 at least 0.5, 59 below), read from total_entries. |
| `APO-PEOPLE-TOTAL` | `sources/apollo_people.py:305` | An organization-only search's total_entries | Confirmed live | As APO-PEOPLE-TOTAL. |
| `APO-ROW-COUNTRY` | `sources/lookalike_leads.py:261` | Search rows carry country and state | Confirmed live | site_visits 6 Oct (the same endpoint): 11 companies put outside the US by country, a state on 52 of 72 rows ("HQ state unknown" 20). |
| `APO-ROW-EMPLOYEES` | `sources/apollo_universe.py:741` | Search rows carry estimated_num_employees | Confirmed live | They do not: the first live run (2 Oct) found none on 432 accounts (commit 9cbd76b). The band comes from the search's size filter, as built then. |
| `APO-VISITOR-FILTERS` | `clients/apollo.py:270` | The website-visitor filters in the REST body, and the two buckets | Confirmed live | site_visits 6 Oct: the 30-day /us search 57 companies, the one-day 4, the intent pages 0; `refused` empty, `errors` []; 7 Oct: 72, 0, 0. |
| `APO-BULK-FIELDS` | `sources/apollo_enrich.py:231` | The bulk answer's unique_enriched_records and credits_consumed | Probe | APO-BULK-FIELDS (one unknown domain, free). |
| `APO-ENRICH-COST` | `sources/apollo_enrich.py:33` | 1 credit per company found, 0 for one not found | Probe | APO-ENRICH-NOTFOUND reads usage around a not-found enrich (free); APO-ENRICH-ONE and APO-CREDIT-COST around a found one (1 credit, opt-in). Apollo's usage also moves with use in the Apollo app, so a month's reconciliation stays approximate. |
| `APO-ENRICH-NOTFOUND` | `clients/apollo.py:124` | enrich and bulk_enrich answers; a company Apollo does not know | Probe | Bulk confirmed live (5 Oct). The single answer: APO-ENRICH-ONE (1 credit, opt-in); not found: APO-ENRICH-NOTFOUND (free). |
| `APO-ENRICH-NOTFOUND` | `sources/apollo_enrich.py:272` | 404 for a domain Apollo has no record of | Probe | APO-ENRICH-NOTFOUND (free). |
| `APO-FUNDING-AMOUNT` | `sources/apollo_universe.py:385` | funding_events[].amount as text with a currency | Probe | APO-FUNDING-AMOUNT (stored facts, free). Partly seen: apollo_enrich found dated rounds for 5–7 of 15 a run. |
| `APO-FUNDING-AMOUNT` | `sources/apollo_universe.py:403` | latest_funding_round_date, latest_funding_stage | Probe | As APO-FUNDING-AMOUNT. |
| `APO-GROWTH-FRACTION` | `sources/apollo_universe.py:484` | organization_headcount_twelve_month_growth is a fraction | Probe | APO-GROWTH-FRACTION (stored facts, free). |
| `APO-GROWTH-FRACTION` | `sources/lookalikes.py:619` | growth_band_of reads a fraction | Probe | As APO-GROWTH-FRACTION. |
| `APO-LOOKALIKE` | `clients/apollo.py:296` | The lookalike search's body key, its limit of 5, its price | Probe | APO-LOOKALIKE with --apollo-credits (1 credit; 5 seeds sent), APO-CREDIT-COST for the price. Otherwise the first lookalike_leads run on 1 Nov shows it (`lookalike_leads_done`). |
| `APO-LOOKALIKE` | `sources/lookalike_leads.py:107` | "United States" and "California, US" on the lookalike search | Probe | As APO-LOOKALIKE. |
| `APO-POSTINGS-KEY` | `clients/apollo.py:137` | The postings list's key | Probe | APO-POSTINGS-KEY (posting titles stored for accounts with open roles). |
| `APO-RATE-LIMITS` | `sources/apollo_people.py:49` | People API Search rate limits on Spill's plan | Probe | APO-RATE-LIMITS (one free search's headers). So far: 301 requests in 9 minutes on 6 and 7 Oct met no 429. |
| `APO-ROW-DESCRIPTION` | `sources/apollo_universe.py:364` | Search rows carry short_description | Probe | APO-ROW-DESCRIPTION (stored facts, free). Roadmap §4 item 2 waits on it. |
| `APO-ROW-NAICS` | `sources/apollo_universe.py:345` | Search rows carry naics_codes | Probe | APO-ROW-NAICS (stored facts, free). |
| `APO-SEARCH-ROW` | `sources/apollo_universe.py:140` | organization_locations "<state name>, US" | Probe | APO-SEARCH-ROW with --apollo-credits (1 credit). The stored accounts cannot show it: a row outside the active states is skipped, not stored. |
| `APO-USAGE` | `clients/apollo.py:189` | The website-visitor credit type and its fields | Probe | APO-USAGE (credit_usage_stats, no credits). |
| `JOB-BOARDS` | `sources/job_posts.py:18` | The job boards' response shapes (Greenhouse, Lever, Ashby, Workable) | Probe | JOB-BOARDS (stored posting text by feed, free). So far read_pages found a feed at 43 of 360 accounts, errors []. |
| `APO-DOMAINS-LIST` | `sources/lookalikes.py:145` | How many domains q_organization_domains_list takes | Watch | 1 Nov: a chunk of 100 that Apollo cuts short shows as accounts with no band in the `lookalikes` summary. |
| `APO-GROWTH-FILTER` | `sources/lookalikes.py:135` | The growth filter in the REST body; inclusive bounds | Watch | Runs first on 1 Nov. The code refuses a run whose first band returns most of the domains asked (Apollo ignored the filter) and says so in the `lookalikes` summary (`growth`). |
| `APO-GROWTH-FILTER` | `sources/lookalikes.py:155` | The guard until the growth filter is confirmed | Watch | As APO-GROWTH-FILTER. |
| `APO-REVEAL-COST` | `contacts/pick.py:101` | A reveal costs 1 lead credit and reports credits_consumed | Watch | 93 reveals from 2 to 7 Oct were counted 93 credits, so credits_consumed never said more than 1. A reveal is never made to test it; compare Apollo's usage page with the ledger on a day no one used the Apollo app. |
| `APO-VISIT-DAY` | `sources/site_visits.py:30` | What "the last 1 day" covers | Watch | Settles when one company shows in two one-day lists a day apart for one visit: two `site_visit` events on consecutive days (the readout counts visits, so a double is visible there). |
| `APO-VISIT-REFUSED` | `sources/site_visits.py:112` | The HTTP status of a plan without website visitors | Watch | The plan has them (searches answered 6 and 7 Oct). The code reads 400, 402, 403, 404 or 422 as refused and says so (`site_visits_done` `refused`); only a plan change would show it. |
| `APO-VISIT-AGGREGATES` | `clients/apollo.py:57` | Website-visitor domain aggregates: REST path | Moot | No job calls it (site visits are 1 or 0, not counts). Confirm path and cost before one does. |
| `APO-VISIT-AGGREGATES` | `clients/apollo.py:387` | domain aggregates: the "stats not found" status | Moot | As APO-VISIT-AGGREGATES. |
| `APO-VISIT-AGGREGATES` | `sources/site_visits.py:23` | The cost of a domain aggregates call | Moot | As APO-VISIT-AGGREGATES. |

### HubSpot (12: 6 confirmed live, 1 seed probe, 3 probe, 2 watch)

| Id | Where | What | Status | Evidence, or how it is settled |
| :- | :- | :- | :- | :- |
| `HS-EMAIL-DOMAIN` (gate) | `clients/hubspot.py:172` | hs_email_domain is searchable with EQ | Confirmed live | verify_accounts 2 and 5 Oct excluded 1 and 2 accounts for "a contact at the domain opted out or bounced in HubSpot", errors []; an unknown property is a 400, an ignored filter would exclude every account. |
| `HS-MEETING-SOURCE` (gate) | `clients/hubspot.py:53` | A meetings-link booking has hs_meeting_source MEETINGS_PUBLIC; outcome COMPLETED | Confirmed live | Read 7 Oct through the HubSpot connector (read-only): 8 of Harry's 12 meetings since 7 Sep are "Spill walkthrough" bookings, all MEETINGS_PUBLIC (the other 4 calendar-synced); the portal has 74 MEETINGS_PUBLIC and 0 MEETINGS_EMBEDDED since 7 Sep; COMPLETED is an hs_meeting_outcome option. |
| `HS-MEETING-SOURCE` (gate) | `crm/readback.py:5` | readback: bookings are MEETINGS_PUBLIC | Confirmed live | As HS-MEETING-SOURCE. |
| `HS-MEETING-SOURCE` (gate) | `crm/readback.py:59` | BOOKING_SOURCES | Confirmed live | As HS-MEETING-SOURCE. |
| `SEED-INTEREST` (gate) | `crm/readback.py:13` | A booking stops the account's leads (stop_lead) | Seed probe | As SEED-INTEREST. |
| `HS-DEALS-ASSOC` (gate) | `clients/hubspot.py:207` | The deal search filters on associations.company (open deals) | Probe | HS-DEALS-ASSOC (one company's deal count against the portal's). verify_accounts has run it since 2 Oct with errors [], but that does not show the filter was applied. |
| `HS-DEALS-ASSOC` (gate) | `clients/hubspot.py:214` | The same, for every deal of a company | Probe | As HS-DEALS-ASSOC. |
| `HS-DEMO-DEAL` (gate) | `crm/readback.py:24` | spill.chat/us/book-demo raises a deal at Demo requested | Watch | Deals at Demo requested or later appear daily (hubspot_readback 7 Oct: 18 seen in 3 days, none ours). Settles on the first US booking: `meeting_booked` with source `hubspot_deal`. |
| `HS-COMPANY-LEAD-STATUS` | `crm/hubspot_writes.py:18` | Whether companies have hs_lead_status | Confirmed live | Read 7 Oct: companies have it, but its options are New, In Progress, Open Deal, Unqualified, Bad Timing and No reply, with no CONNECTED; so leaving it unset on companies is right. |
| `HS-STAGES` | `crm/readback.py:17` | Spill 3.0's stage labels | Confirmed live | Read 7 Oct: Demo requested 154381888, Demo created 154381886, Demo held 154381887, Closed lost 154381892 (and hubspot_readback has run every 15 minutes since 6 Oct, errors []). |
| `HS-ASSOC-IDS` | `clients/hubspot.py:63` | The association type ids | Probe | HS-ASSOC-IDS (v4 labels, 8 pairs). |
| `HS-UTM` | `enrol/utm.py:30` | The meetings page and Webflow keep UTM tags | Watch | utm_links is no. Settles after Harry turns it on: a booking's contact with utm_* in its original source. |

### Slack (2: 2 probe)

| Id | Where | What | Status | Evidence, or how it is settled |
| :- | :- | :- | :- | :- |
| `SLK-SCOPES` | `clients/slack.py:136` | reactions:read is on the installed app | Probe | SLK-SCOPES (auth.test's X-OAuth-Scopes). poll_approvals has read the 14 cards of 7 Oct every 5 minutes with errors [], but it may read reactions from the thread without reactions.get. |
| `SLK-SCOPES` | `clients/slack.py:223` | DMs: im:write, and the Messages tab | Probe | SLK-SCOPES for im:write. The Messages tab stays Watch: the first escalation DM (`escalated`, or `escalation_dm_failed`); deploy/slack-app-manifest.yaml turns it on. |

### Clay (10: 9 probe, 1 moot)

| Id | Where | What | Status | Evidence, or how it is settled |
| :- | :- | :- | :- | :- |
| `CLAY-CHECK` | `clients/clay.py:48` | The Public API beta and base URL | Probe | `us-outbound clay check-email --first NAME --last NAME --domain spill.chat --live` (ops/clay_check.py): one Work Email lookup for a Spill colleague; it prints what each of the Clay items turned out to be. Not run yet (no Clay call in the logs). |
| `CLAY-CHECK` | `clients/clay.py:60` | Work Email's input names | Probe | As CLAY-CHECK. |
| `CLAY-CHECK` | `clients/clay.py:67` | Work Email's output fields | Probe | As CLAY-CHECK. |
| `CLAY-CHECK` | `clients/clay.py:78` | Run and item status values | Probe | As CLAY-CHECK. |
| `CLAY-CHECK` | `clients/clay.py:156` | The function API | Probe | As CLAY-CHECK. |
| `CLAY-CHECK` | `clients/clay.py:217` | The results shape | Probe | As CLAY-CHECK. |
| `CLAY-CHECK` | `contacts/pick.py:118` | What one Work Email lookup costs | Probe | As CLAY-CHECK. |
| `CLAY-CHECK` | `contacts/pick.py:581` | Work Email's input names, from pick_contacts | Probe | As CLAY-CHECK. |
| `CLAY-CHECK` | `ops/clay_check.py:2` | The check-email command's own note | Probe | As CLAY-CHECK. |
| `CLAY-CROSS-COST` | `clay_cross_check.py:55` | What one cross-check lookup costs | Moot | clay_cross_check is no and the "US Outbound – Accounts" function is not built. When it is, its first run's summary (`clay_cross_check`) shows the credits Clay reports. |

### Google Sheets (1: 1 moot)

| Id | Where | What | Status | Evidence, or how it is settled |
| :- | :- | :- | :- | :- |
| `SHT-CREATE` | `clients/sheets.py:253` | spreadsheets.create honours GridData | Moot | The settings sheet exists and syncs daily; `settings bootstrap` refuses while a sheet id is set. |

### Our own rules (2: 2 moot)

| Id | Where | What | Status | Evidence, or how it is settled |
| :- | :- | :- | :- | :- |
| `COPY-PHRASES` | `enrol/copy_rules.py:71` | The EAP phrase lists | Moot | A rule of ours, not an API detail; Harry changes it like any copy rule. |
| `COPY-PHRASES` | `enrol/copy_rules.py:166` | What counts as a statistic | Moot | As above. |

## What `phase0 check` runs

Each line of its output is one of these ids. Read-only unless the kind says otherwise.

| Id | Kind | Calls | Cost |
| :- | :- | :- | :- |
| `INST-LEAD-UNSUB` | read | POST https://api.instantly.ai/api/v2/leads/list (each US Outbound campaign; seed leads only) | none |
| `INST-REPLY-CAMPAIGN` | read | GET https://api.instantly.ai/api/v2/emails (email_type received, each registry mailbox, 14 days) | none |
| `INST-AUTO-REPLY` | read | GET https://api.instantly.ai/api/v2/emails (email_type received) | none |
| `HS-DEALS-ASSOC` | read | POST https://api.hubapi.com/crm/v3/objects/deals/search (one company's deals, then all deals, limit 1) | none |
| `INST-EMAILS-UNTIL` | read | GET https://api.instantly.ai/api/v2/emails (email_type sent, one mailbox, twice: without and with max_timestamp_created) | none |
| `INST-EMAIL-FIELDS` | read | GET https://api.instantly.ai/api/v2/emails (email_type sent) | none |
| `INST-FROM-NAME` | read | GET https://api.instantly.ai/api/v2/emails (email_type sent; from_address_json) | none |
| `HS-ASSOC-IDS` | read | GET https://api.hubapi.com/crm/v4/associations/{from}/{to}/labels (8 pairs) | none |
| `SLK-SCOPES` | read | GET https://slack.com/api/auth.test (its X-OAuth-Scopes header) | none |
| `APO-USAGE` | read | POST https://api.apollo.io/api/v1/usage_stats/credit_usage_stats (0 credits) | none |
| `APO-RATE-LIMITS` | read | POST https://api.apollo.io/api/v1/mixed_people/api_search (per_page 1, 0 credits; its rate-limit headers) | none |
| `APO-ENRICH-NOTFOUND` | read | GET https://api.apollo.io/api/v1/organizations/enrich?domain=spill-phase0-unknown-domain-check.com (0 credits: none found) | none |
| `APO-BULK-FIELDS` | read | POST https://api.apollo.io/api/v1/organizations/bulk_enrich (spill-phase0-unknown-domain-check.com alone, 0 credits) | none |
| `APO-ROW-NAICS` | the database (what the live jobs stored) | the database: apollo_org naics facts of accounts found by search and never enriched | none |
| `APO-ROW-DESCRIPTION` | the database (what the live jobs stored) | the database: apollo_org description facts of accounts found by search and never enriched | none |
| `APO-GROWTH-FRACTION` | the database (what the live jobs stored) | the database: stored headcount_growth_12m facts | none |
| `APO-FUNDING-AMOUNT` | the database (what the live jobs stored) | the database: stored funding facts | none |
| `APO-POSTINGS-KEY` | the database (what the live jobs stored) | the database: stored apollo_jobs facts | none |
| `JOB-BOARDS` | the database (what the live jobs stored) | the database: stored job_posts posting_text facts by feed | none |
| `APO-SEARCH-ROW` | read, opt-in | POST https://api.apollo.io/api/v1/mixed_companies/search (one HQ state, per_page 10: 1 credit) | 1 Apollo credit, with `--apollo-credits` |
| `APO-LOOKALIKE` | read, opt-in | POST https://api.apollo.io/api/v1/mixed_companies/search (lookalike_organization_ids, 5 seeds, per_page 10: 1 credit) | 1 Apollo credit, with `--apollo-credits` |
| `APO-ENRICH-ONE` | read, opt-in | GET https://api.apollo.io/api/v1/organizations/enrich?domain=spill.chat (1 credit) | 1 Apollo credit, with `--apollo-credits` |
| `APO-CREDIT-COST` | read, opt-in | POST https://api.apollo.io/api/v1/usage_stats/credit_usage_stats again after the paid probes (0 credits) | none |
| `CLAY-CHECK` | pointer | nothing here: `us-outbound clay check-email` makes the one lookup | Clay credits only if Work Email finds an address |
| `SEED-RESUME` | reads the seed lead (`--seed`) | GET https://api.instantly.ai/api/v2/leads/{id} (the seed lead; when an earlier run paused it) | none |
| `SEED-PAUSE` | writes on the seed lead (`--seed`) | PATCH https://api.instantly.ai/api/v2/leads/{id} {status: 2}, GET it, PATCH {status: 1}, GET it (the seed lead, if it is still in its sequence) | none |
| `SEED-INTEREST` | writes on the seed lead (`--seed`) | POST https://api.instantly.ai/api/v2/leads/update-interest-status (1, then 2, then back), GET the lead (the seed lead, if it has finished) | none |
| `SEED-FORWARD` | writes on the seed lead (`--seed`) | POST https://api.instantly.ai/api/v2/emails/forward (the seed lead's email 1, to escalation_email only) | none |

## Added after the inventory (7 Oct 2026, the sending and retention builds)

| Id | Where | What | Status | How it is settled |
| :- | :- | :- | :- | :- |
| `SEED-INTEREST` | `clients/instantly.py` `INTEREST_INTERESTED` | 1 is "Interested" (set when a reply is classified positive, `replies/poll.py`) | Seed probe | SEED-INTEREST now sets 1, then 2, and reads each back before setting the lead back. |
| `W-BLACKOUT-PAUSE` | `registry/blackout.py` | Instantly accepts a pause of a campaign it shows as completed (status 3) | Watch | The first blackout pause (from Fri 20 Nov, 16:00 ET): the job's run summary lists every campaign as paused, none under errors. |
| `W-BLACKOUT-RESUME` | `registry/blackout.py` | A campaign activated again after the blackout sends the steps that fell due during it | Watch | Mon 30 Nov: `sent` events for steps whose forecast day fell on 23–27 Nov. If none come, open question 78's fallback is to stop and start by hand. |
| `W-DELETED-404` | `ops/retention.py`, `clients/instantly.delete_lead` | GET /leads/{id} answers 404 for a deleted lead | Watch | The first retention deletion (about a month after the first sequences end): a 404 counts as already gone; anything else fails that night's run loudly. |
| `W-FINISHED-STATUS` | `ops/retention.py` | Instantly does not keep a finished lead "active" | Watch | `us-outbound status` lists retention's held leads; leads held as "active with fewer than 4 sends" after their last step say it does. |
| `M-ERASE-THREAD` | `ops/erase.py` | Instantly's own thread stays in Instantly after an erase | Moot | A manual step erase prints; nothing to probe. |

## Found on the way (already handled)

What the live runs showed that the vendor docs did not say, each already built around:

- Apollo's organization search rows carry no employee count and no funding (2 Oct): the size band comes from the
  search's own filter, and funding from organization enrich (`apollo_enrich`).
- Instantly drops text outside any tag when it saves a step (2 Oct): the body variable sits in a `<div>`.
- Instantly does not fill `{{unsubscribe}}` in an href (5 Oct): the step uses its editor's placeholder address.
- Instantly sent step 1 as text only from a campaign with text_only off (5 Oct): `first_email_text_only` is pinned off.
- Instantly marks an active campaign "completed" when it has no lead left, is_evergreen or not (6 Oct): the next add
  resumes it.
- HubSpot companies have `hs_lead_status`, but without CONNECTED (7 Oct): the writes set it on contacts only, as built.
- HubSpot has a MEETINGS_EMBEDDED source beside MEETINGS_PUBLIC (7 Oct), but every meeting booked through a meetings
  page since 7 Sep is MEETINGS_PUBLIC (74) and none EMBEDDED, so `BOOKING_SOURCES` needs nothing more.
