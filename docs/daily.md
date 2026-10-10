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
| 09:00 | The daily post, short since 9 Oct 2026: **Needs you** (one bullet per thing to do), **Yesterday** (sends and outcomes in one line, what was found in another, any positive reply or objection), **Today** (up to how many new contacts, what limits it, each sender's share, the week so far), **Pipeline** (ready, and what stands behind it), and **Watch** only when something needs a look (a loud mailbox, a kill rule, a campaign not sending, what the data says to tune) | Read **Needs you** first. `us-outbound daily --full` prints the full post as it was before: sources, labels, every mailbox and the credit budgets. The Monday readout carries the weekly detail |
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
| ✅ or `send` (or `approve`) | ✅ or `send` (or `approve`) | Sends it. A send card's lead goes to Instantly within 5 minutes |
| ❌ or `skip` (or `no`, `reject`, `don't send`) | ❌ or `skip` (or `no`, `reject`, `don't send`) | A send card offers the three choices below. A reply card is closed, and nothing is sent |
| ✏️ (or `edit`), then a thread reply with the new email 1. Put "Subject: …" first, or "Email 2:" to change a follow-up | `edit: …` | The edit is checked against the copy rules and posted again. ✅ on it sends it |
| — | `send: …` | Sends that text instead of the draft |
| 👤 | — | Not this person: the next-ranked contact at the company is proposed later |
| 🚫 | — | Drop the company for good |
| `industry: Fintech` (any label on the Industries tab; any case, and a unique part of one, like `games`, will do) | — | The company is that, not what the card says: its label is set, kept on the Overrides tab, the card is withdrawn and a new card with that label's emails is posted at once (if it can't be, the next enrol proposes the company). Works whether you have ❌'d it or not. An unknown label gets the list of labels in the thread |

Each send card's **Industry** line says how its label was checked (Harry, 7 Oct 2026; the label check):
`rules and model agree · Fintech copy` is the normal case; `⚠️ … · the rules say X, the model says Y (medium) ·
General copy` means they disagreed and the email is the safe General one; `⚠️ … · not checked by the model` is a
card from before the check, or with `label_check` = `skip`. **They do** is what the model read the company does,
with its quote. If either is wrong, reply `industry: <label>`.

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
6. **Industry labels** (Harry, 7 Oct 2026): last week's and all cards with the right industry against the 95%
   target ("met" or "not met", from 30 cards), the corrections approvers made (`Games studios → Technology &
   Startups (2)`), how often the rules and the model agreed, and the companies held or left out. Many of one
   correction means a definition or keywords on the Industries tab need a change; run `us-outbound labels eval
   --live` before and after.
7. **Signal value:** the signals whose companies replied more or less than the companies without them. For
   the whole table, `us-outbound signals value`. To act on it, change a weight on the Signals tab, then
   `us-outbound sync`. Nothing re-weights itself.
8. **Tests:** a test that reached one of its pre-registered looks last week, with its reply rates, or how
   far each running test has got.

**Small numbers read as small.** A rate on fewer than 30 companies says "too few to read" and gives the
counts only. A bounce rate on fewer than 100 sends says that one bounce moves it a lot.

**Meetings** count bookings on your HubSpot calendar (the signature's "Book a call here", and the website's
demo page, which books into the same calendar) and Spill 3.0 deals at "Demo requested" or later at a company
we emailed. `hubspot_readback` reads them every 15 minutes and stops that company's emails; it only reads
HubSpot. A meeting and a deal for one booking count once.

**Tests are read only at a look you set in advance.** On the Tests tab, before `us-outbound test start ID
--live`:
- `kind`: `ab` (the default) for a copy test between two Copy rows; `variant` for one change to one email
  for every company (see **Copy tests** below); or `holdout` to read a split the system already makes:
  `opener` against `holdout` (the opener holdout), or `personal` against `copy` (email 1's subject). One
  copy test (`ab` or `variant`) runs at a time; a holdout test can run beside it.
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

## Copy tests

Harry, 7 Oct 2026: "A/B functionality to allow the system to try different versions of email copy". A
`variant` test on the Tests tab changes one part of one email for every company enrolled from its
`start_date`, whatever its tier, industry or Copy row, and splits them half and half (or as `share_a`
says; below): version_a's companies get `text_a`, version_b's get `text_b`. Its own columns:

| Column | What it is |
| :- | :- |
| `email` | Which email of the four it changes: 1 to 4 (blank is 1) |
| `change` | `first_line`: a line of its own straight after "Hi {first name}," and before the opener line. `last_line`: a line of its own just before "Best wishes,". `replace`: the exact words in `find`, as written on the Copy tab, become the arm's text. `subject`: the email's subject |
| `text_a`, `text_b` | Each version's text. A blank one leaves that version's email as the Copy row has it |
| `find` | For `replace` only: the words to replace |

The sync checks the texts against the copy rules (no "!", American spelling, no demo or call ask in email
1, no link in a line of its own, a subject under 60 characters, and so on) and refuses a row that breaks
one, saying why. A text may use `{{first_name}}`, `{{company}}` and the other variables, but not
`{{opener}}` or `{{legal_overlay}}`.

- **Who gets which.** A company's version comes from a hash of the company, so it never changes: a card
  posted again, an industry correction or a second contact at the company get the same one. It is
  independent of the opener holdout and the personal-subject split, so those comparisons still hold.
- **Where the change cannot be made** (a `replace` whose words are not in that company's Copy row; a
  `subject` test on email 1 for a company with the personal subject; or a change that would break a copy
  rule in that email, such as a line made too long by a long company name), the company is left out of the
  test whichever version it hashed to, and gets the Copy row's email as it is. Its card says "Test:
  warm-intro · not in the test (why)", and the enrol summary counts the reasons. `us-outbound test start`
  says how many Copy rows the change fits. The warm intro fits every email 1.
- **The card** shows the version's email, and its facts line ends "Test: warm-intro · warm intro". You
  can edit it as any card: it keeps its version, and the read counts it there and says how many of each
  version's emails were edited.
- **Contacts already in flight keep their emails.** A test reaches only companies enrolled after it
  starts; when it stops (set `status` to `read` or `stopped`, then `us-outbound sync`), new companies get
  the Copy row as it is. Once a version has `accounts_per_version` companies, the rest get the Copy row as
  it is too. `us-outbound cohorts changes` shows the day a test started or stopped.

**Your first test: the warm intro.** First add five header cells to the Tests tab, after `looks` (its last
column now): `email`, `change`, `text_a`, `text_b`, `find`. Then this row:

| Column | Value |
| :- | :- |
| `test_id` | `warm-intro` |
| `kind` | `variant` |
| `hypothesis` | A warm first line in email 1 ("I hope you're really well. Great to be connected.") gets a higher reply rate than none |
| `version_a` | `warm intro` |
| `version_b` | `no intro` |
| `accounts_per_version` | `400` |
| `status` | `planned` (`test start` sets `running`) |
| `start_date` | blank (`test start` sets today) |
| `looks` | `200; 400` |
| `read_date` | `2027-03-29` |
| `decision_rule` | reply rate, human replies within 28 days of step 1 ÷ accounts with step 1 delivered; detects a 2× difference; keep the warm intro if its reply rate is higher at p < 0.10 at the last look |
| `result` | blank |
| `email` | `1` |
| `change` | `first_line` |
| `text_a` | `I hope you're really well. Great to be connected.` |
| `text_b` | blank |
| `find` | blank |

Then `us-outbound sync` (it refuses the row if anything is wrong, and says what), `us-outbound test start
warm-intro` to see what it would do, and `us-outbound test start warm-intro --live`. Read it with
`us-outbound test read warm-intro`; before its first look it shows only how far each version has got.

Why these numbers. SPEC 12 asks a test to detect a 2× difference in reply rate. At the working assumption
of a 3 to 5% reply rate, 4% against 8% needs about 435 companies a version to be found four times in five
at p < 0.10. 400 a version finds it about three times in four (two times in three at 3% against 6%, 85% of
the time at 5% against 10%). So a read of "no difference" means "not twice as many", not "no effect": a
one-line change may move replies by less than that, which this test cannot see. The test takes every new
company, so it fills at the full enrolment rate: about 6 a day with Hannah's and Sam's mailboxes, 11 with
all four, rising with the ramp; call it 10 a day. Started on Monday 12 October, it has 200 a version by
about 11 December and 400 a version by about 23 February (the blackouts take 16 send days). Each
company's 28-day reply window then has to close:
- look 1, `200`: about 8 January. An interim look: act on it only for a large difference or harm.
- look 2, `400`: every company in the test has had its 28 days: about 23 March at 10 a day, mid-February at
  15 a day. This is the full read; the read date, if later, reads the same companies.
- `read_date`, 2027-03-29: the backstop. If volume runs slower (6 a day), the read on that date covers the
  companies whose windows have closed by then.

**An uneven split: `share_a`** (Harry, 8 Oct 2026: "a warm greeting on most but not all of the email
1s"). A `share_a` column after `find` sets the share of companies that get version_a: `70%` (or `0.7`)
gives version_a seven companies in ten and version_b three. Blank is half and half. It works for `ab` and
`variant` tests, from 10% to 90%; a holdout test reads a split the system already makes, so it takes none.
The first time, add the `share_a` header cell after `find` (or `us-outbound settings load --tab Tests
--live`, which keeps your values).
- `accounts_per_version` and the count looks are counted in the smaller version, and the larger takes
  its share more: at 70/30, `290` is 290 companies with no intro and 677 with the warm intro, and a look
  at `145` is 145 and 338. `test start` says the split, and the read compares the two versions' reply
  rates whatever their sizes.
- The cost is time. The smaller version decides when a look is reached, and an uneven split needs more
  companies for the same power: 1.2 times as many at 70/30, 1.6 at 80/20, 2.8 at 90/10. For the warm
  intro at 70/30, `290` in the smaller version (967 in all) finds the same 2× difference as 400 a
  version did half and half (800 in all), about 17 send days later: look 2 in mid-April at 10 a day.
- Change the split by starting a new test, not by editing a running one: companies already in the test
  keep their version, so a mid-test change mixes two splits in one read.

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
| "Claude: $X of the $50 monthly cap…" | 09:00, at 50%, 80% and 100%, once a month each (the month is UTC, as Anthropic counts it) | At the cap, replies come to you as "other" with no draft, copy QA stops, and new companies wait unverified for the industry label check (those already verified carry on). Raise `claude_monthly_cap_usd` on the General tab (up to $100) and the spend limit in the Anthropic Console |
| "Apollo's website-visitor credits are running low…" | 09:00, once a month, under 15% left, while site visits are on | Buy more in Apollo, or the site-visit signals stop |
| "Add N mailboxes now: a new mailbox takes about 3 weeks to warm up…" | Monday 09:00, once a week, when the Active mailboxes at full ramp take fewer new companies a week than `weekly_enrol_cap`, and enough companies are ready or coming to fill more | Buy the mailboxes, then `us-outbound mailbox add ADDRESS --owner "NAME" --live` for each. The same-day "Add a mailbox for …" line in the daily post stays |
| "labels: N industry corrections since the last send day…" | 09:00, once a day, when you corrected 3 or more cards' industry, or 10% of at least 10 | The line names the moves (`Adtech & martech → Fintech (3)`): fix that label's `definition` or `apollo_keywords` on the Industries tab, `us-outbound labels eval --live`, then `us-outbound labels audit --live` |
| "labels: the model put N of the M companies the rules labelled (P%) in another group or outside our labels…" | 09:00, once a day, when the model agreed on the group for under 70% of at least 20 companies the rules labelled (each counted once; the model naming a label within the rules' group, such as Publishers for Marketing & Creative Agencies, agrees) | The rules' NAICS codes or keywords for the group it names bring in the wrong companies: `us-outbound labels crosswalk` says which, then trim them on the Industries tab |
| "labels: the rules gave no label to N of M companies checked…" | 09:00, once a day, when at least 20 companies and 30% of those checked had no rules label | The line names the model's commonest labels for them and their commonest NAICS codes (`541511 → Software & SaaS (9)`): add each code to that label's `naics_prefixes` on the Industries tab, then `us-outbound sync` and `us-outbound relabel --live` |
| "The label check did not run (…); N companies wait unverified" | 09:00, once a day, while the model cannot be asked (the Claude cap, an error) | At the cap, raise `claude_monthly_cap_usd`; on an error, it usually clears itself next run. Verified companies carry on meanwhile. To run on the rules alone, set `label_check` to `skip` |
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
- **A card's industry is wrong** (a fulfilment firm pitched as a games studio): reply `industry: <label>`
  in its thread (or `us-outbound approvals industry ID "<label>" --live`). The label is set and kept
  on the Overrides tab, the card is withdrawn, and a new card with the right emails is posted in its
  place; 🚫 if the company is no fit at all. For a company with no card,
  `us-outbound labels set DOMAIN "<label>" --live`; `us-outbound labels show DOMAIN` gives its label's
  history. Since 7 Oct the task model checks each company's label before it can get a card (the
  label check, docs/pipeline.md stage 3), so this should be rare: the full daily post's **Labels** line
  (`us-outbound daily --full`) counts the corrections, and an ask names them when there are several. `us-outbound relabel` shows what the rules and the stored checks change for
  the companies in the queue; `--live` applies it and withdraws the open cards it changes. Government
  domains (.gov, .mil) are never prospected.
- **Start the waiting cards again:** `us-outbound approvals redo all --live` (or one card: `approvals redo
  ID --live`) withdraws each waiting card and posts it again for the same person and sender, with the
  company's label and emails as they are now (after a `labels audit --live`, say). A company that may no
  longer be emailed is not posted again; one that cannot be made ready now goes back to the queue.
- **A missed-heartbeat alert:** a job has not run when it should have. `us-outbound status` lists
  the jobs that failed or missed, with the error. The worker's logs are in Railway.
- **Someone asks to be forgotten, or Apollo sends a deletion notice:** within 30 days,
  `us-outbound erase --email ADDRESS --live`, then do the manual steps it prints (below, **Erasure and
  Apollo deletion notices**).

## Erasure and Apollo deletion notices

Apollo emails a deletion notice when someone has asked Apollo to remove their data, and we honour each one
within 30 days (SPEC 2). Apollo has no API that lists these notices, and the jobs only ever read Apollo, so
nothing picks them up by itself. For each notice, and for anyone who asks us directly to be forgotten:

1. Within 30 days of the notice, run `us-outbound erase --email ADDRESS` to see what it will do (it already
   clears our database), then `us-outbound erase --email ADDRESS --live`.
2. It deletes the person from our database: their contact rows, the text of their replies, what their cards held
   in the database, Clay's raw rows, and anywhere else the address is written, such as a colleague's reply naming
   them. It GDPR-deletes their HubSpot contact, deletes their leads from every US Outbound campaign, and puts the
   address's hash on suppression, so no job ever stores, proposes or emails them again. It prints what it did for
   each system, with the address only as a hash; the run's heartbeat keeps that report as the record it was done.
3. Do the manual steps it prints: Clay's rows (Clay has no API for this) and the Unibox check in Instantly; and,
   when they apply, the Slack cards that showed them (the jobs never delete a Slack message), the escalation
   email of their reply, and the note and task the reply desk added in HubSpot, which GDPR delete leaves on the
   company.

If they unsubscribed earlier, the Instantly blocklist keeps their address. That is the one copy kept on purpose,
so that no campaign in the workspace emails them again, as our suppression list keeps their hash.

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

**Retention** (7 Oct 2026; SPEC 6 and 13). The `retention` job runs at 00:40 UK every night and needs nothing from
you. It deletes each Instantly lead 31 days after its last step (or after the reply, bounce, unsubscribe or booking
that stopped it, or the last email of a conversation with them), so the Instantly plan's lead count stays down; it
never deletes a lead still being emailed, one whose reply waits for you, or one whose unsubscribe or bounce the jobs
have not recorded yet. It clears the text of replies 90 days after they came
(the class, dates and ids stay, so the readout counts as before). It deletes the people who never replied 12 months
after their last email, and companies no source has seen again in 12 months that nothing else holds. The
suppression list is never touched. When it deleted anything, the daily post ends with one line of counts, and
`us-outbound status` says what it last did and what it is holding back and why. `us-outbound run retention` shows
what is due now without changing anything. Like every job it stays dry while `live_sending` is no. Copies outside
the database are not its to delete: replies quoted in Slack (set #us-outbound's message retention in Slack if you
want them to go too), escalation emails, and the notes of warm replies in HubSpot.

**Don't edit rows by hand in Railway's Data tab.** An edit there bypasses the system's checks
(suppression, one company per domain, a sender kept for life). Make changes with the sheet and the
commands instead.

## The commands

| Command | What it does |
| :- | :- |
| `status` | The switches, when the settings were synced, what waits for you, the jobs that need a look, what retention last deleted, mailboxes, today's number |
| `daily`, `daily --full` | The daily post as it would read now, short or in full; it posts nothing |
| `golive` | The read-only go/no-go check |
| `accounts`, `accounts DOMAIN`, `accounts --csv` | The companies and contacts we hold: a summary and the list, one company in full, or a spreadsheet. Read-only |
| `sync` | Brings sheet edits into force now |
| `start --live` | Syncs, then resumes the campaigns and enrollment |
| `stop --live` | The brake |
| `seed send ADDRESS --owner NAME --live`, `seed check` | The seed-inbox test of the unsubscribe link (`--subject personal`: email 1 with the personal subject) |
| `approvals list`, `approvals send ID --live` (or `contact ID`, `company ID`, `industry ID "Fintech"`) | Send cards without Slack |
| `labels set DOMAIN "Fintech" --live`, `labels show DOMAIN` | Set a company's industry label (as `industry: Fintech` on a card does), or show its label and its check history |
| `labels audit`, then `labels audit --live` | The label check for the whole queue now, rather than about 150 companies a weekday: the dry run says how many, what it costs at most (about $0.01 each) and shows a prompt; `--live` first reads the home page of each company the model was unsure of (public pages, no paid service) and asks about those again with it, then asks, decides, lists where the rules and the model differ, and withdraws the cards that no longer fit |
| `labels crosswalk`, `labels crosswalk --live` | How well the Industries tab translates Apollo's industry, NAICS codes and keyword tags into labels, measured on the companies whose label is known (your corrections, and the model's sure verdicts). It prints the translations that are mostly wrong ("naics 541613 → Marketing & Creative Agencies: 5 companies, 0% right; poor: mostly Adtech & martech"), those that place the group but not the label, and Apollo values the tab does not translate but could. `--live` also writes every row to a Crosswalk tab on the settings sheet. It asks no model and spends nothing |
| `labels sample --live`, then `labels sample --score` | Labels judged by you, not by the label check: 50 companies drawn at random from those emailed or to be emailed (`--size` for more), written to a Label sample tab with each one's site, what it does, our label, the copy it gets and how the label was decided. Fill `your_verdict` with `right`, `right group`, `wrong group` or `outside` (and `right_label` when ours is wrong); `--score` prints the share right with a 95% interval and how many emails would have carried a pitch that is not true. A tab that already holds a verdict is never redrawn. It asks no model and spends nothing |
| `labels eval --live` | Scores the model on the first cards' 13 companies (about $0.15; `--from-corrections` adds the companies approvers corrected). Run it before changing a definition, keywords or the prompt; it exits 1 below 90% acceptable or on any unsafe answer |
| `replies list`, `replies send ID --live` (`--text "…"` sends your text), `replies skip ID --live` | Reply cards without Slack |
| `killrules show`, `killrules clear ID --live` | Kill-rule holds |
| `mailbox check --live --fix` | Mailbox health now, each sender name set to its owner's full name, and the campaigns put right |
| `copy preview --industry "Fintech" --html fintech.html`, `copy qa --live` | An email as a prospect will see it; QA for edited rows |
| `readout` | The Monday readout for last week, printed and not posted |
| `cohorts`, `cohorts changes`, `cohorts in-flight` | Each enrolment week at 7, 14, 21 and 28 days after email 1 (`--cut`, `--age`, `--weeks`); what changed between the last two config versions (or `cohorts changes A B`); each campaign's leads with a step still to send |
| `campaigns ensure --fix --in-flight --live` | Applies campaign drift in the steps, delays or text_only to the leads already in flight too (held otherwise; docs/developing-while-live.md) |
| `signals value`, `signals review` | The signal table (with meetings, against the companies without each signal), or each signal's verdict, with the tiers and email 1's subject |
| `test start ID --live`, `test read ID` | Start a test on the Tests tab (an `ab`, `variant` or `holdout` test; see **Copy tests**); read it at its latest pre-registered look |

Also `handcheck show|approve`, `erase --email` (and for an Apollo deletion notice), `run retention` and `schedule`.
`us-outbound --help` lists every command.
