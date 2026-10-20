# US Outbound: your day

For Harry, from the pilot that starts on Monday 5 October 2026. Run every command inside the
worker: `railway ssh -- us-outbound <command>`. Nothing changes without `--live`.

## The switches (General tab)

| Key | no | yes |
| :- | :- | :- |
| `live_sending` | Nothing new reaches Instantly or a prospect. Jobs run dry | Emails go out: each send day's leads go straight to Instantly. The sheet refuses yes while `approver_slack_ids` is blank, and no lead is added until `optout_tested` = yes |
| `hand_check_gate` | Your D5 (19 Oct 2026): the hand-check is a measure. Only the companies it picked wait for your decision, for 7 days at most; everyone else is emailed as usual | The default until you set no: no new leads go to Instantly in a week until every company picked at random has a decision |

**Company size (General `min_employees`, `max_employees`; 10 and 249 by default).** To contact companies of 5
to 500, set 5 and 500. From the next sync every search covers the new range (new size bands are searched
from page 1), verify_accounts holds accounts to it, and the hand-check's size edges move with it. The
Roles tab's 10-49 order applies below 10 and its 50-249 order above 249. The first time, run
`us-outbound settings load --tab General --live` to add the two keys to the sheet.

**Removed on 20 Oct 2026 (W2-1; your decisions of 19 Oct):** the send cards and `auto_send` (each send day's
leads go straight to Instantly, as they did with `auto_send` = yes), second contacts (`second_contact`: one person
per company), every Clay path and the one-off commands `golive`, `approvals`, `phase0 check`, `clay check-email`
and `labels crosswalk`. Their General rows (`auto_send`, `second_contact` and its two keys, the seven Clay keys,
the never-read `recontact_*`, `hubspot_deal_stage`, `apollo_credits_per_account`, `opener_focus` and
`opener_focus_line`) are no longer read; the sync lists them, and you can delete the rows (`us-outbound settings
load --tab General --live` takes them off). `us-outbound status` now ends with the three go-live checks that still
matter: Copy, Campaigns and Watchdog.

**Removed on 20 Oct 2026 (W2-2; your D7 of 19 Oct):** the Priority, Standard and Control tiers and their
thresholds. Companies are taken by score (then size band, then industry priority), and `explore_share` of each
day's are picked at random from the queue within the Focus shares instead, to show whether the score helps: 0.15
by default (your "Rank + 15% random arm"), so it runs with no row; 0 on the General tab switches it off. The rows
`priority_threshold`, `standard_threshold` and `auto_thresholds` are no longer read (the sync lists them; `settings
load --tab General --live` takes them off), and the tier-mix alerts went with them. A company's tier is now Queue,
Held or Excluded. The Signals rows Nonprofit budget, Nonprofit fiscal year ahead and Named by Harry never fire (no
source writes their facts) and can be deleted.

**Sheet edits apply at the next sync:** 02:00 UK every day, and 11:30 UK on weekdays, so a morning
edit is in force for the 12:00 enrol. To apply an edit now, run `us-outbound sync`.
`us-outbound start --live` syncs by itself first. `us-outbound status` says when the settings were last synced.

## The brake

`us-outbound stop --live` pauses every US Outbound campaign in Instantly and stops new enrollment.
**Setting `live_sending` = no does not stop Instantly:** a campaign that is already active keeps
sending its follow-ups. To resume, run `us-outbound start --live`. It syncs the sheet, checks the
campaigns still match the settings, and activates them.

If the database is down, `us-outbound stop --live --no-db` still pauses every US Outbound campaign
through Instantly and posts in the alert channel. It cannot record the stop, so enrol would go on adding
leads to the paused campaigns (they send nothing): run `us-outbound stop --live` once the database is back.

If the settings sheet has an error that leaves the jobs without usable settings, the safety jobs (the
kill rules, the blackout pause, reading replies and email results, the do-not-contact list, the health
check and `stop`) keep running on the last good version of the failing tab, or on the defaults with the
Mailboxes and States tabs. They stay live if `live_sending` was yes, but only pause and stop: nothing is
started, added or sent until the sheet is fixed. The settings sync says so in Slack once a day.

## Blackout dates

The General tab's `blackout_dates` (23 to 27 November 2026, and 18 December 2026 to 4 January 2027) are days
nothing is sent. Enrol skips them, and a ✅ whose email 1 would land on one waits. Instantly's own schedule
knows weekdays only, so since 7 October 2026 the hourly `blackout` job also pauses every US Outbound campaign
once the send window has closed (16:00 ET) on the last send day before a blackout, and starts the same
campaigns again from midnight ET after it. The follow-ups that fell due go out on the next send day (Monday
30 November; Tuesday 5 January), as the send forecast already assumed. One line in #us-outbound says when it
pauses them and one when it starts them again. `us-outbound status` (its Campaigns check too) and the daily post say "paused
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
| 07:00 | Mailbox check, only when something changed or is wrong: a mailbox promoted to Active, a campaign's daily limit raised with the ramp, a new campaign created or activated, a sender name that is not the owner's full name | Usually nothing. If it asks you to run `us-outbound start --live` or `us-outbound mailbox check --fix --live`, run it |
| Monday 05:45 | The weekly hand-check: `hand_check_sample` companies (10 until you set 20) picked at random from those the coming send days would email, after today's own (so Monday's sending is never held up). Since 14 Oct nothing is held here for doubtful facts or an unclear industry: a company missing its HQ state, size or industry is left out by itself ("incomplete information"), as is one the AI cannot place in our industries, and a borderline size (near a band edge) goes ahead on Apollo's count. With `hand_check_gate` = no only the picked companies wait for you, for 7 days at most; with yes the week's new leads wait for the whole check. A check still open on Wednesday 09:00 posts one reminder. If the Monday draw fails, the 5-minute poll draws the week itself from 06:15; with the whole-day gate, a week that found no company ready at 05:45 is drawn again once one is | Open the **Hand-check** tab (the post links it) and pick a `decision` on each row: *fine*, *leave out* (never emailed: say why in `reason`), or *wrong industry* with the right one in `right_label`; pick *no* in `contact_ok` or `opener_ok` when the person or the opening line is wrong. Each choice is applied within 5 minutes, and a change later is applied too (a leave out changed to fine puts the company back). A choice that cannot be applied says why in `note`. The check closes when every row is applied, or after 7 days; the Monday readout counts the decisions and any company sent unjudged (`handcheck approve` still works from a terminal). Every week's rows stay on the tab, and each applied row is kept in the database as a judgement |
| Monday 08:30 | The Monday readout: last week, the operating metric, the north star as counts until it can be read, throughput and supply, ICP precision and labels, delivery and capture, the splits, the tests, the forecasts, the decisions waiting | See **Mondays: the readout** below |
| 09:00 Tue–Sat | The daily post, short since 9 Oct 2026, Tuesday to Saturday since 19 Oct 2026 (each covers the day before alone: Saturday's is Friday, Tuesday's is Monday; weekend replies are in the Monday readout): **Needs you** (one bullet per thing to do), **Yesterday** (sends and outcomes in one line, what was found in another, any positive reply or objection), **Today** (up to how many new contacts, what limits it, each sender's share, the week so far), **Pipeline** (ready, and what stands behind it), and **Watch** only when something needs a look (a loud mailbox, a kill rule, a campaign not sending, what the data says to tune) | Read **Needs you** first. From 20 Oct 2026 it also asks when sourcing has stalled (no new company and no new contact on the last 3 send days while 50 or more wait), when the week's reply-capture check found Instantly's counts and ours apart or the canary did not come back, and when yesterday's enrolment was held by a gate or lost leads to errors (not when the gate was an operator's own `us-outbound stop`: the pause someone chose). `us-outbound report daily --full` prints the full post as it was before: sources, labels, every mailbox and the credit budgets. The Monday readout carries the weekly detail |
| 12:00 | Enrol adds the day's leads to Instantly (no card since 20 Oct 2026) | Nothing |
| Any time | Reply cards, each with a draft | Answer within 2 hours, or the card is re-posted (13:00 to 23:00 UK). Answer within 24 hours: a positive reply waiting longer pauses new sends and is emailed to you |
| Hourly | Kill-rule alerts: a mailbox, an email source or an industry group held back, with the reason | Check it, then `us-outbound killrules show` and `us-outbound killrules clear ID --live` |
| Around a blackout | "Paused N US Outbound campaigns for the blackout…" (the evening before, UK time), then "Started N … again after the blackout" | Nothing (see **Blackout dates** above). A "Left paused" line names what to run |

## The words and emojis

Reply in the reply card's thread, or react to the card. Only an approver counts: `approver_slack_ids`,
and a mailbox's owner for replies to their own mailbox. The bot puts ✅ and ❌ on each card, so
one click decides.

| On a reply card | What it does |
| :- | :- |
| ✅ or `send` (or `approve`) | Sends the draft |
| ❌ or `skip` (or `no`, `reject`, `don't send`) | The card is closed, and nothing is sent |
| `edit: …` | The edit is checked against the copy rules and posted again. ✅ on it sends it |
| `send: …` | Sends that text instead of the draft |
| `class: …` (for example `class: referral`, `class: not now`) | Records the class the reply should have had. Nothing is sent and the card stays open; every report counts the reply as you said. Once 10 corrections exist, the first readout of each month shows how often the model's class stood |

A card that says *Instantly thinks this reply is interested; our model does not* is one where Instantly's own read
of the email disagrees with ours: read it yourself. An away message Instantly reads as interested gets a card too
(with no draft).

## Mondays: the readout

At 08:30 UK, after the hand-check's draw and before the daily post, `monday_readout` posts last week (Monday to
Sunday, UK time) to #us-outbound. `us-outbound report readout` prints the same text without posting. Read it top
to bottom:

Rebuilt on 20 Oct 2026 (W3-1): it prints what can be read now and no verdict ("below", "on track", "raise"); every
rate has its counts and a 95% range, a split shows only when each part has 15 companies, and a line at the end says
how many of the ranges would miss by chance alone. Read it top to bottom:

1. **The headline and last week:** how many decisions wait for you; emails sent, the companies that replied
   (people, not out-of-office; each company once, with the reply emails beside when they differ) and how many
   positively, meetings booked, bounces and unsubscribes.
2. **Operating metric** (until replies mature): companies contacted last week (of `weekly_enrol_cap`) × the share
   the hand-check judged right for us over four weeks × the share delivered, "≈ N ICP companies reached". The
   hand-check holds its sampled companies until judged, so this overstates by at most about 2 points.
3. **North star:** companies engaged (a positive or referral reply within 28 days of email 1, or a meeting within
   45), each company once, a bounced one kept in. Until 30 companies have had 45 days it gives counts and the reply
   curve (by day 7, 14, 21 and 28 since email 1); from 30, the rate with its range ("1 of 30 (3.3%, 95%
   0.4–14.5%)"). Meetings two ways: at any time, and not from an ad (the booker's HubSpot source is not paid search).
   From 30 companies past 28 days, the positive and the human reply rates. Exposure (how many got all four emails, a
   safety pause, the hand-check's hold). "Reply capture unverified" on the block until the week's capture check
   passes. The stop rule's count.
4. **Throughput and supply:** the week's sourcing plan, what held each send day and the leads lost by cause,
   mailbox use, and the contactable supply in send days with the yields it rests on.
5. **Hand-check, ICP precision and industry labels:** the week's hand-check; the random rows judged fine over the
   last four weeks with a 95% range (`Fine: 38 of 48 (79%, 95% 66%–88%)`), the reasons for leaving out, the same by
   gate version (the labels' and ICP rules' versions the companies passed); a split (group, size band, state, slice,
   score decile, arm, label source) only when every cell has 15 rows, else "pooled only". The 0.90 target takes
   about 150 rows to show. **Industry labels:** the corrections made by hand, how often the rules and the model
   agreed, the label sample's exact and group figures with ranges, the emails sent a label's own copy apart
   (October: 4 of 30 pitched the wrong label), the rater's agreement with himself, and the label policy and whether
   it is applied (General `copy_level_policy`). Many of one correction means a definition or keywords on the
   Industries tab need a change; run `us-outbound labels eval --live` before and after.
6. **Delivery and capture:** step-1 bounces per contact with its range (contacts whose first email bounced, of those
   sent it), each email source's bounces per contact beside the per-send figure its safety pause uses, each
   mailbox's last 100 sends, the reply-capture check, complaints "not measured", and whether opt-outs, replies and
   safety-check refusals were all dealt with (each with what to do when not).
7. **Splits:** the share of last week's companies drawn at random (the explore arm); human replies within 28 days
   by explore arm, industry group and size band, each only when every part has 15 matured companies; the A/A check
   (the sham arm, a label that changes nothing: a difference outside its range is a counting problem, not an
   effect). Then last week's activity by arm, angle, group, sender and step, as counts.
8. **Tests** and **Signals:** a test that reached a pre-registered look (a decision for you), or how far each has
   got; each signal's companies with and without it, with ranges, as monitoring.
9. **Forecasts:** last week's forecasts (companies contacted, ICP precision, positive replies from the companies
   reaching 28 days) against what they came to; one outside its range three weeks running is flagged for a look at
   its definition.
10. **Cohorts and changes:** the last four enrolment weeks at the latest age reached, and what changed last week in
    the settings, copy, campaign constants and code (`us-outbound report cohorts`, `us-outbound cohorts changes`).
11. **Decisions waiting**, last, each with where to act. Before the readout, Monday's credit and spend asks and the
    mailboxes to add (there is no Monday daily post).

The numbers behind it are kept daily by `metrics_daily` (08:50 UK, from 20 Oct 2026) in the database's `metrics`
table, one row per day, cut and measure, so a later look can read a series rather than old posts.

**The experiment registry** (in *Tests*, from 21 Oct 2026). General `live_split` says which random split new
contacts are drawn into: `both` (the default) keeps today's three, the opener holdout, the email 1 subject split and
the running test on the Tests tab; `opener`, `subject` or `test` keeps only that one; `none` draws none. Contacts
already enrolled keep theirs. Each split's line gives what it is decided on, the test and its looks, how many
companies each side needs (589 a side to tell 3% from 6%), how many it has, and the date it should be readable at the
current pace. A test started from 21 Oct needs, on the Tests tab before `us-outbound test start`, `metric_id` (O4: any
human reply within 28 days; O3: a positive one), `definition_version` (the metrics' version, 2026-10-20), `test`
(`fisher_exact` or `beta_posterior`), `alpha` (like 0.10) and `look_thresholds` (`one_look`, or `obrien_fleming` with
looks), and runs alone, with `live_split` naming its split. `warm-intro-70` runs on as it started.

**The early warning** (from 21 Oct 2026; `metrics_daily`). One post a week, only when it fires: no human reply from
100 companies that have had 28 days (P = 0.048 if the true rate were 3%), or a meeting or positive-reply rate very
likely under half of what the pilot expects. It says to check reply capture and deliverability before the copy. It
pauses nothing unless General `early_warning_pause` = yes; the stop rule (5 meetings in the first 1,500 companies)
stays the brake.

**The segment value** (the 1st, 02:20 UK, `segment_prior_monthly`). Spill's own decided HubSpot deals per industry
group and size band, read only, are the starting point for which groups convert; the lookalike fit's industry part
uses them (Legal Teams above Technology & Startups, Construction below). Focus-share or switch-off rows for the
Suggestions tab come only once a group has 300 companies past 28 days, so not for months; the readout says how many
each Focus group has.

**When a sync widens who we email** (more industries active, a wider size range on General), one post says so: add a
dated line to `docs/decisions.md` with the reason.

**Small numbers read as small.** A rate is given only from 30 companies whose window has closed; before that, counts.
A split shows only when every part has 15 companies ("pooled only" otherwise). Every rate carries its counts and a
95% range, and about 1 in 20 ranges misses the true rate by chance, which the readout's last line counts for you.

**Meetings** count bookings on your HubSpot calendar (the signature's "Book a call here", and the website's
demo page, which books into the same calendar) and Spill 3.0 deals at "Demo requested" or later at a company
we emailed. `hubspot_readback` reads them every hour and stops that company's emails; it only reads
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

- **Who gets which.** A company's version comes from a hash of the company, so it never changes: an
  industry correction gets the same one. It is
  independent of the opener holdout and the personal-subject split, so those comparisons still hold.
- **Where the change cannot be made** (a `replace` whose words are not in that company's Copy row; a
  `subject` test on email 1 for a company with the personal subject; or a change that would break a copy
  rule in that email, such as a line made too long by a long company name), the company is left out of the
  test whichever version it hashed to, and gets the Copy row's email as it is. The enrol summary counts the
  reasons. `us-outbound test start`
  says how many Copy rows the change fits. The warm intro fits every email 1.
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

### Before you are up (weekdays, no message unless something fails)

| UK time | Job | What it does |
| :- | :- | :- |
| 03:00 | `source_universe` | Finds new companies in Apollo until there are two weeks of `weekly_enrol_cap` in companies that can be emailed ("contactable supply"), never more than 2 × the cap in a run or 3 × in a week. Where it looks follows the week's plan (pages per state: at least one a state, at most 40% to one; W3-3), printed in the Monday readout |
| 03:45 | `read_pages` | Free reads: careers pages |
| 04:30 | `verify_accounts` | The free checks and the industry label check |
| 05:00, 05:10 | `apollo_enrich`, `apollo_signals` | Paid reads, for verified companies only (since 19 Oct): an exact headcount (one outside the size range is left out for good) and funding; open job postings |
| 05:15 | `contact_precheck` | Free reads (W3-3, in place of `apollo_people` at 04:20): who Apollo has to email at each verified company waiting for a contact, and its People leaders. A company with nobody is set aside for 30 days |
| 05:30 | `pick_contacts` | One rescore, then a contact for the companies the next two send days need, the ones with the most people to try first |

To see why sourcing stopped: the `stopped_by` of the run's `source_universe_done` log line (and its heartbeat's
detail, with the counts under `supply`). It names the
contactable supply against the target and whether each yield is *measured*, the *default* (fewer than 20
observations) or *floored* at 0.20. On the floor, sourcing asks for a lot of raw companies per contact: the
ceilings keep that to 300 a run and 450 a week at a cap of 150.

## Expected volume in the pilot

Each sender takes new contacts at a quarter of its daily cap, so the follow-ups on days 7, 14 and
21 always fit. In week 1 the ramp holds each mailbox to 10 sends a day, which is about 3 new
contacts a day per mailbox. Expect about **6 new contacts a day** with Hannah's and Sam's mailboxes, and
about **11** once Harry's two are warm. Volume grows as the ramp rises to 20 sends a day (about 5
new contacts a mailbox) and then 30 (about 8). In the pilot, `weekly_enrol_cap` (150) is well
above this.

## What Slack will ask you to do

Since 7 Oct 2026 the system asks in #us-outbound, with the approvers mentioned, whenever it needs a
decision or a purchase. Each line is posted once (a day, a week or a month, as below), so a line that
comes back is new. Since 12 Oct 2026 every message has the same shape (`us_outbound/base/say.py`): an icon
and a plain headline, the facts, and a "What to do" line with a link to the sheet tab or the command to
paste, so the table below is a reference; the message itself says what to do. A test-run post starts
"Test run: this would have gone to #us-outbound. Nothing was sent to prospects."

| Ask | When | What to do |
| :- | :- | :- |
| "Credits and settings: … Apollo runs out in about D days…" (or "Apollo is below our reserve…") | 09:00 Tue–Sat with the daily post, Monday 08:30 with the readout; once a day, when the floor is under 21 days away at the last fortnight's pace, or Apollo is already under it (then sourcing and email reveals have stopped) | Buy Apollo credits, or lower `apollo_monthly_credits` on the General tab (below the floor: buy, or lower `apollo_floor`) |
| "Apollo: … credits used this month (80%)", or "… are used" | 09:00, at 80% and at 100%, once a month each | At 100% sourcing and email reveals stop until the 1st. Raise `apollo_monthly_credits` on the General tab to allow more |
| "Claude: $X of the $50 monthly cap…" | 09:00, at 50%, 80% and 100%, once a month each (the month is UTC, as Anthropic counts it) | At the cap, replies come to you as "other" with no draft, copy QA stops, and new companies wait unverified for the industry label check (those already verified carry on). Raise `claude_monthly_cap_usd` on the General tab (up to $100) and the spend limit in the Anthropic Console |
| "Apollo's website-visitor credits are running low…" | 09:00, once a month, under 15% left, while site visits are on | Buy more in Apollo, or the site-visit signals stop |
| "Add N mailboxes now: a new mailbox takes about 3 weeks to warm up…" | Monday 08:30 (with the readout), once a week, when the Active mailboxes at full ramp take fewer new companies a week than `weekly_enrol_cap`, and enough companies are ready or coming to fill more | Buy the mailboxes, then `us-outbound mailbox add ADDRESS --owner "NAME" --live` for each. The same-day "Add a mailbox for …" line in the daily post stays |
| "labels: N industry corrections since the last send day…" | 09:00, once a day, when 3 or more companies' industry was corrected by hand (the Hand-check tab, `labels set`) | The line names the moves (`Adtech & martech → Fintech (3)`): fix that label's `definition` or `apollo_keywords` on the Industries tab, `us-outbound labels eval --live`, then `us-outbound sync`: a changed definition changes the label check's version, and verify_accounts asks it again about the companies in the queue, about 150 each weekday morning |
| "labels: the model put N of the M companies the rules labelled (P%) in another group or outside our labels…" | 09:00, once a day, when the model agreed on the group for under 70% of at least 20 companies the rules labelled (each counted once; the model naming a label within the rules' group, such as Publishers for Marketing & Creative Agencies, agrees) | The rules' NAICS codes or keywords for the group it names bring in the wrong companies: trim them on the Industries tab |
| "labels: the rules gave no label to N of M companies checked…" | 09:00, once a day, when at least 20 companies and 30% of those checked had no rules label | The line names the model's commonest labels for them and their commonest NAICS codes (`541511 → Software & SaaS (9)`): add each code to that label's `naics_prefixes` on the Industries tab, then `us-outbound sync`: verify_accounts gives the queue the new rules' labels from the next weekday morning |
| "The label check did not run (…); N companies wait unverified" | 09:00, once a day, while the model cannot be asked (the Claude cap, an error) | At the cap, raise `claude_monthly_cap_usd`; on an error, it usually clears itself next run. Verified companies carry on meanwhile. To run on the rules alone, set `label_check` to `skip` |
| "Instantly's plan is full: nobody new is being emailed" | When an add finds the plan full, once a day | Upgrade the Instantly plan, or delete leads that finished their sequence. Nobody is suppressed: the contacts wait, and the next enrol adds them once there is room |
| "Instantly's plan has room for N more leads, under 2 weeks…" | After an add, once a week | The same, before it fills |
| "Replies that said “not now”: today is the day to follow up" | 08:45 UK, daily (`follow_up`, from W2-4, 20 Oct 2026), for each not-now reply whose date has come: the date they gave, or 90 days after the reply | Write to them again from the mailbox they wrote to. A HubSpot task due today is made for you too (it names the reply item; a warm reply's company and contact are linked). Nobody who has asked to stop since is listed |
| "Some automatic jobs ran into problems" | Hourly, once a day per job and kind | Usually nothing: each job tries again on its next run. If it keeps coming, `us-outbound status` and the worker's logs in Railway |
| "HubSpot turned down our key" (or Apollo, Instantly, Slack, Google, Anthropic), inside "Some automatic jobs ran into problems" | Hourly, once a day per key | If the key was revoked: make a new one, replace the variable in the us-outbound service's **Variables**, then **Deploy**. If the key is current, check what the plan allows (Apollo answers 403 for a search its plan does not include) |
| "The jobs have stopped… The settings sheet has errors on the … tab" (also "N automatic jobs have stopped running") | Hourly, once a day | Fix the sheet (the message shows the first errors and links the tab), then `us-outbound sync`. The worker also reads the sheet each time it starts |

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
- **The daily post says a campaign is not active:** that sender gets no new leads until
  `us-outbound start --live` activates it. The 07:00 mailbox check activates a newly created
  campaign by itself once sending has gone live. A campaign Instantly shows as *completed* is fine:
  Instantly marks an active campaign completed whenever it has no lead left to email (straight after
  `start`, if it has none yet), and the next lead added resumes it.
- **A company's industry is wrong** (a fulfilment firm pitched as a games studio): pick *wrong industry*
  with the right one in `right_label` on the Hand-check tab, or `us-outbound labels set DOMAIN "<label>"
  --live`. The label is set and kept on the Overrides tab, and the company's next emails are that label's;
  contacts already in flight keep theirs. `us-outbound labels show DOMAIN` gives its label's history. Since
  7 Oct the task model checks each company's label before it is verified (the label check, docs/pipeline.md
  stage 3), so this should be rare: the full daily post's **Labels** line (`us-outbound report daily --full`)
  counts the corrections, and an ask names them when there are several. Government domains (.gov, .mil) are never
  prospected.
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
pages (`/us`) and its pricing and demo pages (`/us/pricing`, `/us/demo`, `/us/book`; constants in
`settings/model.py` since W2-4)
in the last 30 days. A visit lifts that company's score before the 12:00 enrol ("Visited the US site"
+35, "Viewed US pricing or demo page" +25, so both make Priority). A new visitor is looked up in
Apollo: one in the US (in any state: a visitor is never excluded on its state) within the General size
range joins the queue, and one Apollo can't place (no HQ state or country,
no size, no industry) is left out for incomplete information (Harry, 14 Oct 2026). It costs about 3 Apollo credits a day, plus 1 for each new visitor looked up. The daily
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
- **Volume:** `weekly_enrol_cap` on the General tab is yours to change as the ramp rises (docs/roadmap.md §3); the
  readout shows what held each send day, so you can see whether the cap is what binds.
- **Email 1's subject split** (5 Oct 2026): half the companies (General `email1_subject_share`, 0.5)
  get the personal subject `email1_subject` ("support for the {{company}} team"; `{{company}}` and
  `{{first_name}}` only) in email 1 instead of the Copy row's. Emails 2 to 4 are unchanged. Each
  company keeps its arm, and `us-outbound report signals` says which subject gets more replies once
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
  more emails from the next morning (7 Oct 2026). `us-outbound report lookalikes`
  lists the customers by industry and size. To put the graded rows on the sheet, run
  `us-outbound settings load --tab Signals --live`: it adds "Close match" and "Some match to Spill's
  customers" and switches "Looks like Spill's customers" off.
- **Parked since 19 Oct 2026, so it does not run on the 1st: lookalike leads** (`lookalike_leads`). Its
  search paid for whole-US pages and kept only the active states; it comes back when it searches the
  active states alone. When it ran, on the 1st at 02:50 UK, Apollo looked for US companies like
  Spill's active customers, UK customers included, in the Focus tab's industries and the same size
  band, and the new ones join the queue. They still go through the usual checks (HQ state, size,
  HubSpot; left out if a fact is missing), and they score the "Found as a lookalike of a
  customer" signal (+10). Nothing arrives in Slack on its own: that morning's daily post says what
  it found under **Sources**, with how the UK customers' searches did beside the US customers'.
  It spends at most 60 Apollo credits. `us-outbound run lookalike_leads` runs it by hand; a second
  run in the same month does nothing, since the customers are much the same.
- **The 1st, 09:15 UK: the label sample** (`label_sample_monthly`). 25 companies we email or will
  email, not judged before, are added to the Label sample tab, and a Slack post links to the tab
  with last month's score (label right, group right, emails with an untrue pitch) and the score over
  every batch. Type the right label in `your_verdict` for each. Judged rows are never redrawn: they
  are the label check's answer key (`us-outbound labels eval --from-sample`). The same run drafts
  definition changes for the labels the check confused (the writing model), scores the answer key
  with and without them, and puts them on the Definition suggestions tab, linked from the post. Set
  `decision` to approve or reject; the next settings sync (02:00, or 11:30 on weekdays) writes each
  approved line into its label's `definition` on the Industries tab and dates `applied`. A changed
  definition sends every company back through the label check, about $0.01 each.
  Since 20 Oct 2026 (W3-2) two of the batch's rows are companies you judged before, unmarked, so the post
  can say how often your second verdict matches the first; each company is still scored once. The run
  also stores the label policy (above) and drafts ICP rules from the hand-check: each leave-out reason
  given 3 or more times in four weeks (media or publisher, IT services or outsourcing, subsidiary, not an
  operating business) becomes a row on the **Suggestions** tab with its evidence (`catches 3 of 3
  leave-outs; excludes 0 of 38 fine rows; answer key 50 → 51 of 63 right`). A rule that would leave out a
  company you judged fine, or does worse on the answer key, is written `rejected by eval`. Set `status` to
  `approved` (or `rejected`); only an approved row is applied, at the next settings sync: a "not X" line
  added to the labels' definitions, and for IT services the NAICS code added to the label's
  `exclude_naics`. An `entity_text` row is a change to the label check's prompt that engineering makes.

## Where the data is

The companies and contacts live in the Postgres database on Railway, schema `us_outbound`: the
`accounts` table (one row per company, by its domain) and `contacts` (one row per person). Beside
them are `signal_events` (what the sources found), `events` (sends, replies, bounces, opt-outs),
`hitl_items` (the Slack cards) and `suppression`. The database has no public access, so look at it
with `us-outbound accounts`: a summary, then the companies in queue order (`--status`, `--tier` and
`--industry` narrow the list). `us-outbound accounts acme.com` shows one company in full, with its
contacts' emails. For a spreadsheet, run `railway ssh -- us-outbound accounts --csv > companies.csv`
on your own computer; it holds names and emails, so keep it private and delete it when you are done.

**People leaders** (5 Oct 2026; from W3-3, 20 Oct 2026, in `contact_precheck`): each weekday at 05:15, before
`pick_contacts` rescores the queue, Apollo's free people search looks up who leads People at each verified company
waiting for a contact, so "New People leader" and "People leader in place" count before anyone is contacted. "First
People hire (likely)" counts only where Apollo knows at least half the company's staff, so that finding no People
leader there means something.

**Once after W3-3 reaches the worker** (20 Oct 2026): `us-outbound source migrate-state` prints the places in this
month's Apollo searches and the judged website visitors it would copy out of the credit ledger's notes; then
`us-outbound source migrate-state --live` copies them (`source_universe` makes the same copy on its first run if this
has not run, so nothing restarts at page 1). Then `us-outbound accounts dedupe` lists the domains more than one
company holds; `us-outbound accounts dedupe --live` merges each into one (never two companies that both have a
contact being emailed: those are listed for you), and once none is left it makes the database hold one company per
domain.

**Suggestions** (W3-3): the Monday readout adds rows to the Suggestions tab of the settings sheet: a title
`pick_contacts` left out at 10 or more companies of one size (`roles_title`: add it to a Roles-tab row?), and an
email domain revealed twice for one company (`domain_alias`: take it as the company's own?). Nothing changes until
you approve a row.

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
| `status` | `live_sending`, when the settings were synced, what waits for you, the jobs that need a look, what retention last deleted, mailboxes, today's number, and the go-live checks: Copy, Campaigns, Watchdog |
| `report daily`, `report daily --full` | The daily post as it would read now, short or in full; it posts nothing |
| `accounts`, `accounts DOMAIN`, `accounts --csv` | The companies and contacts we hold: a summary and the list, one company in full, or a spreadsheet. Read-only |
| `sync` | Brings sheet edits into force now |
| `start --live` | Syncs, then resumes the campaigns and enrollment |
| `stop --live` | The brake |
| `seed send ADDRESS --owner NAME --live`, `seed check` | The seed-inbox test of the unsubscribe link (`--subject personal`: email 1 with the personal subject) |
| `labels set DOMAIN "Fintech" --live`, `labels show DOMAIN` | Set a company's industry label (as *wrong industry* on the Hand-check tab does), or show its label and its check history |
| (the `label_sample_monthly` job, the 1st at 09:15) | Labels judged by you, not by the label check: 50 companies drawn at random from those emailed or to be emailed, added to the Label sample tab as this month's batch, under the rows already there, with each one's site, what it does, our label, the copy it gets and how the label was decided. Fill `your_verdict` with the right label (any case), or `right`, `right group`, `wrong group` or `outside` (and `right_label` when ours is wrong); the next month's run scores the batch: the share right with a 95% interval, how many emails carried a pitch that is not true (by the copy each was sent), and, where the right label is known, how often our label, the rules and the model each had it (for `outside`, the model's or the rules' "no label" is right). Judged rows are never redrawn. The monthly run asks the models for its definition drafts and its answer-key checks (above); `labels sample` alone asks none |
| `labels eval --live` | Scores the model on the first cards' 13 companies (about $0.15; `--from-corrections` adds the companies corrected by hand, `--from-sample` the companies judged on the Label sample tab, about $0.01 each). Run it before changing a definition, keywords or the prompt; it exits 1 below 90% acceptable or on any unsafe answer. Since 20 Oct the sample's `outside` rows are in the answer key as companies to leave out, so a label with its own copy on one of them counts as unsafe: `--from-sample` can exit 1 where it passed before, and that row is the one to look at |
| `replies list`, `replies send ID --live` (`--text "…"` sends your text), `replies skip ID --live` | Reply cards without Slack |
| `killrules show`, `killrules clear ID --live` | Kill-rule holds |
| `mailbox check --live --fix` | Mailbox health now, each sender name set to its owner's full name, and the campaigns put right |
| `copy preview --industry "Fintech" --html fintech.html`, `copy qa --live` | An email as a prospect will see it; QA for edited rows |
| `report readout` | The Monday readout for last week, printed and not posted |
| `report cohorts`, `cohorts changes`, `cohorts in-flight` | Each enrolment week at 7, 14, 21 and 28 days after email 1 (`--cut`, `--age`, `--weeks`); what changed between the last two config versions (or `cohorts changes A B`); each campaign's leads with a step still to send |
| `campaigns ensure --fix --in-flight --live` | Applies campaign drift in the steps, delays or text_only to the leads already in flight too (held otherwise; docs/developing-while-live.md) |
| `report signals` | Each signal's verdict, with the tiers and email 1's subject (the Monday readout carries the signal table's headline) |
| `test start ID --live`, `test read ID` | Start a test on the Tests tab (an `ab`, `variant` or `holdout` test; see **Copy tests**); read it at its latest pre-registered look |

Also `handcheck show|approve`, `erase --email` (and for an Apollo deletion notice), `run retention`, `report pages`,
`report lookalikes`, `report capture`, `unenrol --month YYYY-MM` and `schedule`. Folded in W2-4 (20 Oct 2026): `daily`,
`readout`, `signals review` and `lookalikes show` are `report ...`; `rescore` is `run score`; `settings sync` is `sync`;
`relabel`, `labels audit`, `labels sample`, `signals value` and `lookalikes fit` were removed.
`us-outbound --help` lists every command.
