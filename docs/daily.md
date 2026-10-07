# US Outbound: your day

For Harry, from the pilot that starts on Monday 5 October 2026. Run every command inside the
worker: `railway ssh -- us-outbound <command>`. Nothing changes without `--live`.

## The two switches (General tab)

| Key | no | yes |
| :- | :- | :- |
| `live_sending` | Nothing new reaches Instantly or a prospect. Jobs run dry, and enrol posts a few preview cards to #us-outbound-dev | Emails go out. The sheet refuses yes while `approver_slack_ids` is blank, and no lead is added until `optout_tested` = yes |
| `auto_send` | The pilot: every email waits for an approver's ✅ on its card in #us-outbound | Emails go straight to Instantly once the weekly hand-check is approved |

**Company size (General `min_employees`, `max_employees`; 10 and 249 by default).** To contact companies of 5
to 500, set 5 and 500. From the next sync every search covers the new range (new size bands are searched
from page 1), verify_accounts holds accounts to it, and the hand-check's size edges move with it. The
Roles tab's 10-49 order applies below 10 and its 50-249 order above 249. The first time, run
`us-outbound settings load --tab General --live` to add the two keys to the sheet.

**A second contact** (`second_contact`, no; Harry, 6 Oct 2026). With yes, a company of 50 or more staff
(`second_contact_min_employees`) also gets a second person of another role, from the same sender, 3 days
(`second_contact_delay_days`) after the first person's email 1, with the room new companies leave. Their
card is headed "Send approval · second contact" and names the first person; approve it like any other.
When anyone at the company replies, bounces or unsubscribes, both people's emails stop. To switch it on:
`us-outbound settings load --tab General --set second_contact=yes --live`, then `us-outbound sync`.
`us-outbound status` and the daily post say whether it is on.

**Sheet edits apply at the next sync:** 02:00 UK every day, and 11:30 UK on weekdays, so a morning
edit is in force for the 12:00 enrol. To apply an edit now, run `us-outbound sync`.
`us-outbound start --live` syncs by itself first. `us-outbound status` says when the settings were last synced.

## The brake

`us-outbound stop --live` pauses every US Outbound campaign in Instantly and stops new enrollment.
**Setting `live_sending` = no does not stop Instantly:** a campaign that is already active keeps
sending its follow-ups. To resume, run `us-outbound start --live`. It syncs the sheet, checks the
campaigns still match the settings, and activates them.

## Blackout dates

The General tab's `blackout_dates` (23 to 27 November 2026, and 18 December 2026 to 4 January 2027) are days
nothing is sent. Enrol skips them, and a ✅ whose email 1 would land on one waits. Instantly's own schedule
knows weekdays only, so since 7 October 2026 the hourly `blackout` job also pauses every US Outbound campaign
once the send window has closed (16:00 ET) on the last send day before a blackout, and starts the same
campaigns again from midnight ET after it. The follow-ups that fell due go out on the next send day (Monday
30 November; Tuesday 5 January), as the send forecast already assumed. One line in #us-outbound says when it
pauses them and one when it starts them again. `us-outbound status`, `golive` and the daily post say "paused
for the blackout until Mon 30 Nov": there is nothing to do.

- **`us-outbound stop --live` during a blackout** still stops everything, and the campaigns stay paused after
  it (the line says so) until `us-outbound start --live`.
- **`us-outbound start --live` during a blackout** resumes enrolment but leaves the campaigns paused: the job
  starts them after it.
- **A kill rule** that pauses a campaign during a blackout, or an owner with no Active mailbox when it ends,
  keeps that campaign paused: the morning mailbox check starts it once its mailbox is Active.
- **A campaign already paused** before a blackout (by `stop`, a kill rule or by hand) is never touched by it.
- **To send on a blackout date after all,** take the date off `blackout_dates`, then `us-outbound sync`: the next
  hourly run starts the campaigns. Starting one by hand in Instantly during a blackout does not last: the next
  run pauses it again. A pause made by hand in Instantly during a blackout cannot be told from the job's, so use
  `us-outbound stop --live` to pause.
- **With `live_sending` = no** the job only says what it would do (in #us-outbound-dev), as every job does; pause
  by hand with `us-outbound stop --live`.

## Your day in #us-outbound (UK times)

| When | What arrives | What to do |
| :- | :- | :- |
| 07:00 | Mailbox health, only when something changed or is wrong: a mailbox promoted to Active, a campaign's daily limit raised with the ramp, a new campaign created or activated, a sender name that is not the owner's full name | Usually nothing. If it asks you to run `us-outbound start --live` or `us-outbound mailbox check --fix --live`, run it |
| Monday 08:00 | The weekly hand-check, only if some accounts have doubtful facts (no HQ state, size or industry, a size near a band edge), site visitors included. With `clay_cross_check` = yes, only the doubts Clay couldn't settle, with both values ("Clay says 62 staff, Apollo says 49") | `us-outbound handcheck show`, then `us-outbound handcheck approve --live`, adding `--pull DOMAIN` for any that are wrong. A missing fact needs an Overrides row (`hq_state`, `employees` or `industry`); approving alone keeps the account on the check |
| Monday 08:30 | The Monday readout: last week, the targets, the exit criteria to scale, the cuts, the signal table and the tests | See **Mondays: the readout** below |
| 09:00 | The daily post | Read the **Needs you** line under the headline first |
| 12:00 | Send cards: one per email, with the whole sequence in its thread | ✅ or ❌ each one by the end of the next send day. After that the card lapses and the company goes back to the queue. If a ✅ can't go through yet (sending stopped, a reply waiting too long), the card stays open with a note in its thread |
| Any time | Reply cards, each with a draft | Answer within 2 hours, or the card is re-posted (13:00 to 23:00 UK). Answer within 24 hours: a positive reply waiting longer pauses new sends and is emailed to you |
| Hourly | Kill-rule alerts: a mailbox, an email source or an industry group held back, with the reason | Check it, then `us-outbound killrules show` and `us-outbound killrules clear ID --live` |
| Around a blackout | "Paused N US Outbound campaigns for the blackout…" (the evening before, UK time), then "Started N … again after the blackout" | Nothing (see **Blackout dates** above). A "Left paused" line names what to run |

## The words and emojis

Reply in the card's thread, or react to the card. Only an approver counts: `approver_slack_ids`,
and a mailbox's owner for replies to their own mailbox. The bot puts ✅ and ❌ on each card, so
one click decides.

| On a send card (12:00) | On a reply card | What it does |
| :- | :- | :- |
| ✅ or `send` | ✅ or `send` | Sends it. A send card's lead goes to Instantly within 5 minutes |
| ❌ or `skip` | ❌ or `skip` | A send card offers the three choices below. A reply card is closed, and nothing is sent |
| ✏️ (or `edit`), then a thread reply with the new email 1. Put "Subject: …" first, or "Email 2:" to change a follow-up | `edit: …` | The edit is checked against the copy rules and posted again. ✅ on it sends it |
| — | `send: …` | Sends that text instead of the draft |
| 👤 | — | Not this person: the next-ranked contact at the company is proposed later |
| 🚫 | — | Drop the company for good |

## Mondays: the readout

At 08:30 UK, after the hand-check and before the daily post, `monday_readout` posts last week (Monday to
Sunday, UK time) to #us-outbound. `us-outbound readout` prints the same text without posting. Read it top
to bottom:

1. **Last week:** emails sent, replies (people, not out-of-office) and how many were positive, meetings
   booked, bounces and unsubscribes. Then the companies emailed so far and how many of their 28-day reply
   windows have closed: until they have, every rate still rises.
2. **Against the targets:** companies enrolled against `weekly_enrol_cap`; the reply, positive and meeting
   rates so far against the working assumption (3 to 5%, about 1%, 0.5 to 1%); the stop rule's progress.
3. **Exit criteria to scale**, each "Met" or "Not met" with its numbers: bounces under 2%, no complaints,
   unsubscribes confirmed end to end, every reply classified and routed, no unexplained refusal. When all
   five are met, raise `weekly_enrol_cap` with the ramp (docs/roadmap.md §3).
4. **By tier, angle, industry group, sender and step:** last week's sends, replies and meetings, and for
   each the companies emailed so far and how many replied.
5. **Cohorts:** the last four enrolment weeks (a company counts in the UK week its first contact was
   enrolled), each at the latest age its companies have reached after email 1 (7, 14, 21 or 28 days); the
   latest two compared at the same age once each has 30 companies, with what changed between them ("a
   coincidence to test, not a cause"); and **Settings changes**: what changed last week in the settings,
   copy, campaign constants and code, and any change made to campaigns with leads in flight. For the full
   table, `us-outbound cohorts` (`--cut tier`, `--cut config_version`, `--age 14`); for what changed between
   two versions, `us-outbound cohorts changes`.
6. **Signal value:** the signals whose companies replied more or less than the companies without them. For
   the whole table, `us-outbound signals value`. To act on it, change a weight on the Signals tab, then
   `us-outbound sync`. Nothing re-weights itself.
7. **Tests:** a test that reached one of its pre-registered looks last week, with its reply rates, or how
   far each running test has got.

**Small numbers read as small.** A rate on fewer than 30 companies says "too few to read" and gives the
counts only. A bounce rate on fewer than 100 sends says that one bounce moves it a lot.

**Meetings** count bookings on your HubSpot calendar (the signature's "Book a call here", and the website's
demo page, which books into the same calendar) and Spill 3.0 deals at "Demo requested" or later at a company
we emailed. `hubspot_readback` reads them every 15 minutes and stops that company's emails; it only reads
HubSpot. A meeting and a deal for one booking count once.

**Tests are read only at a look you set in advance.** On the Tests tab, before `us-outbound test start ID
--live`:
- `kind`: `ab` (the default) for a copy test between two Copy rows, or `holdout` to read a split the
  system already makes: `opener` against `holdout` (the opener holdout), or `personal` against `copy`
  (email 1's subject). A holdout test can run beside the copy test.
- `looks`: the interim looks, separated by semicolons. A number, like `200`, is "both versions have 200
  companies whose 28-day reply window has closed"; a date is that day. `read_date` is always the last look.

`us-outbound test read ID` reads the test at its latest look, over the companies that look covers. Before
the first look it refuses and shows only how far each version has got, never a reply. Write the result on
the Tests tab. The first time, `us-outbound settings load --tab Tests --live`, then `us-outbound sync`, adds
the `kind` and `looks` columns; your values stay.

**UTM tags (off).** With `utm_links` = yes, the links to spill.chat and your booking link carry UTM tags
(`utm_source=us_outbound`, `utm_medium=email`, `utm_campaign` the Copy row, `utm_content` the email's
number), so website visits and bookings can be traced to the emails. The words of each link are unchanged,
and the Trustpilot and unsubscribe links never get tags. It is off by default: tagged links can read as
marketing to inbox filters, so send a seed with it on and check where it lands before leaving it on. The
first time, `us-outbound settings load --tab General --live` adds the key to the sheet.

## Expected volume in the pilot

Each sender takes new contacts at a quarter of its daily cap, so the follow-ups on days 7, 14 and
21 always fit. In week 1 the ramp holds each mailbox to 10 sends a day, which is about 3 new
contacts a day per mailbox. Expect about **6 cards a day** with Hannah's and Sam's mailboxes, and
about **11** once Harry's two are warm. Volume grows as the ramp rises to 20 sends a day (about 5
new contacts a mailbox) and then 30 (about 8). In the pilot, `weekly_enrol_cap` (150) is well
above this.

## What Slack will ask you to do

Since 7 Oct 2026 the system asks in #us-outbound, with the approvers mentioned, whenever it needs a
decision or a purchase. Each line is posted once (a day, a week or a month, as below), so a line that
comes back is new.

| Ask | When | What to do |
| :- | :- | :- |
| "Apollo has N credits; at this pace it reaches apollo_floor (F) in about D days…" | 09:00, once a day, when the floor is under 21 days away at the last fortnight's pace, or Apollo is already under it (then sourcing and email reveals have stopped) | Buy Apollo credits, or lower `apollo_monthly_credits` on the General tab (below the floor: buy, or lower `apollo_floor`) |
| "Apollo: … credits used this month (80%)", or "… are used" | 09:00, at 80% and at 100%, once a month each | At 100% sourcing and email reveals stop until the 1st. Raise `apollo_monthly_credits` on the General tab to allow more |
| "Clay: … (80%)", or "… are used" | The same, for `clay_monthly_credits` (only while Clay is used) | Raise `clay_monthly_credits` |
| "Claude: $X of the $50 monthly cap…" | 09:00, at 50%, 80% and 100%, once a month each (the month is UTC, as Anthropic counts it) | At the cap, replies come to you as "other" with no draft, and copy QA stops. Raise `claude_monthly_cap_usd` on the General tab (up to $100) and the spend limit in the Anthropic Console |
| "Apollo's website-visitor credits are running low…" | 09:00, once a month, under 15% left, while site visits are on | Buy more in Apollo, or the site-visit signals stop |
| "Add N mailboxes now: a new mailbox takes about 3 weeks to warm up…" | Monday 09:00, once a week, when the Active mailboxes at full ramp take fewer new companies a week than `weekly_enrol_cap`, and enough companies are ready or coming to fill more | Buy the mailboxes, then `us-outbound mailbox add ADDRESS --owner "NAME" --live` for each. The same-day "Add a mailbox for …" line in the daily post stays |
| "Instantly's plan has no room for new leads, so nothing new is being sent…" | When an add finds the plan full, once a day | Upgrade the Instantly plan, or delete leads that finished their sequence. Nobody is suppressed: the contacts wait, and a ✅ already given goes through once there is room (the card says it is held) |
| "Instantly's plan has room for N more leads, under 2 weeks…" | After an add, once a week | The same, before it fills |
| "Errors the jobs met: • enrol … carried on past 2 errors…" | Hourly, once a day per job and kind | Usually nothing: each job tries again on its next run. If it keeps coming, `us-outbound status` and the worker's logs in Railway |
| "HubSpot refused our key or this request: if the key was revoked, replace US_OUTBOUND_HUBSPOT_TOKEN in Railway (Variables), then redeploy; if it is current, the HubSpot plan may not allow this." (or Apollo, Clay, Instantly, Slack, Google, Anthropic) | Hourly, once a day per key | If the key was revoked: make a new one, replace the variable in the us-outbound service's **Variables**, then **Deploy**. If the key is current, check what the plan allows (Apollo answers 403 for a search its plan does not include) |
| "The settings are unusable (…): every job refuses to run…" | Hourly, once a day | Fix the sheet (the errors are in the settings sync's message), then `us-outbound sync` |

**If Slack itself is down or its token is revoked,** none of this can be posted. Healthchecks.io
then emails you, because the hourly check pings it "fail" (or stops pinging, if the worker is down):
docs/railway-setup.md, step h.

## When something is wrong

- **Stop everything:** `us-outbound stop --live`. Resume with `us-outbound start --live`.
- **A kill rule fired:** `us-outbound killrules show`. Once you have checked it,
  `us-outbound killrules clear ID --live` lifts the hold.
- **A mailbox misbehaves:** `us-outbound mailbox pause ADDRESS --live` takes it off its campaign's
  sending list.
- **The mailbox check says a sender name is wrong:** prospects see each mailbox's From name, and it
  should be the owner's full name from the Mailboxes tab ("Hannah Spalding"), not "Hannah at Spill".
  `us-outbound mailbox check` lists what it would change; `us-outbound mailbox check --fix --live`
  sets it in Instantly (the account's first and last name only).
- **The mailbox check says "Campaign drift held":** a deploy changed the step template, delays or
  text_only, and the campaign still has leads in flight, so their remaining emails are left as they
  were. Either wait until `us-outbound cohorts in-flight` shows none for that campaign and run
  `us-outbound campaigns ensure --fix --live`, or apply it to them too with
  `us-outbound campaigns ensure --fix --in-flight --live` (docs/developing-while-live.md).
- **A card says "Not sent":** the re-check at your ✅ found the person or company can no longer be
  emailed (an unsubscribe, a customer or open deal in HubSpot, a suppressed domain). Nothing was
  sent and the card is closed; there is nothing to do. A card that is only *held* (sending stopped,
  a reply waiting too long) stays open and your ✅ stands: the lead is added within 5 minutes of the
  hold clearing, unless the card expires first.
- **The daily post says a campaign is not active:** that sender gets no new cards until
  `us-outbound start --live` activates it. The 07:00 mailbox check activates a newly created
  campaign by itself once sending has gone live. A campaign Instantly shows as *completed* is fine:
  Instantly marks an active campaign completed whenever it has no lead left to email (straight after
  `start`, if it has none yet), and the next lead added resumes it.
- **A missed-heartbeat alert:** a job has not run when it should have. `us-outbound status` lists
  the jobs that failed or missed, with the error. The worker's logs are in Railway.
- **Someone asks to be forgotten:** `us-outbound erase --email ADDRESS --live`, then do the manual
  steps it prints.

## Website visits

`site_visits` runs at 06:00 UK every day. It asks Apollo which companies visited spill.chat's US
pages (General `site_visit_us_paths`, `/us`) and its pricing and demo pages (`site_visit_intent_paths`)
in the last 30 days. A visit lifts that company's score before the 12:00 enrol ("Visited the US site"
+35, "Viewed US pricing or demo page" +25, so both make Priority). A new visitor is looked up in
Apollo: one in the US (in any state: a visitor is never excluded on its state) within the General size
range joins the queue, and one Apollo can't place (no HQ state or country,
no size, no industry, a size near an edge) comes in held for Monday's hand-check rather than being
left out. It costs about 3 Apollo credits a day, plus 1 for each new visitor looked up. The daily
post's Sources section has one line on it. The emails never mention a visit.

**If the daily post says "No website-visitor data from Apollo for spill.chat",** check the tracker.
The jobs only read Apollo's visitor list and never touch the tracker, so this is done by hand:
- In Apollo, Settings → Website Visitors: spill.chat is listed and shows data received, and the plan
  includes website visitors.
- Open spill.chat/us in a browser with the developer tools' Network tab open: a request goes to
  Apollo when the page loads. If none does, the tracking script is missing from the US pages.
- While you are there: the intent path should read `/us/pricing` (it was `us/pricing`, without the
  slash), `/us/book-demo` should be added as high intent, and the script should not be on
  employee-facing pages.

## Weekly

- **Focus tab:** the industry mix (Tech 50%, Agencies 30%, Legal 20% to start). Change the shares there.
- **Copy:** approve rows on the Copy tab (`status` approved, `approved_by`). Editing a row clears its
  QA pass, so run `us-outbound copy qa --live` after an edit, then `us-outbound sync`. Add `--active` to
  re-check only the rows that can be sent now (the active industries' and General's), which keeps the spend
  well inside the month's Claude cap; run it without `--live` first to see the rows and the most it can cost.
  Every email has two links at most, body and signature together (Harry, 6 Oct 2026): the body's one link is
  its call to action, and a row with a second body link fails the check and is never sent.
- **Volume:** raise `weekly_enrol_cap` on the General tab as the ramp rises and the exit criteria
  hold (docs/roadmap.md §3).
- **Email 1's subject split** (5 Oct 2026): half the companies (General `email1_subject_share`, 0.5)
  get the personal subject `email1_subject` ("support for the {{company}} team"; `{{company}}` and
  `{{first_name}}` only) in email 1 instead of the Copy row's. Emails 2 to 4 are unchanged. Each
  company keeps its arm, and `us-outbound signals review` says which subject gets more replies once
  each arm has 30 companies emailed. To change the subject or the share, edit them on the General tab,
  then `us-outbound sync`; 0 sends every email 1 with the Copy row's subject. The first time, run
  `us-outbound settings load --tab General --live` to add the two keys to the sheet. To see the
  personal subject: `copy preview --subject personal`, or `seed send ADDRESS --owner NAME --subject
  personal --live` for a seed inbox.

## Monthly

- **Lookalikes (the 1st, 02:30 UK):** the `lookalikes` job reads Spill's customers from HubSpot (read
  only), their 12-month headcount growth from Apollo (counts only, at most 60 credits), and works out
  each account's lookalike fit (industry, size and growth). Nothing to do. Customers themselves are kept
  out every night: `suppression_load` (01:30) reads them too, so a company that signs up mid-month gets no
  more emails from the next morning (7 Oct 2026). `us-outbound lookalikes show`
  lists the customers by industry and size; `us-outbound lookalikes fit` shows the fits and the tier
  mix the lookalike rows give, without changing anything. To put the graded rows on the sheet, run
  `us-outbound settings load --tab Signals --live`: it adds "Close match" and "Some match to Spill's
  customers" and switches "Looks like Spill's customers" off.
- **The 1st, 02:50 UK: lookalike leads** (`lookalike_leads`). Apollo looks for US companies like
  Spill's active customers, UK customers included, in the Focus tab's industries and the same size
  band, and the new ones join the queue. They still go through the usual checks (HQ state, size,
  HubSpot, the hand-check if a fact is doubtful), and they score the "Found as a lookalike of a
  customer" signal (+10). Nothing arrives in Slack on its own: that morning's daily post says what
  it found under **Sources**, with how the UK customers' searches did beside the US customers'.
  It spends at most 60 Apollo credits. `us-outbound run lookalike_leads` runs it by hand; a second
  run in the same month does nothing, since the customers are much the same.

## Where the data is

The companies and contacts live in the Postgres database on Railway, schema `us_outbound`: the
`accounts` table (one row per company, by its domain) and `contacts` (one row per person). Beside
them are `signal_events` (what the sources found), `events` (sends, replies, bounces, opt-outs),
`hitl_items` (the Slack cards) and `suppression`. The database has no public access, so look at it
with `us-outbound accounts`: a summary, then the companies in queue order (`--status`, `--tier` and
`--industry` narrow the list). `us-outbound accounts acme.com` shows one company in full, with its
contacts' emails. For a spreadsheet, run `railway ssh -- us-outbound accounts --csv > companies.csv`
on your own computer; it holds names and emails, so keep it private and delete it when you are done.

**People leaders** (5 Oct 2026): each weekday at 04:20, before the queue is sorted, `apollo_people`
looks up who leads People at every company in the queue in Apollo's free people search, so "New
People leader" and "People leader in place" count for companies nobody has contacted yet. "First
People hire (likely)" counts only where Apollo knows at least half the company's staff, so that
finding no People leader there means something. Once, after this reaches the worker:
`us-outbound settings load --tab Signals --live`, then `us-outbound sync`.

**Don't edit rows by hand in Railway's Data tab.** An edit there bypasses the system's checks
(suppression, one company per domain, a sender kept for life). Make changes with the sheet and the
commands instead.

## The commands

| Command | What it does |
| :- | :- |
| `status` | The switches, when the settings were synced, what waits for you, the jobs that need a look, mailboxes, today's number |
| `golive` | The read-only go/no-go check |
| `accounts`, `accounts DOMAIN`, `accounts --csv` | The companies and contacts we hold: a summary and the list, one company in full, or a spreadsheet. Read-only |
| `sync` | Brings sheet edits into force now |
| `start --live` | Syncs, then resumes the campaigns and enrollment |
| `stop --live` | The brake |
| `seed send ADDRESS --owner NAME --live`, `seed check` | The seed-inbox test of the unsubscribe link (`--subject personal`: email 1 with the personal subject) |
| `approvals list`, `approvals send ID --live` (or `contact ID`, `company ID`) | Send cards without Slack |
| `replies list`, `replies send ID --live` (`--text "…"` sends your text), `replies skip ID --live` | Reply cards without Slack |
| `killrules show`, `killrules clear ID --live` | Kill-rule holds |
| `mailbox check --live --fix` | Mailbox health now, each sender name set to its owner's full name, and the campaigns put right |
| `copy preview --industry "Fintech" --html fintech.html`, `copy qa --live` | An email as a prospect will see it; QA for edited rows |
| `readout` | The Monday readout for last week, printed and not posted |
| `cohorts`, `cohorts changes`, `cohorts in-flight` | Each enrolment week at 7, 14, 21 and 28 days after email 1 (`--cut`, `--age`, `--weeks`); what changed between the last two config versions (or `cohorts changes A B`); each campaign's leads with a step still to send |
| `campaigns ensure --fix --in-flight --live` | Applies campaign drift in the steps, delays or text_only to the leads already in flight too (held otherwise; docs/developing-while-live.md) |
| `signals value`, `signals review` | The signal table (with meetings, against the companies without each signal), or each signal's verdict, with the tiers and email 1's subject |
| `test start ID --live`, `test read ID` | Start a test on the Tests tab; read it at its latest pre-registered look |

Also `handcheck show|approve`, `erase --email` and `schedule`. `us-outbound --help` lists every command.
