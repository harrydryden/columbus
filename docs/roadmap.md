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
| Verify | `verify_accounts` on Apollo data plus HubSpot (customer, owner, open deal, opted-out), while `clay_verification` = `skip` | `verify_in_clay` (the Clay functions don't exist yet, and Clay's REST API is unconfirmed) |
| Score | The review's Appendix A: EAP de-weighted, hiring read from job postings, Q4 off, funding split by age, a size signal favouring 10–99, site visits to Priority, the lookalike signal, IT services excluded from tech, Legal Teams on at 20% of the focus | Careers and benefits pages (they need Clay), so the EAP, benefits and wellbeing openers can't fire yet |
| Choose the person | `pick_contacts`: Roles by size and seniority. 10–49: founder, then a senior People leader, then operations. 50–249: a senior People leader, then the founder, then operations, then HR managers. It reveals one verified email (about 1 Apollo credit) and writes the People-leader facts its search sees | A second contact at 50–249 |
| Copy | 318 sequences, one per industry and role, at days 0, 7, 14 and 21. Email 1 is a hook with the industry link only; email 4 mentions the free trial. Nine data-led openers. Render-time rules, plus QA by Sonnet against facts.md and each industry page | Per-account openers written by Claude (below) |
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
6. **Hand-check.** Monday morning, `handcheck show --live`, then `handcheck approve --live`, pulling
   any account that looks wrong.
7. **Sign-off.** Set `live_sending` = yes, then run `us-outbound start --live`. It activates the
   paused campaigns once `campaigns ensure` reports no drift. Enrol runs at 12:00 UK (07:00 ET) on
   weekdays. `us-outbound stop --live` pauses everything again.
8. **The demo page.** Its SEO title still reads "The UK's Highest Rated EAP". Every email 2–4 links
   to it, and the signature links Harry's booking page.

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
4. Turn on Clay verification and the careers-page signals as soon as the Clay functions exist (§4).

## 4. What is left to build

### Week 1, during the pilot
1. **Live checks of the `PHASE0-CONFIRM` items**, starting with the ones that gate sending and opt-outs:
   - Instantly: the `{{unsubscribe}}` tag; lead status codes; the reply and forward endpoints;
     stop-on-reply for replies Instantly classes as automatic.
   - Apollo: organization and people search filters, job postings, credit charges.
   - HubSpot: meetings and pipeline stage labels.
   Fix whatever the first runs show.
2. **A per-account opener written by Claude.** This is the biggest remaining lever on Harry's
   "personalised, relevant data and hook". Today only accounts with a matched signal get an
   opener, and without Clay those are hiring, growth, funding, People-team facts and size.
   - A nightly Batch call (the task model) writes one factual sentence per queued account, from
     its Apollo facts (description, keywords, hiring, growth, location). The render-time rules
     check it, and a 30% holdout measures it.
   - Cost: about $1–3 a month, within the $10 cap.
3. **Careers and benefits pages without Clay:** a direct read of each account's careers and
   benefits pages, so the EAP, benefits and wellbeing signals and their openers can fire. Clay
   replaces it when its functions exist.
4. **Interest status back to Instantly** (positive, meeting booked). Also stop the remaining steps
   for an enrolled account that turns out to be a customer.
5. **`pick_contacts` should skip email sources a kill rule has paused** (`holds.paused_sources`).

### Weeks 2–4, to scale
1. **Clay:** the "US Outbound" functions (Accounts, Contacts), `verify_in_clay`, and the Clay
   email waterfall for Apollo misses and catch-alls. Then set `clay_verification` = required.
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
- Eight industry pages still carry another page's text: the churches text on Animal welfare, Arts &
  culture, Environmental nonprofits, Human rights and International aid & relief; Social welfare on
  Emergency & rescue; Automotive on Packaging; Private duty on Supported living. Copy for those
  rows was written around them.
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
