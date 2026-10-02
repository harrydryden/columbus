# US Outbound: go-live on 5 Oct, and what is left to build

Written 1 Oct 2026, after Harry's notes of the same day ("make all the changes"): go live on
Monday 5 October with a pilot, then scale quickly. Make every email relevant through both the
industry and the role. Email 1 informs and plants a seed, and asks only for a visit to the site.
Email 4 alludes to the free trial. Target the most senior decision maker who has time to engage,
as Spill's HubSpot history shows. Use Spill's HubSpot customers to inform lookalikes.

## 1. Where the system stands

| Stage | Built | Not built yet |
| :- | :- | :- |
| Find accounts | `source_universe` (Apollo organization search by industry, state and size; one account per root domain), `apollo_signals` (job postings), `lookalikes` (Spill's HubSpot customers as cells, a signal and an early exclusion of customer domains), `named` accounts | `site_visits`, `public_signals` (IRS BMF, job feeds, WARN), Form 5500 |
| Verify | `verify_accounts` on Apollo data plus HubSpot (customer, owner, open deal, opted-out), while `clay_verification` = `skip`. Doubtful Apollo facts (no HQ state, no size, a count near the 10, 50 or 250 edge) go to the weekly hand-check with the reason (2 Oct). For the groups `apollo_enrich` enriches, Apollo's exact employee count replaces the searched size band before verify runs | `verify_in_clay`; Clay's cross-check of doubtful HQ state and size (the `verify.cross_check` hook) |
| Score | The review's Appendix A: EAP de-weighted, hiring read from job postings, Q4 off, funding split by age (the funding facts come from Apollo's organization enrich, `apollo_enrich`, weekdays 04:10, for the industry groups named in the General `apollo_enrich_groups`, Technology & Startups by default, since Apollo's search rows carry no funding; 1 credit per company found, within 15% of the month's Apollo credits; 2 Oct), a size signal favouring 10–99, site visits to Priority, the lookalike signal, IT services excluded from tech, Legal Teams on at 20% of the focus. The careers and benefits page reader, `read_pages` (2 Oct, no Clay credits), so the EAP, benefits and wellbeing signals and openers can fire | Enhancing the reader, if its coverage falls short (§4, "Decide after the first batches") |
| Choose the person | `pick_contacts`: Roles by size and seniority. 10–49: founder, then a senior People leader, then operations. 50–249: a senior People leader, then the founder, then operations, then HR managers. It reveals one verified email (about 1 Apollo credit) and writes the People-leader facts its search sees. Clay's Work Email waterfall for Apollo's misses and catch-alls, behind `clay_email_fallback` (no until Clay's API is confirmed; 2 Oct) | A second contact at 50–249 |
| Copy | 318 sequences, one per industry and role, at days 0, 7, 14 and 21. Email 1 is a hook with the industry link only; email 4 mentions the free trial. Tokenized openers (2 Oct): a line per signal and copy role, filled at enrol time with the account's own facts, with a 30% no-opener holdout. Render-time rules, plus QA by Sonnet against facts.md and each industry page | The careers-page facts that make the page-reader openers fire (§4 item 3) |
| Send | `enrol` (scheduled, dry until `live_sending` = yes), the per-mailbox ramp (10, then 20, then 30 a day), Instantly's own unsubscribe link and header, the signature | Interest status written back to Instantly |
| Replies | `sync_outcomes` and `poll_replies` every 15 minutes: classification (Sonnet), drafts (Opus), Instantly unsubscribes and "stop" replies into suppression and HubSpot, out-of-office dates | Pausing and resuming a lead around an out-of-office reply (switched off until Instantly's lead pause is confirmed) |
| Hand-off | `poll_approvals`: Slack ✅/edit/❌ from Harry or the mailbox owner, or `us-outbound replies list|approve|skip` without Slack. HubSpot company, contact, note, task and deal on positive or referral replies. 24-hour escalation. `hubspot_readback` for booked meetings | — |
| Safety | `kill_rules` (bounces, blocks, complaints), weekly `hand_check_post` and `handcheck show|approve`, `daily_post`, `golive`, heartbeats, the guard on every outbound call, dry-run by default | Retention jobs (deleting leads 31 days after their last step; Apollo deletion notices) |
| Learn | The events table and the readout views | `monday_readout`, test reads with pre-registered looks, UTMs on links |

About 70 API details are marked `PHASE0-CONFIRM` in the code: endpoint shapes and status codes we
could not check from the build machine. The pilot's first runs confirm them (§3).

## 2. Plain text or HTML: the answer for the pilot

Plain text doesn't look spammy to a reader; most one-to-one email is close to plain. What looks
automated in plain text is raw links. Every email now carries five: the industry page, Spill, the
booking link, Trustpilot and the unsubscribe link. Shown as full URLs, they read like a mail-merge.
Mailbox providers don't penalize light HTML. They penalize heavy HTML: images, tracking pixels,
styled templates and many links to many domains.

So the pilot sends **light HTML**:
- default fonts and no images;
- no open or click tracking (both are off);
- links as words, not URLs;
- the data-source notice in small type.

That keeps the credibility of a personal email and most of plain text's deliverability. The
risk that remains is link count, now five per email with two outside spill.chat (HubSpot and
Trustpilot). That is the first thing to watch:
- check where the seed-inbox test lands;
- split reply rate by mailbox provider in the daily post.

If Gmail or Outlook files the pilot under Promotions or spam, the first change is a plain email 1
that keeps the signature's words but drops its two outside links (decision D19 in the review), not
a switch of every email to plain text.

## 3. Go-live: Friday 2 to Monday 5 October

### Done in the build (1 Oct)
All of §1's built column is merged, tested and deployed to the Railway worker. The worker runs the
jobs on their UK schedule. Until `live_sending` = yes, nothing reaches a prospect: sourcing,
verification and contact choice write only the database; enrol, replies and posts run dry.

### Harry, before Monday (the go-live checklist)
`us-outbound golive` prints PASS or FAIL for each line and exits non-zero until every FAIL is cleared.

1. **Slack.** Create the app from `deploy/slack-app-manifest.yaml`, create #us-outbound and
   #us-outbound-dev, invite the bot, and add the bot token to Railway as
   `US_OUTBOUND_SLACK_BOT_TOKEN`. Without it, live runs refuse to start, and reply alerts and
   approvals have only the `replies` commands.
2. **Approvers.** Set `approver_slack_ids` = `U098X453UAG` on the General tab. To let Hannah and Sam
   approve replies to their own mailboxes, add their Slack ids in the Mailboxes `slack_id` column.
3. **Mailboxes.** `mailbox check --live` promotes warm mailboxes to Active (all four still say
   Warming on the sheet). Then `campaigns ensure --fix --live` creates the four "US Outbound – owner"
   campaigns, paused, with the ramped daily limits.
4. **Opt-out test.** Send one test email from each campaign to a seed inbox. Confirm the
   `{{unsubscribe}}` link works in it and that a click shows the lead as unsubscribed in Instantly.
   Then set `optout_tested` = yes.
5. **Copy approval.** On the Copy tab, set status = approved and approved_by = Harry on the rows to
   send. For the pilot that is the launch focus (Technology & Startups, Marketing & Creative
   Agencies, Legal Teams): about 25 industries × 3 roles. Every row has a QA pass stamped on it.
   Editing a row clears its stamp until `copy qa` runs again.
   **Openers (2 Oct):** run `us-outbound settings load --tab Signals --live` to add the four opener
   columns (your edits stay), read the lines on the Signals tab (§4 item 2), then `settings sync`.
   Until the columns are on the sheet, each signal's plain opener is used. `--tab General` adds
   `opener_holdout_share` (0.3), `opener_focus` (no) and `opener_focus_line`. The 30% holdout
   applies from the deploy either way, because it is the default.
6. **Hand-check.** Monday morning, `handcheck show --live`, then `handcheck approve --live`, pulling
   any account that looks wrong.
7. **Sign-off.** Set `live_sending` = yes, then run `us-outbound start --live`. It activates the
   paused campaigns once `campaigns ensure` reports no drift. Enrol runs at 12:00 UK (07:00 ET) on
   weekdays. `us-outbound stop --live` pauses everything again.
8. **The page signals' new source.** `us-outbound settings load --tab Signals --tab General --live`, then
   `us-outbound settings sync`. The five page signals then read `careers_pages` beside `clay_careers`,
   and the General tab gains `clay_email_fallback` = no. Until then settings_sync's summary says so
   (`signals_notice`), and so do `pages show` and the daily post. A Signals load keeps the sheet's
   values (weights and lines Harry edited stay) except the `source` column, which names code and so
   always takes the build's.
9. **The demo page's SEO title.** https://www.spill.chat/us/book-demo looks right on the page itself.
   Its SEO title, which shows in the browser tab, in Google and in link previews, is "Spill | The
   UK's Highest Rated EAP | Book a demo". Its description says "employee assistance programme". The
   US locale inherits both from the UK page (Webflow page 65c650592086330a300a3cf6). Every email 2–4
   links to this page. The signature's "Book a call here" goes to Harry's HubSpot meetings page instead.
10. **Funding from Apollo's organization enrich (2 Oct).** `us-outbound settings load --tab General --live`, then
    `us-outbound settings sync`, adds `apollo_enrich_groups` = Technology & Startups (comma-separated industry
    groups; blank enriches none). The job runs on that default until then. It enriches up to about 15 accounts a
    weekday (15% of `apollo_monthly_credits`), each again after 180 days, and the daily post counts what it found.

### The pilot (week of 5 Oct)
- **Volume:** the ramp holds each mailbox to 10 sends a day in its first sending week. Four
  mailboxes start at about 40 sends a day, nearly all step 1s, so 30 to 40 new accounts a day.
- **Mix:** Tech 50%, Agencies 30%, Legal 20% (the Focus tab).
- **Watch daily:** bounces (kill rules pause a mailbox over 3%), complaints, the seed-inbox
  placement, replies reaching Slack within 15 minutes and being classified correctly, and
  positive replies answered the same day.
- **Exit criteria to scale** (end of week 1):
  - bounce rate under 2% and no complaints;
  - unsubscribes confirmed end to end;
  - every reply classified and routed;
  - no copy-rule or guard refusal left unexplained.

### Scaling (from week 2)
1. The ramp takes each mailbox to 20 a day in week 2 and 30 from week 3: about 120 a day across four.
2. Raise `weekly_enrol_cap` in step with the ramp. Approve copy for the next industry groups as
   they're added to the Focus tab.
3. **Order more mailboxes now.** Capacity is the binding limit: four mailboxes at 30 a day give
   about 650 new accounts a month. New mailboxes need about 21 days of warmup, so mailboxes on a
   third domain ordered on 2 October can send from about 26 October. This is a paid service
   (Harry's decision).
4. The careers-page signals fire from our own page reader (`read_pages`, §4). Clay is kept for the
   email waterfall: set `clay_email_fallback` = yes once one Work Email call has been confirmed live.

## 4. What is left to build

### Week 1, during the pilot
1. **Live checks of the `PHASE0-CONFIRM` items**, starting with the ones that gate sending and opt-outs:
   - Instantly: the `{{unsubscribe}}` tag; lead status codes; the reply and forward endpoints;
     stop-on-reply for replies Instantly classes as automatic.
   - Apollo: organization and people search filters, job postings, credit charges. Organization enrich
     (`sources/apollo_enrich.py`): the bulk call's body and answer (`organizations`, `unique_enriched_records`), the
     single call's answer for a domain Apollo does not know (404 or an empty `organization`), and the funding fields
     (`latest_funding_round_date`, `latest_funding_stage`, `funding_events[].amount` as text like "8M").
   - HubSpot: meetings and pipeline stage labels.
   - The job-board feeds (`sources/job_posts.py`): Greenhouse's `company_name` and escaped `content`,
     Lever's `lists`, Ashby's `descriptionHtml`, Workable's `details=true` descriptions.
   Fix whatever the first runs show.
2. **Tokenized openers: built (2 Oct).** One line per signal and copy role, filled with the
   account's own stored facts (`enrol/openers.py`). This is the biggest remaining lever on Harry's
   "personalised, relevant data and hook".
   - The Signals tab has four new columns: `opener_people`, `opener_founder`, `opener_ops` and
     `opener_self` (the new People leader, when the contact is that person). A cell may hold
     alternatives, one per line. The tokens are `{company}`, `{city}`, `{open_roles}` (in words
     through nine), `{posting_title}` (the most senior current posting, cleaned of locations, req
     ids and "(Remote)"), `{people_title}`, `{funding_stage}`, `{growth}` (in words, never a
     percentage), `{evidence}`, `{provider}` and `{page}` (where the evidence was read). An unknown
     token is a sheet error, with a "did you mean". `us-outbound settings load --tab Signals` adds the
     columns, and the sheet's values win in every column it already has.
   - 39 lines: Hiring and growth, Funding in the last 6 months, People role open, First People
     hire and New People leader, which can fire without Clay, plus the four page-reader signals,
     each for three roles, and a self line. For example: "I saw Brightline has six roles open,
     including a Senior Product Designer.", "Congratulations on the Series A." and "Congratulations
     on the new role at Brightline." The contact is the new leader only when Apollo's person ids
     match (`pick_contacts` now records them); a title is never taken as proof.
   - Copy QA (2 Oct): 18 of the 38 templates were rewritten. Only founders are congratulated on a
     round, and there is no "first" People hire and no "already offers" an EAP. The evidence is the
     object of the sentence, and benefits read plural or mass. Hiring and growth gains a roles-only
     middle line. In code: "VP, People Operations" reads "VP of People Operations"; "Sr." and "II"
     go; "Organisational" is caught as British; a posting or term the email-1 rules block is passed
     over for the next. Magellan and TELUS Health count only near EAP words.
   - Signals are context, never the line (Harry, 2 Oct, after the examples above): no default line
     names what was observed, so the titles, counts, rounds and congratulations are gone. Each signal's
     lines take the pressure that situation tends to bring, at work and at home, and turn to having
     somewhere to turn; the benefits-page lines too ("rework the benefits-page lines the same way"),
     never echoing the page and never a word against an EAP or app they offer. No opener may mention
     funding or money (`copy_rules.money_violations`). An account with no signal line, Control
     among them, gets the generic line (General `opener_generic_*`). The tokens stay for lines Harry
     writes on the sheet. The lines are in `settings/defaults.py` and `templates/copy/style.md`.
   - The opener is chosen at enrol time, when the contact's role is known. It comes from the signal
     that set the angle. The first line that fills and passes the copy rules wins, then the
     signal's plain opener, then none. Nothing is invented and there is no cost per account. The
     General angle, and so Control, has no signal line.
   - A 30% holdout (`opener_holdout_share`, by account hash) gets no opener. Each contact records
     `opener_arm` (opener, holdout or none) and `opener_source` (the line, or for a holdout the line
     it would have had), and `v_account_outcomes` carries both, so a readout can compare like with
     like.
   - Optional and off (`opener_focus` = no): for an account with no firing signal, `opener_focus_line`
     ("I came across {company} and its work on {focus}.") uses a short "what they do" phrase. Sonnet
     takes it once, in a live run, from Apollo's keywords and description (now stored by
     `source_universe`). It costs about $0.002 an account, within the monthly cap. The phrase is checked for length, claims,
     names, the banned words, and every word appearing in Apollo's own text.
   - `copy check` checks every line with sample facts. `copy preview --opener [--signal NAME]
     [--leader]` shows a real line, and `copy preview --account DOMAIN` shows a stored account's
     opener. The weekly hand-check shows the opener enrol would send.
   - Still needs live data: Apollo's `short_description` on search rows (PHASE0-CONFIRM). The
     `latest_funding_stage` spellings ("Series A", "Seed") and the shape of `posting_titles`
     should be read on the first real accounts. Is `headcount_growth_12m` a fraction? The page-reader
     signals' facts arrive with item 3. Then the holdout's first read, after enough replies.
     Per-account sentences written freely by Claude are still not planned: Harry couldn't approve
     them, they can't be tested line by line, and they can invent.
3. **Careers and benefits pages without Clay: built 2 Oct** (`read_pages`, weekdays 03:45;
   `sources/pages.py`, `sources/job_posts.py`). Harry's decision: our own reader, no Clay credits.
   It reads each queued account's own careers, jobs and benefits pages (robots.txt respected, up to
   six pages, no JavaScript) and the Greenhouse, Lever, Ashby or Workable board its site links to.
   The benefit sentences, with quote and URL, are matched against the Signals tab as Clay's would
   be, so the EAP, mental-health, wellbeing-app, progressive-benefits and competitor signals and
   their openers fire. A failed read is "not read": no points, and a Hold stays.

   **Decide after the first batches** (Harry: "we should enhance this functionality if we're unable
   to get the data we need after testing live on the first few batches"). `us-outbound pages show`
   and the daily post count, for each run and so far, the read outcomes, the share with a job board,
   the share with any benefits text and the accounts each page signal matched. After the first 200
   accounts read, if fewer than about a third yield benefits text, enhance the reader. The options:
   - Claude extraction from the fuller page text (the task model, within the $10 cap, since only
     pages already fetched are read);
   - JavaScript rendering, for careers pages built in the browser;
   - Clay's Claygent, through an "US Outbound – Accounts" function (Clay credits).
4. **Interest status back to Instantly** (positive, meeting booked). Also stop the remaining steps
   for an enrolled account that turns out to be a customer.
5. **`pick_contacts` should skip email sources a kill rule has paused** (`holds.paused_sources`). Done for
   Clay (2 Oct): no Clay lookup while the clay source is paused. Apollo's reveals still go ahead.

### Weeks 2–4, to scale
1. **Clay, narrowed to what only it does** (Harry, 2 Oct 2026):
   - the email waterfall for contacts Apollo can't verify: built behind `clay_email_fallback` (no).
     Confirm one Work Email call through Clay's API (`clients/clay.py` PHASE0-CONFIRM: the routines
     endpoint, Work Email's inputs, its output fields and what a lookup costs), then set it to yes;
   - later, a cross-check when Apollo's HQ state or size looks doubtful (the `verify.cross_check`
     hook; until then those accounts go to the weekly hand-check).
   `clay_verification` stays `skip` for the pilot. The Accounts function and `verify_in_clay` are
   needed only if the page reader's coverage falls short (§4 week 1, item 3).
2. **`site_visits`:** Apollo's company-level visitors on `/us`, with same-day priority for
   enrolled and queued accounts (D13; copy never mentions a visit).
3. **A second contact at 50–249** (multi-threading), once capacity allows it.
4. **The learning loop:**
   - `monday_readout`;
   - the signal-value table;
   - test reads with pre-registered looks (a `kind` column on the Tests tab);
   - UTMs on the industry and demo links, and demo-page bookings read back.
5. **Retention and compliance:** delete leads 31 days after their last step, act on Apollo
   deletion notices within 30 days, and purge reply text after 90 days.

### October to December
1. `public_signals`: IRS BMF for nonprofits, job-post feeds and WARN layoffs. Then Form 5500
   renewal timing and PEO detection.
2. The price line by size band (D9), and in-thread steps 2 and 4 (D12).
3. Wave-2 states, and Florida if compliance allows.
4. An inbox placement test service (paid, optional).
5. HubSpot's newer API versions (v1–v3 end in Sept 2027).

### Website and sheet items for Harry
- **To publish:** corrections to the nine US industry pages that carried another page's text are
  saved in Webflow, unpublished (`docs/website/industry-pages-2026-10-01.md`). The nine are Animal
  welfare, Arts & culture, Environmental nonprofits, Human rights, International aid & relief,
  Emergency & rescue, Packaging, Supported living and Automotive & vehicles. After publishing, a
  fresh site export can refresh the Industries tab's page columns for them.
- Small Businesses has no page (its emails link spill.chat/us). A duplicate Nonprofits page should
  be archived. The pages say "over 30,000 employees" while the emails say 50,000.
- **Open question 72**, page-only claims to confirm for the US: HIPAA compliance; nothing reported
  to boards or regulators; booking by text; multiple languages; grief-experienced counselors;
  cover for contractors and volunteers; counseling in a grant budget; no copays; part-time staff.
  QA flags each as a minor where used.

## 5. How this was built (1 Oct)
Model roles:
- **Opus:** six engineers (sourcing, contacts and roles, scoring and lookalikes, reply ingest, the
  reply desk, launch safety), each in its own worktree, and fourteen copywriters.
- **Sonnet:** six QA reviewers checking every row against facts.md and its industry page.

Copy QA, two rounds plus a final check:
- round 1 passed 261 of 318 rows; the failures went back to their writers with the lessons;
- round 2 passed 307;
- the last 11 were fixed and re-checked.

The test suite (1,834 tests) covers every new job with fakes; no real external call was made in a test.
