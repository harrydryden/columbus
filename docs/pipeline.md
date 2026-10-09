# How an account is found, qualified, cleaned and enriched

Apollo, Clay and Instantly can each find companies, find emails and enrich records, and Apollo, Clay, Instantly and HubSpot each have their own idea of an ICP. This page covers:
- which tool does which job, and where the ICP lives;
- the order in which an account is found, checked and paid for;
- the credit budgets and the weekly target, and how the system shows which one is holding it back;
- how many emails Instantly can send, and what it reports back;
- how Harry can steer the system from the sheet.

Harry's decisions on 30 Sep 2026:
- He approved changes 1–8.
- Credit budgets are monthly, like Apollo's and Clay's own: 2,000 each, about 500 a week.
- The enrolment target is weekly: 150, Monday to Sunday, UK time.
- Steps go a week apart.
- The sheet gets an industry focus and named companies.
- Lookalikes wait. (Superseded 1 Oct: lookalikes from Spill's HubSpot customers; see "How Harry can steer the system".)

Harry's decision on 2 Oct 2026: Clay's page reading moves into our own code (`read_pages`, no Clay
credits), and Clay is narrowed to what only it does: the email waterfall for contacts Apollo can't
verify, and a cross-check when Apollo's HQ state or size looks doubtful (both built on 6 Oct, each
behind its own General switch, off until Clay is confirmed: "Clay's two jobs" below). `clay_verification`
stays `skip` for the pilot. The reader measures its own coverage, so whether to enhance it is decided
on numbers after the first batches (docs/roadmap.md).

Harry's decision on 2 Oct 2026: funding is a sign that a company is in a period of change and growth.
Apollo's organisation search rows carry no funding fields and no employee count (the first live run),
so the funding comes from Apollo's organisation enrich (`apollo_enrich`, 1 credit per company found), for
the industry groups named in the General `apollo_enrich_groups` (Technology & Startups, where funding is
common), within 15% of the month's Apollo credits.

The list of changes is at the end.

## The short answer

- **The sheet is the only ICP.** No persona, saved search, list, score or ICP profile is set up inside any vendor. Every call builds its filters from the sheet when it runs.
- **Each job has one owner.** A second vendor is only ever a fallback, called when the first one misses.
- **Fit finds accounts, and signals rank them.** The main driver is an Apollo organisation search built from the sheet's Industries, States and size bands. Signals never add a company to the universe, with one exception: a company that visits the US site.
- **Free checks come before paid ones.** Clay, the scarcest budget, is spent only on accounts that have passed every free check. An Apollo email reveal is spent only on the contact about to be emailed.
- **Budgets are monthly and paced by the day.** Each job that spends credits checks the month's balance and today's share of it before every batch. Enrolment spends none.
- **Postgres is the only record.** Vendors return observations. Python stores each one as a fact with its source and date, and makes every decision.

## One owner per job

| Job | Owner | Fallback | Not used |
| :- | :- | :- | :- |
| Find companies (the universe) | Apollo organisation search | IRS BMF for nonprofits (January). New site visitors. The Named accounts tab. Great Place To Work and B Corp lists (phase 3) | Clay Find Companies, Instantly SuperSearch, Apollo lists and saved searches |
| Company facts (size, HQ, NAICS, founded) | Apollo (an exact employee count from organisation enrich for `apollo_enrich_groups`; the searched size band otherwise) | Doubtful Apollo facts (no HQ state, no size, a count near the 10, 50 or 250 edge) go to the weekly hand-check instead of being verified unseen. With `clay_cross_check` = yes, Clay is asked first (`verify.cross_check`; "Clay's two jobs" below) | Clay's other data providers; HubSpot auto-enrichment |
| Clean company name | Clay AI, checked against the `clean/names.py` rules | The `clean/names.py` rules alone | |
| Read careers, benefits and values pages | Our own reader, `read_pages` (source `careers_pages`; no credits; Harry, 2 Oct 2026) | Clay "US Outbound – Accounts" (`clay_careers`) if the reader's coverage falls short. A failed read is scored as "not read" | |
| Funding | Apollo organisation enrich (`apollo_enrich`, for `apollo_enrich_groups`; Harry, 2 Oct 2026). Search rows carry none | Clay's Company Latest Funding, run only when Apollo has nothing | |
| Hiring counts | Apollo organisation search with job filters (`apollo_jobs`) | | |
| Job-posting text | The public Greenhouse, Lever, Ashby and Workable feeds (free), read by `read_pages` from the board the company's site links to | | |
| People at the company (titles, locations) | Apollo people search (free) | | Clay Find People, Instantly SuperSearch |
| Work email | Apollo bulk match, verified emails only | Clay's Work Email waterfall, for misses and catch-alls only, valid results only, behind `clay_email_fallback` (off until `us-outbound clay check-email --live` confirms one lookup). "US Outbound – Contacts" replaces it once built | Apollo's own waterfall (its results arrive only by webhook, and there is no public endpoint); Instantly's lead finder |
| Email verification | Whoever found the email: Apollo's status, or Clay's check | | Instantly verification, which would spend Instantly credits |
| Site visits | Apollo website visitors | Harry's weekly CSV export | Instantly's website-visitor feature |
| Score, tier and angle | Python and the Signals tab | | Apollo scoring, Clay scoring, HubSpot lead scoring and ICP tiers |
| Sending and warmup | Instantly | | Instantly Unibox, AI reply agent, CRM, lead finder and verification |
| Warm leads | HubSpot (the jobs write, only after a positive reply) | | The Apollo–HubSpot sync; Clay writing to HubSpot |

Instantly finds, enriches and verifies as well (SuperSearch, a seven-provider waterfall, and verification at 0.25 credits a lead). This system uses none of that. It would mean paying for a third email source (SPEC 1.1), and a third place where the facts about an account could live.

## Where the ICP lives

| Question | Sheet tab | How the vendors see it |
| :- | :- | :- |
| Which industries? | Industries: labels, NAICS prefixes, excluded NAICS, Apollo keywords | Built into the filters of each Apollo search. Python then re-checks every result against the tab, so a loose Apollo filter can't let a wrong company in |
| Where? | States (HQ state), and CA and WA never | Apollo `organization_locations`. People are checked against the same list |
| How big? | General `min_employees` to `max_employees` (10–249 by default; Harry, 6 Oct 2026: "update that to say 5-500 easily"), searched by size band, each band clipped to the range | Apollo `organization_num_employees_ranges` |
| Who do we email? | Roles: titles, the copy each row gets, and its rank at 10–49 and at 50–249 staff | Apollo people-search title filters; Python ranks the results |
| What makes an account better or worse? | Signals, Angles | Never sent to a vendor. Clay reports what the pages say and never judges them (SPEC 8) |
| Who is never contacted? | Hard exclusions (code), Overrides, suppression | Checked in Python before any credit is spent |

The vendors' own ICP features all stay off:
- Apollo: personas, the Context Center profile, scoring and lists. Apollo is read only in any case.
- Clay: Audiences and Signals. Clay Signals is a phase 4 source.
- Instantly: SuperSearch filters.
- HubSpot: the "Ideal customer profile tier" property and lead scoring. The jobs write only `us_outbound_tier`.

## The funnel

Each stage lists what it spends and the status it leaves the account in. A stage starts only on accounts the previous stage passed.

**1. Find: `source_universe` (weekly, Proposed 9).**
- Apollo searches the slices of active industry × active state × size band. Cost: 1 Apollo credit per 100 companies.
- **Proposed 9:** each week covers a quarter of the slices, so every slice is refreshed every four weeks and the month's Apollo budget is spent evenly instead of in one large bill on the 1st.
- The same front door (`accounts.admit`) serves every other way in: new site visitors, IRS BMF matches, the Named accounts tab, the monthly lookalike leads (below), and later the GPTW and B Corp lists. Each is matched to an Apollo record by domain.
- **Lookalike leads: `lookalike_leads` (monthly, the 1st at 02:50 UK; Harry, 5 Oct 2026).** Apollo's organization search ranks US companies by likeness to up to 5 of Spill's active customers at a time (UK customers included, US ones first), one industry group and size band at a time, with the universe's filters (the US, never CA or WA, the size band, no insurers). Each row must have a US HQ in an active state before it reaches the door, and verify then checks it like any account. New accounts have source `lookalike` and the "Found as a lookalike of a customer" signal. Cost: at most 60 Apollo credits a run, about 1 for the seeds' ids and 1 or 2 per group and band.
- At the door, each company gets its root domain, its name is cleaned by rule, and its industry label and group are set from the Industries tab.
- It is deduplicated on root domain against accounts and aliases, against suppression, and against the whole HubSpot portal.
- It is rejected at once for:
  - no domain
  - HQ in CA, WA or an inactive state (not a website visitor: its HQ may be in any US state, Harry, 6 Oct 2026; a contact located in CA or WA is still never emailed)
  - partner NAICS or keywords
  - HubSpot conflicts
- **Status: `new`.**

**2. Free signals: daily and weekly.**
- **apollo_people** (free; weekdays 04:20, before verify_accounts' rescore; Harry, 5 Oct 2026): for
  every queue account with a known size, Apollo's people search (0 credits, no emails) at the
  company for the Roles-tab People-leader titles at its size, in the US, with no email filter, and
  once more for how many people Apollo holds there. It writes `people_leader_count`, the newest
  leader's `people_leader_days_in_title` and `people_leader_newest`, `people_found` and
  `people_search_coverage` (people found over employees: the employees count, else the top of the
  size band). A count of 0 is written only at coverage 0.5 or more, so "First People hire (likely)"
  fires only where finding nobody is meaningful. Search rows carry no start dates, so the days in
  title come from the search's time-in-title filter, narrowed to within a week (up to 6 more
  searches, for accounts with a leader in post less than 180 days). Never-searched accounts first,
  then each again after 30 days; at most 150 accounts, 300 requests and 9 minutes a run, a request
  every 1.5 seconds. `pick_contacts` still writes the People leaders its own search sees, but not
  over a full search less than 30 days old. US headcount by state and the CA/WA share are not built.
- **apollo_jobs**: open roles and open People roles.
- **read_pages** (free; weekdays 03:45, before verify_accounts' rescore): the company's own careers,
  jobs and benefits pages (up to six pages, robots.txt respected, no JavaScript), then the
  Greenhouse, Lever, Ashby or Workable board its site links to. The benefit sentences, with quote
  and URL, feed the EAP, mental-health, wellbeing-app, progressive-benefits and competitor signals.
  `us-outbound pages show` and the daily post give its coverage.
- **apollo_enrich** (weekdays 04:10, 1 Apollo credit per company found, 0 for one Apollo doesn't know;
  Harry, 2 Oct 2026): Apollo's organisation enrich, in bulk calls of up to 10 domains, for the queue
  accounts in the General `apollo_enrich_groups` (Technology & Startups), never-enriched first, then
  those enriched more than 180 days ago, in enrol's order. It writes the funding facts the funding
  signals read (`days_since_funding`, aged to today at scoring, `funding_stage`, `funding_amount_usd`),
  `employees` and `headcount_growth_12m`, and the description, technologies and keywords when none is
  stored. An exact count replaces the searched size band on the account, unless an Overrides row or
  Clay says otherwise; verify then treats a count outside the General size range as usual (a verified account that
  count puts outside goes back to `new`, so verify sees it again). A company Apollo has no
  record for is "not found", not "no funding", and is not asked again for 180 days. The daily post
  counts what it found.
- **Site visits** (`site_visits`, daily 06:00, about 3 Apollo credits a day plus 1 per new visitor looked up; Harry, 5 Oct 2026):
  Apollo's organisation search with its website-visitor filters, read only, for the General
  `site_visit_domain` (spill.chat): who visited the `site_visit_us_paths` (`/us`) in the last day
  and the last 30 days, and the `site_visit_intent_paths` (`/us/pricing`, `/us/demo`, `/us/book`)
  in the last 30 days. A visitor is matched to its account by root domain (aliases included) or
  Apollo id, and gets `us_visits_30d` and `pricing_or_demo_visits_30d` (1 while Apollo lists it, 0
  once it no longer does, written only on a change), and a `site_visit` event for each day it
  visited (the daily post's warm accounts, the readout's visits before and after the first email).
  A visitor we don't hold is kept out on its search row only when it is surely not for us (outside
  the US, no label fits, an insurer or broker; never its HQ state, Harry, 6 Oct 2026). Every other one is looked up in
  Apollo's organisation enrich (1 credit each found, at most 30 a run) and judged again: out only
  for a size clearly outside the General size range; otherwise it comes in by the front door with source
  `site_visit`. One Apollo leaves in doubt (no HQ state, no size, no industry, a count near an
  edge) comes in held for the weekly hand-check, not left out (Harry, 6 Oct 2026). The
  rescore follows, so a visit counts at the 12:00 enrol. Apollo refusing the filters, or a week
  with no visitor, puts one line in the daily post: check the tracker.
- **Layoffs.**
- **IRS BMF.**
- **Calendar.**

These give a provisional score. Free data can decide every hard exclusion:
- fewer than 5 US people
- more than 20% of US staff in CA or WA
- founded less than 2 years ago
- HubSpot activity

An account also needs at least one candidate contact: a person matching the Roles tab for its size, located in the US outside CA and WA. **Status: `queued`** once every free check passes, or `disqualified` with the reason.

**3. Verify in Clay: weekdays, `verify_in_clay`, within today's share of the month's Clay budget.**
- It takes `queued` accounts in order of provisional score, enough to keep the verified queue 10 working days deep. With an industry focus set, it takes each group's share. It stops when today's share of the month's Clay credits is spent.
- It runs Apollo organisation enrich (1 credit) first, only if the search record is missing a field Clay needs.
- It then runs the Clay Accounts function in batches of up to 100. The function returns the clean name, confirmed HQ and size, an industry label, the pages it read, benefits, mental-health provision, culture statements, and funding (only when Apollo had none).
- If Clay and Apollo disagree on HQ state or size band, the account goes to the hand-check.
- The careers pages and job boards are already read by `read_pages` (free). Clay's own page read
  is for later, and only if the reader's coverage falls short (docs/roadmap.md).
- Until Clay is built (`clay_verification` = `skip`), `verify_accounts` verifies on Apollo data and
  HubSpot, and sends an account whose HQ state, size or industry is in doubt to the weekly
  hand-check with the reason; Harry's approval clears it, and an Overrides row corrects a wrong
  fact. A missing fact is not cleared by approving: the account stays on the check until an
  Overrides row fills it in (Harry, 6 Oct 2026).
- With `clay_cross_check` = yes (default no; Harry, 6 Oct 2026), Clay is asked about the HQ state and
  headcount of those accounts first, once each ("Clay's two jobs" below). A doubt Clay settles is
  dropped; one it disagrees with reaches the hand-check with both values ("Clay says 62 staff,
  Apollo says 49"); a missing HQ state or size is filled in from Clay, so the account can verify the
  same day. Industry stays with the hand-check.
- **The industry label check** (Harry, 7 Oct 2026: "industry categorisation is critical to the efficacy of the
  system"; `us_outbound/labels.py`). Once an account passes the free checks, the task model (Sonnet, effort low,
  about $0.01 a company, once) reads its Apollo facts (name, domain, Apollo industry, NAICS codes, keywords,
  description) and, once `read_pages` has read it, its home page's title, description and first 600 characters
  (never the rules' label) and picks one label from the Industries list, with its confidence, what kind
  of body it is, a quote and a short "what they do". One rule combines it with the rules' label:
  - they agree: the label's own copy (`label_source` rules+model);
  - they disagree: the model's label when it is sure (model); otherwise, within one group, the group's own label and
    copy (umbrella); across groups, the rules' label with General copy, flagged on the card (disputed);
  - a public body is disqualified; a society or membership body, a company no label fits, or one whose industry is
    switched off is disqualified when the model is sure and held for the weekly hand-check ("industry uncertain: …")
    when it is not; an Overrides `industry` row or an approver's correction stands, and nothing is asked.
  - The verdict is kept (a `label_verdict` fact) and asked again when the label list, a definition, keywords or
    the prompt change (`labels_hash`), or once when the model was not sure and the company's home page has been read
    since (`labels.second_look`). The first audit (7 Oct 2026) held 204 of 409 companies, the model unsure on Apollo's
    facts alone, so `read_pages` first reads the home page (robots.txt and the home page only) of each queue company
    the model was unsure of whose page it has not read, at most 150 in 8 minutes; `labels audit --live` makes the same
    pass before it asks. A page that says nothing or refuses is recorded empty and not read again for the check.
    Each run asks at most 150 (12 minutes), Focus groups first, then queue order;
    accounts already verified with no fresh verdict are checked in the same share, so the queue converges in about
    three weekdays with no command run. A card rendered before its company's label was decided, which no longer fits
    it, is withdrawn by the next `poll_approvals`, and the next enrol proposes the company with the right copy.
  - `label_check` (General): `required` (the default) keeps a new account unverified while the model cannot be asked
    (the Claude cap, an error), and the daily post asks; accounts already verified keep going, an unchecked one with
    its group's copy. `skip` runs on the rules alone, with the group's copy.
- It rescores, which sets the final tier and angle. **Status: `verified`.**

**4. Contact: weekdays at 05:30, `pick_contacts`, just in time, within today's share of the month's Apollo budget.**
- It takes only the accounts the next two enrolment days will use at the weekly target's pace, in enrol's queue order, not the whole verified queue.
- It searches Apollo for free: people at the company with a Roles-tab title for its size, in the US, with a verified email.
- It ranks them (Harry, 1 Oct 2026: "the closer to seniority and decision maker the better"): the Roles-tab rank for the size, then seniority, then how well the title matches, then the newest in role. Junior titles come last, and never as a People leader.
- It reveals the top person's email with Apollo bulk match (1 credit). The email must be verified, at the company's own domain, not a personal domain, a shared inbox, suppressed or already a contact, and the person not in CA or WA. If it fails, it reveals the next person: at most two reveals an account.
- When nobody suitable is found, the account records why (a `contact_pick` fact), `us-outbound status` counts it, and it is tried again after 14 days. Nobody is paid for twice.
- With `clay_email_fallback` = yes (default no): when Apollo's reveal misses or gives an address it
  doesn't call verified (a catch-all), that person goes once to Clay's Work Email waterfall (or the
  "US Outbound – Contacts" function once its id is set), within today's share of the Clay budget and
  never while a kill rule pauses the clay source. Only a `valid` result is kept, with
  `email_source` = clay; `catch_all_valid` waits for change 8. Work Email gets its own input names
  (Full Name, Company Domain, Social Profile URL, Company Name). After three failed lookups the run
  stops asking Clay, and without the Clay key it does not start.
- While a kill rule pauses the apollo email source (its addresses bounced), the run reveals nothing and
  spends no credit, and says so in its heartbeat (7 Oct 2026). Clay alone is not used meanwhile: it is the
  fallback for Apollo's misses, not a source of its own. `us-outbound killrules clear ID --live` lifts it.

| Rank | 10–49 staff | 50–249 staff |
| :- | :- | :- |
| 1 | Founder or executive; a partner or principal at a law or professional-services firm | People leader (senior: CHRO, VP, Head or Director of People or HR) |
| 2 | People leader (senior) | Founder or executive; partner or principal |
| 3 | Operations (COO, Head or Director of Operations, Chief of Staff) | Operations |
| 4 | Office Manager or Firm Administrator, as a fallback | HR Manager, HR Generalist or People Operations Manager (People leader copy) |
| 5 | | Office Manager or Firm Administrator, as a fallback |
| Never | Finance; HR Manager | Finance |

A sheet made before 1 Oct 2026 has the Roles tab in SPEC 5's layout. It is still read, as the old order, until `us-outbound settings load --tab Roles --live` replaces it; settings_sync says so meanwhile.

**A second contact (multi-threading; Harry, 6 Oct 2026; `enrol/second.py`).** Behind the General switch
`second_contact` (no by default: sending capacity is the binding limit today):
- **Where:** enrolled accounts of `second_contact_min_employees` (50) or more staff, read as the Roles tab
  reads size (a 50–99 band is 50), still in a queue tier.
- **Who:** the best-ranked person, by the Roles tab's order for the size, whose copy role is not the
  first contact's: the founder after a People leader, a People leader after a founder. An HR manager writes
  as a People leader, so never follows one. `pick_contacts` reveals them through every check a first
  contact passes (verified email from Apollo or Clay's fallback, not in CA or WA, not suppressed, not a
  personal domain, shared mailbox or customer domain, nobody's contact yet; at most two reveals), against
  the same Apollo budget, and only with what the first contacts of new accounts leave of its lookahead.
- **When:** never before the first contact is enrolled, and only `second_contact_delay_days` (3) after
  `sync_outcomes` records the first contact's email 1 as sent, so the two first emails never arrive the same
  day. The account must still be enrolled, with nobody at it who replied, bounced, unsubscribed or
  complained, and no second contact yet. One second contact an account, never more.
- **Its `contact_pick` fact** has `slot` 2; the contact is written with `contact_slot` 2 when enrolled.

**5. Enrol: weekdays at 12:00, `enrol`.**
- It works out today's number (below), re-checks HubSpot, assigns the test version and renders the copy.
- With `auto_send` = no (the pilot; Harry, 2 Oct 2026), it adds no lead: it posts a send approval, a card with the whole sequence, to #us-outbound. Only an approver's ✅ (or `us-outbound approvals send ID --live`) adds the lead to its sender's campaign, after re-checking HubSpot, opt-outs and the pauses. A card not approved by the end of the next send day lapses, and the account goes back to the queue. Cards still waiting hold their sender's slots and count towards the week.
- With `auto_send` = yes, it adds the leads straight away, once the weekly hand-check is approved.
- In a live run, an owner whose campaign is not active in Instantly gets no capacity and no cards: `us-outbound start --live` activates it. **Status: `enrolled`** once the lead is added.
- **Second contacts** (with `second_contact` = yes) count in ready accounts, so in today's number, in the
  week (`weekly_enrol_cap` counts contacts: a second contact is one more) and in their sender's slots, but
  are walked after every first contact, so they take only what new accounts leave. They keep the account's
  sender (its campaign; a paused or full sender's second contacts wait), get their own role's Copy row
  (falling back as a first contact's does: the industry, its group, then General; skipped when none is
  approved, or when the row is the very one the first contact got) and their own opener, and share the
  account's test version, opener holdout and subject arm. Their card is headed "Send approval · second
  contact" and names the first contact and when their email 1 went. The industry focus shares out new
  accounts only, so a second contact is outside it.
- **Both sequences stop when anyone at the account replies, bounces, unsubscribes or complains**
  (`replies/account_stop.py`, run by `sync_outcomes` every 15 minutes). Instantly's own stop for the
  company covers most replies; the sweep covers bounces, unsubscribes, complaints and replies Instantly
  ties to no lead. The colleague's lead is paused once phase 0 confirms Instantly's lead pause
  (`LEAD_PAUSE_CONFIRMED`), and deleted from the campaign until then; each stop is a `lead_stopped` event.
  Suppression stays per address: an unsubscribe or bounce suppresses only that address, never the colleague.

### Why fit, not signals, finds accounts

A search built from signals ("funded in the last 18 months", "hiring a People role") would give a smaller, hotter list. But it would leave out the Control tier: the 15% of sends that go to accounts with no signal. Without the Control tier, the Monday readout can't show what any signal is worth. So the universe is every company that fits, and signals only order it.

## Budgets and targets

Credit budgets are monthly, a calendar month in UK time, because that's how Apollo and Clay count credits. The enrolment target is weekly, Monday to Sunday, UK time. The General tab holds:

| Key | Default | Spent or used by | Checked |
| :- | :- | :- | :- |
| `apollo_monthly_credits` | 2,000 (about 500 a week) | `source_universe` (search pages, at most 25%), `apollo_signals` (job postings, at most 25%), `apollo_enrich` (organisation enrich, at most 15%), `pick_contacts` (email reveals: the rest, at least 35%), `verify_in_clay` (enrich, once built), `site_visits` (3 a day for the searches plus 1 per new visitor looked up, at most 35 a run, about 150 to 200 a month, outside the daily pacing as it runs after `pick_contacts`) | Before every batch: the month's balance, and today's share of it (each source's share paced the same way) |
| `clay_monthly_credits` | 2,000 (about 500 a week; 0 means no Clay calls) | `verify_in_clay`, `pick_contacts` (Clay Contacts or Work Email, with `clay_email_fallback`), `verify_accounts` (the cross-check, with `clay_cross_check`), `clay check-email` (one lookup) | The same |
| `apollo_floor` | 5,000 | All Apollo spend | Stops Apollo spend if the account balance falls below it |
| `claude_monthly_cap_usd` | $10 | Reply classification and drafts | A UTC month, as the Anthropic Console counts it |
| `weekly_enrol_cap` | 150 | `enrol` | Each send day takes what's left of it ÷ the send days left in the week. A short Monday is made up by Friday |

**Today's share.** Each weekday may spend what was left of the month that morning ÷ the weekdays left, today included. The budget then lasts the whole month instead of going in the first week. A quiet day leaves more for the rest of the month. Unspent credits don't carry over.

**How the month is going** shows the same figures for each budget:
- used and left;
- the month's pace to date (the budget spread evenly over its weekdays, through today);
- whether spend is ahead of, on or behind that pace (more than 10% either side counts);
- where the month is heading at the pace so far;
- how much today may still spend.

For example:

```
Apollo: 1,007 of 2,000 credits used this month (50%); pace to date 1,818, so behind pace; heading for 1,108 by 31 Oct; up to 326 more today (3 weekdays left).
```

The budgets are not part of today's enrolment number. The enrol job spends no credits, so a budget that has run out never holds back accounts that are already verified and have an email. It limits the stages before enrolment, and when that leaves too few accounts ready, the limiter says so (below).

**Is 2,000 Apollo credits a month sustainable?**
- **Balance:** 30,128 credits were left on 29 Sep, and they reset on 21 Aug 2027.
- **Use until the reset:** about 10.7 months × 2,000 = about 21,400.
- **What that leaves:** about 8,700, which is about 3,700 above the floor.

The monthly 2,000 has to cover:
- the sweep (`source_universe`, up to 25%: 500);
- job postings (`apollo_signals`, up to 25%: 500);
- organisation enrichment for funding and size (`apollo_enrich`, up to 15%: 300, about 15 accounts a weekday);
- about 650 email reveals (`pick_contacts`, the rest: at least 35%, 700).

Phase 1 measures the sweep.

**Clay:** the month's verified accounts are about 2,000 ÷ credits per account (measured on the first 100 accounts). To keep 150 accounts a week ready, the budget needs roughly 650 × credits per account ÷ the share that passes Clay's checks each month.

**In the sheet:** keep `apollo_monthly_credits` and `clay_monthly_credits` and set both to 2000. Rename `daily_enrol_cap` to `weekly_enrol_cap` with 150. A row still named `daily_enrol_cap`, `apollo_weekly_credits` or `clay_weekly_credits` is rejected, with the new name.

## Where the limit shows

Today's number is the smallest of three terms:

| Term | What it is | What raises it |
| :- | :- | :- |
| **Weekly target** | What's left of `weekly_enrol_cap` ÷ the send days left this week | A higher `weekly_enrol_cap` |
| **Sending capacity** | Each sender's new leads today, after the follow-ups already due and Instantly's backlog (next section) | Another mailbox, or a higher daily cap once warm |
| **Ready accounts** | Verified accounts with a sendable email, and second contacts due today (with `second_contact` = yes) | Whatever is short behind it (below) |

When ready accounts is the limit, the explanation walks back through the stages and names the first one that is short:
1. verified accounts still waiting for an email, and whether Apollo's budget is used (for today, or until the 1st);
2. Clay has no monthly budget;
3. Clay's budget is used (for today, or until the 1st) while accounts wait;
4. accounts are waiting for Clay;
5. nothing is waiting for Clay, so the universe or the free checks are the limit.

When sending capacity is the limit, it names each sender that is full and why:
- Instantly says the campaign or its inboxes hit their daily limit;
- its inboxes sent 95% of their cap on the last send day;
- follow-ups already fill a day.

It then says to add a mailbox, and how many ready accounts are waiting for inbox space. For example:

```
Add a mailbox for Hannah Spalding: Instantly says the campaign reached its daily limit. `us-outbound mailbox add <address> --owner "Hannah Spalding" --live`, then it warms for 21 days.
92 ready accounts are waiting for inbox space.
```

The same picture appears in several places:
- **`us-outbound status`** has two blocks: "Credit budgets this month" (one pace line per budget) and "Enrolment this week" (today's number, what limits it, each sender, the focus, and what stands behind it).
- **The enrol job's summary** (`limited_by`, `limits`, `focus`), kept in its heartbeat row and in the Railway log.
- **The mailbox check's summary** each morning: limits it set, and campaigns Instantly says are held back.
- **`v_budgets`** in Postgres: each budget this month, with `expected_by_now`, `projected` and `pace`.
- **The daily post** (phase 3, Slack): the `limited_by` line, every morning. Until Slack is set up, it goes to the log.
- **The Monday readout** (built 6 Oct 2026, below) does not carry these lines yet: how many days each term was the limit, and each budget's pace (**Proposed 11**, with alerts when a budget runs ahead of pace, or when the same limit binds three send days running).

## How many emails Instantly can send, and what it reports back

Instantly decides the moment each email goes. The jobs decide how many new leads go in, set the limits Instantly works within, and read back what it did:

| Limit or report | How the jobs use it |
| :- | :- |
| Each mailbox's daily cap | Set on the Mailboxes tab (`daily_cap`, 30). The morning check reads Instantly's own limit on each inbox and sets it back to the sheet's cap when they differ (live; reported in dry-run). Caps are changed in the sheet, never in Instantly |
| The campaign's daily limit | Set to the sum of the owner's Active caps. The morning mailbox check puts the daily limit and the sending list right itself; other drift is reported for `us-outbound campaigns ensure --fix --live` |
| Sends per inbox per day | `GET /accounts/analytics/daily`, filtered to the registry inboxes, read each morning for the last 7 days. If yesterday's sends fall short of what the forecast had due, Instantly is behind, and the shortfall comes off today's room |
| Why a campaign isn't sending | `GET /campaigns/{id}/sending-status`, read each morning. "Daily limit reached" (the campaign's, or every inbox's) marks the sender as full |
| The send window | Mon–Fri 09:00–16:00 ET. No sends at weekends or on blackout dates: enrol skips them, and the hourly `blackout` job pauses the campaigns over them, so no follow-up goes out on one either (7 Oct 2026) |
| Step timing | Days 0, 7, 14 and 21. Instantly counts delays in calendar days and moves a step due at the weekend to Monday. A week apart, every step falls on the same weekday as the first, so none does |
| Warmup | Instantly. Warmup emails are separate from the campaign limit, and warmup stays on (SPEC 13) |

**The forecast** (`enrol/capacity.py`) works per sender, because each account keeps its sender for life:
- Every enrolled lead holds a slot on the days its later steps go out. Leads that replied, bounced or unsubscribed hold nothing.
- A new lead sends four emails, so a sender takes at most **capacity ÷ 4** new leads a day. That pace keeps a full day steady.
- It takes fewer when, on any of the four days a lead enrolled today would send, the follow-ups already due (plus any backlog) leave less room.
- So no email, old or new, ever has to wait for a full inbox.
- New accounts go to the sender with the largest share of today's pace left, so Harry's two mailboxes take twice Hannah's or Sam's share.

**Why the steps are a week apart.** With SPEC 10's days 0, 3, 8 and 15, most later steps of leads enrolled Wednesday to Friday fell at a weekend and piled onto Mondays. Simulating 12 weeks with the four inboxes (120 sends a day):

| Cadence | New accounts a week | Sequence length |
| :- | :- | :- |
| Days 0, 3, 8, 15 (SPEC 10) | about 61 | 15 days |
| **Days 0, 7, 14, 21 (now)** | **150**: every inbox full every day | 21 days |

The copy doesn't change, only the gaps. The readout counts a reply for a week after the last step: 28 days from step 1, where SPEC 12 had 21 days for its 15-day sequence. The cadence is set in one place (`clients/instantly.py`, `STEP_DAYS`), and the campaigns, the forecast and the reply window all read it.

**Not yet counted:** the plan's own caps (emails a month and uploaded contacts, shared with the EU campaigns in the same workspace). Phase 0 checks whether Instantly's API reports the workspace's plan usage. If it does, it becomes a fourth term; if not, a General key `instantly_monthly_emails` holds this system's share (**Proposed 14**).

## Where the copy lives

**The Copy tab holds one row per industry** (Harry, 30 Sep 2026). Each row is a four-email sequence: `s1_subject`, `s1_body` … `s4_body`, plus one line per contacted role (`people_leader_line`, `founder_line`, `operations_line`), and `status`, `approved_by`, `qa`, `qa_notes`, `sources` and `note`.
- **Which row a lead gets:** the most specific approved row that has passed QA. That is its own industry label for its contact's role, then the label, then the label's group for the role, then the group, then `General`. So an industry can go live as soon as its own row is approved, and a General row covers the rest if Harry approves one.
- **How specific it may be** (the label check, Harry, 7 Oct 2026; `labels.copy_level`): a label's own rows only when its label is confirmed (the rules and the model agree, the model is sure, an Overrides row or an approver's correction); the group's rows when the two disagreed within the group, under `label_check` = `skip`, or before the company is checked; General's when they disagreed across groups. So a doubtful label never carries a label's pitch, and **an approved, QA-passed row for each active group's own label and for General must stay on the Copy tab**: the safety net lands on them. Each card's *Industry* line says which (`rules and model agree · Fintech copy`, `⚠️ … · General copy`), and its *They do* line what the model read.
- **How it is personal:**
  - Industry-specific copy throughout.
  - The role line in email 1.
  - The opener: the line for the contact's role from the signal that set the angle, filled at enrol time with the account's own facts (Signals tab `opener_people`, `opener_founder`, `opener_ops`, `opener_self`; `enrol/openers.py`; Harry, 2 Oct 2026). Signals are context, never the line: the hiring, People, growth and funding signals' lines speak to the pressure the situation tends to bring, never to what was observed, and no opener mentions funding or money. An account with no signal line (the General angle, Control accounts among it) gets the generic line for the contact's role (General `opener_generic_people`, `opener_generic_founder`, `opener_generic_ops`, then `opener_generic`). The 30% holdout gets none.
  - The company, first name and place.
  - Email 1's subject, as a measured split (Harry, 5 Oct 2026). The Copy rows' subjects read like headlines ("Counseling that lives in Slack"); mail filters (Superhuman, Gmail) and people read a short, lower-case subject about the reader as personal mail. So the General `email1_subject_share` of companies (0.5), chosen by a hash of the account id with its own salt (independent of the opener holdout), get General `email1_subject` ("support for the {{company}} team"; `{{company}}` and `{{first_name}}` only) as email 1's subject; the rest keep the Copy row's `s1_subject`. Emails 2 to 4 keep the Copy row's subjects either way. The sheet check refuses a subject with markup, another variable, or anything the subject rules refuse (`copy_rules.subject_violations`), and a blank one while the share is above 0. Enrol records `contacts.subject_arm` (personal or copy; `v_account_outcomes` carries it); `us-outbound signals review` compares the arms' reply rates with the signals' test, and the daily post's To improve does once each arm has 50 emailed. The send card, `copy preview` (`--subject personal|copy`, or a stored account's own arm) and `seed send --subject personal` show the subject that is sent.
- **Email 1 links the industry's spill.chat page and asks for nothing** (`{{industry_url}}`; Harry, 1 Oct 2026: a demo ask in the first email is too presumptive). The Industries tab's `landing_page_url` is the page: `https://www.spill.chat/us/industry/` plus its slug. An industry with no page yet (Small Businesses, still a draft on the site) links Spill's US site instead (General `site_url`, https://www.spill.chat/us; Harry, 1 Oct 2026).
- **"Trusted by over 50,000 employees"** in email 2's "What is Spill?" links the US site (`{{site_url}}`; Harry, 1 Oct 2026).
- **One price:** `{{price_line}}` reads "Plans start from $195 a month for the whole team, on a rolling 30-day contract." for every team (General `price_from`; Harry, 1 Oct 2026). SPEC 4's price-by-size table is not quoted.
- **Sign-off:** "Best wishes," and the sender's first name (Harry, 1 Oct 2026).
- **Two links an email at most, body and signature together** (Harry, 6 Oct 2026; Instantly's unsubscribe link aside; `copy_rules.BODY_LINKS`). The signature always carries one, so the body has one: its call to action, the industry page in email 1 and the demo page in emails 2 to 4. The rule is a check, never a rewrite: copy with a second body link fails the sheet check and is never sent, so what Harry approves is what goes out. On 6 Oct every email 2 linked "trusted by over 50,000 employees" to the site; the Copy tab and `settings/data/copy.csv` now write it as plain words, and the style guide and facts list say so.
- **The signature: the sender's full name and one line with one link** (`templates/copy/signature.txt`, `render.signature`; Harry, 5 Oct 2026: "The signature should only have one line and link"). The line is one of three: Spill's US site (General `site_url`), "Book a call here" (General `booking_link`) or the Trustpilot reviews. The code picks it from the email's own links so it never repeats them:
  - The email suggests booking a call (it links the demo page or the booking link, or says "book a call", "book a demo", "book a time", "schedule a call", "set up a call" or "grab time"): the booking line, always.
  - Otherwise, not the website line when the email links a page on the site (the industry page, the US site, any spill.chat address but the demo page), and never a line whose address the email already links.
  - Among the lines left, a rotation by prospect and email number: different prospects get different lines, a prospect's next email the next line, and a re-render, a Slack edit and a preview the same line.
  - With today's copy, email 1 (the industry page) shows the booking or the reviews line, and emails 2 to 4 (the demo page) show the booking line.
- **Emails 2 to 4 each have one call to action, the demo page**, `{{demo_url}}` (General `booking_page`, https://www.spill.chat/us/book-demo), as a link in the text.
- **UTM tags on our links** (Harry, 6 Oct 2026; `enrol/utm.py`, General `utm_links`, no by default until a seed send shows tagged links don't land in AI "marketing" folders). In the HTML, every link to spill.chat (the industry page, the demo page, the US site) and the signature's site or booking link carry `utm_source=us_outbound`, `utm_medium=email`, `utm_campaign` (the Copy row's `copy_version`, so a visit names its sequence; a Slack edit keeps its card's) and `utm_content` (`step1` to `step4`, with `-signature` for the signature's link). Only the address behind a link changes: the words, the plain-text version, the copy rules and the signature's choice of line all read the bare addresses, so the sheet check and the render-time check are unchanged. Trustpilot is never tagged, and Instantly's unsubscribe link (`https://UNSUBSCRIBE_INSTANTLY.ai`, which Instantly swaps per lead) is in the campaign template, never in the rendered body, so the campaign drift check is unaffected too. `utm_links` = no sends bare links. With `email_format = text` the addresses are written out, so they stay bare.
- **The emails are HTML** (General `email_format = html`): embedded links, bullets and the bold headings of the long-form email, with tracking still off. `email_format = text` sends plain text with the links written out, if phase 0 finds Instantly mangles HTML in a custom variable. Instantly's `text_only` follows the setting.
- **Where the words come from:**
  - `templates/copy/style.md`: the voice, the four emails, the role lines, the markup, and Harry's long-form email as the model for email 2.
  - `templates/copy/facts.md`: the only claims the emails may make about Spill.
  - The industry's page, in the Industries tab's `page_*` columns: the pressures, the language and the angles.
- **The four emails:**

  | Email | Day | What it does | Words |
  | :- | :- | :- | :- |
  | 1 | 0 | The hook: a real pressure in this industry, the role line, one line on how Spill helps, the industry page link (no demo ask) | 60 to 110 |
  | 2 | 7 | Harry's long form: What is Spill? Who is Spill for? What makes Spill unique (four bullets, the last the starting price), the demo link | 180 to 280 |
  | 3 | 14 | A new angle, often from the page's FAQs: confidentiality, managers, out-of-hours, working with an EAP | 50 to 90 |
  | 4 | 21 | A polite close that leaves the door open | 40 to 80 |

- **The enrol job writes each lead's four emails and uploads them** as the lead's custom variables `{{s1_subject}}` … `{{s4_body}}`. The three campaigns (`us-outbound campaigns ensure --live`) have four steps that are just those placeholders. Nobody writes sequences in Instantly by hand; a sequence edited there is reported as drift the next morning.

**The QA pipeline: copy reaches a prospect only through all five gates** (`enrol/copy_desk.py`):

| # | Gate | Who or what | What it catches |
| :- | :- | :- | :- |
| 1 | Draft | The writing model (`claude_model`, Opus), `us-outbound copy draft --industry X --live`, or a person | |
| 2 | Sheet check | Code, free: `us-outbound copy check` renders every row for each role, from the demo host and from another sender, with sample and longest values | Everything the copy rules check (below) |
| 3 | QA | The task model (`claude_task_model`, Sonnet), `us-outbound copy qa --live`: writes `qa` ("pass 1a2b3c4d") and `qa_notes` to the row as each verdict comes, so a run cut short keeps what it did and the next run checks only the rows left | Claims not in facts.md or on the page, wrong data, statistics, tone, US grammar, the structure. A row that fails the sheet check fails QA without a model call |
| 4 | Approval | Harry: `status = approved` and `approved_by` | Judgment |
| 5 | Render-time check | Code, on every lead, in the enrol job | Anything the lead's own values break: a missing first name, a blank site or booking link that leaves the signature no line to show, an opener with a banned word (the opener is dropped) |

**QA stamps the exact wording:** `qa` carries a check code over the row's subjects, bodies and role lines. Edit the copy and the code no longer matches, so the row stops being sent until it passes QA again (`us-outbound copy qa --version <v> --live`, about a cent a row).

**The copy rules** (`enrol/copy_rules.py`; SPEC 10, plus Harry's changes):
- SPEC 10's word rules: counselor and counseling, never therapy or therapist; never licensed; never unlimited; American spelling; no statistic but "30% of employees use Spill"; never disparage their EAP; "EAP" never in Spill's name; demos only with Harry.
- The shape of each email:
  - It opens "Hi {{first_name}}," and ends with a sign-off and `{{sender_first_name}}`.
  - It carries exactly one link to the demo page.
  - It links only to the demo page, the industry page or spill.chat, with no bare addresses and no "click here".
  - The sequence links the industry page.
- Words per email, not counting the opener.
- No dollar figures: the price comes only from `{{price_line}}` (General `price_from`).
- The sign-off is "Best wishes," and `{{sender_first_name}}`.
- No exclamation marks, spam phrases, "Re:" or emoji in subjects.
- No line over 300 characters.
- Markup the system can read: no typed HTML, no broken links, no one-item lists.

**Two models, one cap:** `claude_model` (Opus) writes; `claude_task_model` (Sonnet) does the well-defined tasks (copy QA now, reply classification in phase 2). Both spend the same $10 a month (`claude_monthly_cap_usd`). Orchestration is the scheduler's code, not a model. Drafting all 106 sequences through the API would cost more than a month's cap, so the first library was drafted in the build session (Opus) and checked there (Sonnet), outside the cap. The in-system commands are for new and redone rows.

**The first library:**
- There are 106 draft rows: 105 industries and a General row.
- Insurance, HR consulting and Substance use treatment are partners, never contacted, so they have no copy.
- Every row passed the sheet check, then QA in the build session. 21 rows failed QA's first round, mostly claims beyond facts.md or the page, and were rewritten and checked again. Each row's `qa_notes` gives QA's summary and its minor suggestions.
- All are `draft` until Harry approves them.
- Load them with `us-outbound settings load --live` (below).

**Loading the new tabs into the sheet** (`settings/load.py`):
- `us-outbound settings load` (dry-run) prints what would change; `--live` writes it, then `us-outbound settings sync` brings it in.
- The dry-run lists every cell the load would change, row by row and column by column (`Games studios: naics_prefixes: '513210; 5112; 541511' → '513210; 5112'`); a long cell shows the part that differs, with a little either side (Harry, 7 Oct 2026).
- Industries rows are matched by label. Harry's `active`, `priority` and `proof_point` are kept, the build's other columns win, and rows Harry added stay. `--keep COLUMN` keeps the sheet's value in one more column for that load, such as a `page_faqs` line edited on the sheet.
- A Copy tab still in the old one-row-per-step layout is replaced. Its rows stay in the database's settings history, and until it is replaced the sync reads it as no copy and says so.
- A Copy tab already in the new layout keeps every row as it is; only missing versions are added.

**Phase 0 tests Instantly's custom-variable length limit.** The longest email 2 is about 2,350 characters of HTML with the old footer (the 5 Oct signature, with one line, is shorter than the 1 Oct one with three), and email 1 about 1,950 with the longest opener. If Instantly's limit is lower, the fallback is to put the signature in the campaign step, beside the unsubscribe link; its line differs by email and prospect, so it would go as its own custom variable.

## How Harry can steer the system

**Levers in the sheet:**

| Lever | Tab | Effect |
| :- | :- | :- |
| Focus the week on some industry groups | **Focus** (new): `industry_group`, `share` (60% or 0.6), `note` | Each listed group gets its share of the week's enrolment, in queue order within the group. The groups not listed share what's left of 100%. If a group has too few ready accounts, the rest of the day goes to the next accounts in queue order, so inboxes are never idle. The shares may not add up to more than 100%, and each group needs an active industry on the Industries tab |
| Approach specific companies | **Named accounts** (new): `domain` (a root domain like acme.com), `name`, `note` | Each domain comes in through the front door (same dedupe, suppression and partner checks) and gets the "Named by Harry" signal (+30, editable on the Signals tab). It still has to pass every other check, including HubSpot and Clay. Taking a row off stops the signal |
| Turn an industry on or off | Industries: `active` | The universe search and the queue include it or leave it out. For only some industries, switch the others off |
| Put an industry first | Industries: `priority` | Its accounts go ahead of others at the same score and size |
| Widen or narrow an industry | Industries: `naics_prefixes`, `exclude_naics`, `apollo_keywords` | Changes the Apollo filters and the Python re-check |
| Say what an industry is | Industries: `definition` (Harry, 7 Oct 2026) | The label check's line for the label: what a company under it is, and is not. Blank takes the build's. An edit (or a keyword edit) has the queue checked again over the next weekdays |
| Run on the rules alone | General: `label_check` = `skip` | For a Claude outage: new companies are verified on the rules' label with the group's copy. `required` is the default |
| Where | States | HQ states in or out (CA and WA never) |
| Who | Roles | Titles and who comes first by company size |
| What counts | Signals | Weights, new keyword signals on an existing source, Hold and Exclude rules |
| A single company | Overrides | Any field for one domain, winning over every source |
| Mix | General: `priority_threshold`, `standard_threshold`, `control_share` | How tiers are cut, and the share kept for the control group |

**Adding the two new tabs to your sheet.** Add a tab named `Focus` with the header row `industry_group | share | note`, and a tab named `Named accounts` with `domain | name | note`. Until they exist, the sync reads them as empty and carries on.

**Lookalikes** come from Spill's own HubSpot customers instead of a Seeds tab (Harry, 1 Oct 2026: "I'm happy using Spill companies from HubSpot to help inform lookalike target lists"). The monthly `lookalikes` job (`sources/lookalikes.py`, the 1st at 02:30 UK; Harry, 5 Oct 2026: "The customer base for Spill is fairly static") reads customer companies read-only, at company level only, and counts them into cells of industry label × size band. It also reads their 12-month headcount growth from Apollo's organization search by growth range, kept only as counts per industry group and band (`lookalike_growth`), and fills in the growth band of queue accounts that have none. From these it works out each account's lookalike fit, 0 to 100: industry 0.45, size 0.35, growth 0.20, with a part not known left out (Harry, 5 Oct 2026: "Sharpen up lookalike for scoring to include industry, size and growth rate"). The fit feeds the "Close match to Spill's customers" (+10) and "Some match to Spill's customers" (+3) signals, which replace "Looks like Spill's customers". The cells also keep every customer domain (current or former) out of the queue from the front door on, and are listed by `us-outbound lookalikes show` for the Focus tab and sourcing priorities; `us-outbound lookalikes fit` shows the fits and the tier mix the rows give, read-only.

**Lookalike leads** go one step further (Harry, 5 Oct 2026: "the customer base for Spill is fairly static, so the whole HubSpot pull and lookalike search can happen on a monthly cadence"). On the 1st of each month `lookalike_leads` (`sources/lookalike_leads.py`) takes up to 40 active customers as seeds, at most 5 per industry group and size band, in the groups the Focus tab names (all active groups when its shares leave room), in any country: most of Spill's customers are in the UK, and Harry wants them to inspire US lookalikes. Apollo's lookalike search returns US companies only, and each row is checked for a US HQ in an active state before it reaches the front door. The seeds stay in memory: no customer's domain or name is stored or logged. Each lead's `lookalike_lead` fact keeps the seed's group, band and country (US or non-US), and the run counts US and non-US seeds' yield apart, so Harry can see whether UK seeds work. The signal "Found as a lookalike of a customer" (+10 for 120 days) is on the Signals tab; `us-outbound settings load --tab Signals --live` adds it to the sheet.

## How the system learns

Harry, 6 Oct 2026: "push ahead with building" the learning loop (docs/roadmap.md §4, weeks 2 to 4, item 4).
Nothing re-weights or decides by itself: the system counts, and Harry changes the sheet.

| Part | Where | What it does |
| :- | :- | :- |
| `monday_readout` | `learn/readout.py`, Mondays 08:30 UK | Last week's sends, replies, positive replies, meetings, bounces and unsubscribes; the targets (`weekly_enrol_cap`, SPEC 12's funnel assumption, the stop rule); the exit criteria to scale, each met or not; the same numbers by tier, angle, industry group, sender and step; the signal table's headline; the tests at a look. Posts to #us-outbound (the dev channel in dry-run); `us-outbound readout` prints it |
| The signal table | `v_signal_value`, `learn/signal_value.py`, `us-outbound signals value` | Per active signal, over the enrolled companies: how many had it at enrolment (`contacts.signals_at_enrol`), were sent, replied, replied positively and booked a meeting, against the companies emailed without it |
| Test reads | Tests tab `kind` and `looks`, `learn/looks.py`, `us-outbound test read ID` | A test is read only at a pre-registered look, over what that look covers; before the first, the read refuses and shows no reply |
| Bookings | `crm/readback.py` (`hubspot_readback`, every 15 minutes), `events.source` | A meeting on Harry's calendar booked through his link (`hubspot_meeting`), or a Spill 3.0 deal at Demo requested or later at an enrolled company (`hubspot_deal`), recorded as `meeting_booked` |
| UTM tags | `enrol/utm.py`, General `utm_links` | Our links say which sequence and email a website visit or booking came from |
| The config version | `config_version.py`, `contacts.config_version`, `code_sha`, `copy_hash`, the `config_versions` table | What each contact was rendered under, as a 12-character id: the content tabs' versions, the sendable Copy rows, the General content keys, the signature template, the campaign constants and the code (Harry, 7 Oct 2026). Stamped at enrolment; a card waiting in Slack keeps the version it was rendered under |
| Cohorts | `learn/cohorts.py`, `us-outbound cohorts`, the readout's *Cohorts* section | Each enrolment week's companies at 7, 14, 21 and 28 days after email 1, by any cut (tier, angle, industry group, sender, copy version, subject and opener arms, config version); consecutive weeks compared at the same age from 30 companies a side, with what changed between them named as a change to test, not a cause |
| The settings report | `us-outbound cohorts changes`, the readout's *Settings changes*, the `config_log` table | What differs between two config versions in plain words (code, step days, the campaign template, the signature, General values, Copy rows, the content tabs' rows), and each change made to what leads in flight share (`campaigns ensure --fix --in-flight`, From names) |

**Small numbers read as small.** Under 30 companies on either side, a rate is "too few to read" and only
the counts are given (`signal_value.MIN_TO_READ`, the view's `too_few`, the readout). From 30, the signal
table and `signals review` use a two-proportion test on the reply rates, at p < 0.10: with a pilot's samples,
a lead to follow, not proof. A bounce rate on under 100 sends says that one bounce moves it a lot.

**The views behind it.** `v_account_outcomes` adds the meeting (any `meeting_booked` after step 1, with no
28-day limit) and the contact's angle, copy version and test. `v_readout_weekly` cuts by tier (at
enrolment), angle, industry group, sender (the account's) and step (sends, bounces and replies only), with
last week's activity (sends, bounces, replies, positive replies, meetings by company, unsubscribes,
complaints) beside the cohort by week of step 1. `v_signal_value` keeps SPEC 12's Control comparison and the
below-Control flag after 200, and adds the enrolled, sent and meeting counts and the "without" side.

**Cohorts, and developing while live.** A contact's emails are rendered at enrolment and never rewritten, so a
change to copy or settings reaches only the contacts enrolled after it; the campaign's step template, delays
and text_only are shared by every lead in it, so drift in those is held while leads are in flight
(docs/developing-while-live.md). A cohort is the UK week of a company's first enrolment, split by config
version when a week has two; contacts enrolled before 8 Oct 2026 are "unstamped". A company counts at an age
once it has reached it, so two weeks are compared over the same days after email 1.

**Pre-registered looks.** A count look `N` is reached when both versions have `N` companies whose 28-day
reply window has closed, and reads exactly the first `N` of each; a date look reads the companies whose window
had closed by that date. Replies count within each company's window and meetings until the look, so a late
read says what it would have said on time. `kind` = `holdout` reads a split enrol records anyway (the opener
holdout, `contacts.opener_arm`; email 1's subject, `contacts.subject_arm`) for the companies enrolled from
`start_date`, and may run beside the one copy test SPEC 9 allows.

**Copy variants.** `kind` = `variant` (Harry, 7 Oct 2026; `enrol/variants.py`) is a copy test that changes one
part of one email for every company, whatever its Copy row: a line after the greeting (`first_line`), a line
before the sign-off (`last_line`), the exact words in `find` (`replace`), or the subject, with each arm's text in
`text_a` and `text_b` (blank: the Copy row's email as it is). The arm is a hash of the company and the test, as an
`ab` test splits (SPEC 9), independent of the opener holdout and the subject split; every tier is in it, and a
second contact shares its company's arm. The sync checks each text against the copy rules that apply to it alone,
and every email again as it is rendered; where the change cannot be made, or would break a rule in that
company's email under either arm, the company is left out of the test and gets the Copy row's email. Enrol
records `contacts.test_id` and `test_arm`; the card names the arm; `learn/looks.py` reads the arms, counting an
email an approver edited in the arm it was given; and the running copy test is in the config version
(`copy_test`). One copy test, `ab` or `variant`, runs at a time. `share_a` (Harry, 8 Oct 2026; blank is 50%)
makes either kind uneven: version_a takes accounts whose hash falls in that share, `accounts_per_version` and the
count looks count the smaller arm, and the larger arm's are scaled to its share (`Test.cap`, `Test.scaled`).

**How a demo is booked, and read back.** The signature's "Book a call here" opens Harry's HubSpot meetings
link (General `booking_link`); the emails' call to action opens spill.chat/us/book-demo (`booking_page`),
which books into the same calendar (SPEC 4). `hubspot_readback` reads both ways: the meetings on Harry's
calendar booked through a meetings link, and one search of the Spill 3.0 pipeline for deals at "Demo
requested" or later made in the last three days, matched to enrolled accounts by their companies (our stored
id, else the company's domain) or contacts (the email's hash, else its domain), and counted only when made
after that company was first enrolled. Our own deal from a reply asking for a demo counts only once it
reaches "Demo created" (the per-company deal read, which also follows any deal once we hold the company). The
company counts once however many records the booking made. It only reads HubSpot (PHASE0-CONFIRM: whether
the website's page raises a Spill 3.0 deal itself).

## Changes for Harry

| # | Change | Why | SPEC | Status |
| :- | :- | :- | :- | :- |
| 1 | A free "someone to email" check before Clay | Clay credits are never spent on an account that can't be emailed | 9 | Approved 30 Sep; built in phase 1 |
| 2 | The Clay Accounts function takes `apollo_funding_date` and skips Company Latest Funding when it is set | Saves Clay credits | 8 | Approved; for the function build |
| 3 | One resolver with the precedence table (below) | Stops the last job to run from deciding an account's HQ, size or name | 13 | Approved; phase 1 |
| 4 | One owning source per fact (`open_roles` from `apollo_jobs`) | Removes three-way disagreement | 5, 7 | Approved; phase 1 |
| 5 | `pick_contacts` reveals emails only for the next two days | No credits spent on accounts later re-ranked or dropped | 9 | Approved; phase 2 |
| 6 | Apollo organisation enrich only when needed | Up to one Apollo credit saved per verified account | 7 | Approved; phase 1 |
| 7 | Vendor features stay off; check HubSpot auto-enrichment without changing it | Keeps one record and one ICP | 1.2, 3 | Approved |
| 8 | Decide whether `catch_all_valid` is sendable; test Instantly's "risky contacts off" | Otherwise a catch-all could be enrolled and never sent | 8, 9 | Approved; the test is in phase 0 |
| — | Monthly credit budgets (2,000 each) paced by the day, with the month's pace shown; a weekly enrolment target (150) | Harry | 5, 9 | Done |
| — | The send forecast replaces "caps ÷ 4", with Instantly's reports (limits, sends, sending status) and an "add a mailbox" flag | Harry | 9 | Done |
| — | Steps on days 0, 7, 14, 21; reply window 28 days | 150 a week instead of about 61 | 10, 12 | Done |
| — | Focus and Named accounts tabs | Harry | 5 | Done |
| — | Funding and an exact headcount from Apollo organisation enrich (`apollo_enrich`, weekdays 04:10, `apollo_enrich_groups`, 15% of the Apollo budget) | Harry, 2 Oct 2026: search rows carry no funding, so the funding signals never fired | 7, 9 | Done |
| — | People leaders at every queue account (`apollo_people`, weekdays 04:20, 0 credits); "First People hire" replaced by "First People hire (likely)" (+20, 60 days), which also needs `people_search_coverage >= 0.5` | Harry, 5 Oct 2026: only 12 of 432 companies had People data, "New People leader" had fired on none, and nothing wrote a count of 0 | 5, 7, 9 | Done; `settings load --tab Signals --live` brings the new row and switches the old one off |
| — | Website visits from Apollo's visitor filters on organisation search (`site_visits`, daily 06:00; one search per window, not SPEC 7's domain aggregates per account) | Harry, 5 Oct 2026: "the strongest intent signal" | 7, 9 | Done; the REST filters to confirm on the first run |
| — | Copy by industry and role, four emails a row; Harry's long form as email 2; the demo page as every email's call to action; the industry page linked; HTML emails | Harry | 5, 9, 10 | Done; drafts for Harry to approve |
| — | A link in every email: the industry page in email 1, the demo page in emails 2 to 4, where SPEC 10 had step 1 carry one link only (the privacy page) | Harry, 30 Sep and 1 Oct | 10 | Done |
| — | QA before approval: the sheet check, then the task model, stamped to the wording | Harry: "guards and QA" | 1.4, 10 | Done |
| — | `claude_model` (Opus) writes; `claude_task_model` (Sonnet) checks and classifies | Harry | 1.1 | Done |
| — | The learning loop: `monday_readout` (Mondays 08:30), the signal table, tests read only at pre-registered looks (Tests tab `kind`, `looks`), UTM tags (General `utm_links`), demo bookings read back from Spill 3.0 | Harry, 6 Oct 2026: "push ahead with building" | 9, 12 | Done; `settings load --tab General --tab Tests --live` brings the new key and columns |
| — | Cohorts: each contact stamped with its config version; campaign drift in the steps, delays or text_only held while leads are in flight (`campaigns ensure --fix --in-flight` applies it to them, logged); reports by enrolment week at fixed ages with what changed between them; a lead suppressed while in flight is stopped | Harry, 7 Oct 2026: "a cohort system in place for contacts that have started not being interrupted by changes" | 6, 9, 12, 13 | Done; `us-outbound db apply --live` on deploy adds the columns and tables |
| 9 | A weekly universe sweep over a quarter of the slices | Even Apollo spend through the month | 9 | Proposed (phase 1) |
| 11 | Limit and budget lines in the Monday readout, with the two alerts | Shows the bottleneck without asking | 12 | Proposed (phase 3) |
| 13 | A Seeds tab for lookalikes | More companies like the best ones | 5, 7 | Replaced 1 Oct by lookalikes from Spill's HubSpot customers (Harry); built |
| 14 | The Instantly plan's caps as a fourth term, from its API or `instantly_monthly_emails` | The workspace is shared with the EU campaigns | 1.2, 9 | Proposed; the phase 0 check first |

## Clay's two jobs (Harry, 2 and 6 Oct 2026)

Clay does only what Apollo can't: an email for a person Apollo can't verify, and a second opinion on an
HQ state or size Apollo leaves in doubt. Each is behind its own General switch, both no, and both spend
`clay_monthly_credits`, paced by the day, recorded in `credit_ledger` before each call.

**1. The email waterfall (`clay_email_fallback`).** Built. Before switching it on, confirm one Work Email
call through Clay's Routines API, for your own name:

```
us-outbound clay check-email --first YOUR_FIRST_NAME --last YOUR_LAST_NAME --domain spill.chat          (says what it would send)
us-outbound clay check-email --first YOUR_FIRST_NAME --last YOUR_LAST_NAME --domain spill.chat --live   (makes the one call)
```

It refuses any domain but Spill's own. It prints whether the endpoint answered, the output's keys and
shapes, the status our parser reads, the credits Clay reports, and then either "set clay_email_fallback =
yes on the General tab, then run `us-outbound sync`" or what failed and why (a 403 means "API & CLI" is not
ticked on Work Email; a validation failure means its input names differ). It exits 1 until the fallback
can go on.

**2. The cross-check (`clay_cross_check`).** Built; it needs the function below, which doesn't exist in
Clay yet. `verify_accounts` (weekdays 04:30) sends the run's doubtful accounts in one batch of up to 100,
each once (the answer is kept as a `clay` fact, with Clay's source note as its quote); a Clay error is
reported in the job's summary, the doubt stands, and the account is asked again the next run. 3 credits
are reserved per account until Clay reports the real cost.

### What to build in Clay: "US Outbound – Accounts", narrowed

In the folder "US Outbound", a function named **US Outbound – Accounts**. It answers two questions about
a company and nothing else. It replaces SPEC 8's fuller output for now: pages are read by `read_pages` and
funding comes from Apollo. If `verify_in_clay` is ever built, SPEC 8's keys can be added beside these; the
cross-check reads only these.

| | |
| :- | :- |
| Input `domain` | Text, always sent: the company's root domain, like `acmecreative.com` |
| Input `company_name` | Text, sent when we have one: the cleaned name, like `Acme Creative`. Use it to disambiguate, never in place of the domain |
| Output | Strict JSON: one object with exactly the keys below, each always present (null when unknown). Name the function's outputs exactly so |
| `hq_state` | The 2-letter USPS code of the company's headquarters state (`IL`; `DC` counts), or null when it is unknown or the headquarters is outside the US. The headquarters, not a branch office or a person's location |
| `employees` | The number of staff as a whole number (`64`), not a range (`51-200`) or text (`64 employees`); null when unknown |
| `source` | One short sentence, at most 300 characters, saying where each value came from, like `LinkedIn company page: 64 employees, HQ Chicago, IL`. Harry reads it on the hand-check |
| `credits_used` | Optional: the credits the row cost, if the function can give it |
| How to fill them | A data provider (Clay's company enrichment from whichever provider the plan includes) or Claygent reading the company's own site and LinkedIn page, cheapest first. Report what the source says; never judge whether the company fits |
| Settings | Tick "API & CLI" in the function's Integrations settings, or the API returns 403. Set a workbook spend limit if the plan offers one |

Example output: `{"hq_state": "IL", "employees": 64, "source": "LinkedIn company page: 64 employees, HQ Chicago, IL"}`

Then: paste the function's id (`t_…`) into the General `clay_accounts_function_id`, set `clay_cross_check` =
yes, and run `us-outbound sync`. The sheet refuses `clay_cross_check` = yes while the id is blank.

What the answer does, per account:

| Apollo | Clay | What Harry sees |
| :- | :- | :- |
| No HQ state | `IL` | Nothing: filled in, and the account verifies on its other checks |
| No HQ state | null, or outside the US | "Apollo gives no HQ state", word for word |
| No count and no band | `120` | Nothing: filled in (`employees` 120, band 100-249) |
| No count and no band | `49` (near the 50 edge) | "Clay says 49 staff, Apollo gives none" |
| 49 (near the 50 edge) | `45` | Nothing: the same side of every size edge (the floor, 50 staff and the ceiling) |
| 49 | `62` | "Clay says 62 staff, Apollo says 49" |
| 64, band 20-49 (they disagree) | `62` | Nothing: Clay sides with the count |
| HQ `NY` | `CA` | "Clay says CA, Apollo says NY" |
| No industry | anything | "Apollo gives no industry": Clay doesn't settle industry, as a loose label would pick the wrong copy |
| A field set by an Overrides row | anything | The Overrides row stands: Clay neither fills nor questions it |

Approving the hand-check clears "Clay says …" reasons like any other doubt; a missing fact still needs an
Overrides row.

An Overrides row is laid over an account in one place (`settings/overrides.py`, 9 Oct 2026): an `industry` row
brings its label's group, an `employees` row its size band. An `industry` row, or an `industry_group` row alone (the
group's own label), also reaches the account's columns: the next `verify_accounts` decides an account whose
columns lag the row again, verified or not and asking the model nothing, so enrol's copy, the queue and the cards
follow it.

## Which value wins

Sources write facts only. One resolver sets the account's columns from the facts, before every scoring run, by this precedence. Without it, the last job to run would win, so the Apollo sweep would overwrite what Clay had just confirmed.

| Field | Order |
| :- | :- |
| clean_name | Override → Clay's name, if it passes our rules → Apollo's name after our rules |
| legal_name | Override → Clay |
| domain | It is the key. If Clay's `domain_confirmed` is a different root domain that is not a known alias, the account goes to the hand-check |
| hq_state, hq_city | Override → Clay → Apollo. A state disagreement goes to the hand-check |
| employees, size_band | Override → Clay → Apollo (organisation enrich's exact count over the searched band). A band disagreement goes to the hand-check |
| industry label | Override → an approver's correction (`label_source` approver) → the label check's decision once made (`label_source` set: a source finding the company again never puts the raw rules' label back) → Clay's label, if it is on the Industries tab → the label whose NAICS or keywords matched |
| industry_group | Always from the label via the Industries tab, never a vendor's own category |
| naics | Apollo |
| founded_year | Override → Clay → Apollo |
| Funding | The most recent round from Apollo (organisation enrich) or Clay |
| US headcount, CA/WA/FL share, people leaders | Apollo people (the only source) |

Each fact name has one owning source. `open_roles` comes from `apollo_jobs` only, and `job_posts` supplies posting text only. The default "Hiring and growth" signal reads `apollo_jobs` (open_roles) and `apollo_org` (headcount_growth_12m).

## How the parts stay decoupled

| Layer | Does | Never |
| :- | :- | :- |
| `clients/` | One file per vendor: HTTP calls, response shapes, and the guard | Makes decisions or reads settings |
| `sources/` | One module per source key: vendor → facts in `signal_events` | Scores, calls another source, or writes account columns. The universe module is the only one that creates accounts |
| `clean/` | Pure functions: names, domains, people | Calls a vendor |
| resolver | Facts → account columns, by the table above | Calls a vendor |
| `scoring/` | Facts + sheet → score, tier and angle | Calls a vendor |
| `accounts.py` | The front door: one account per root domain, suppression and partners checked | Calls a vendor |
| `budget.py` | The month and each budget's pace; the week and the weekly target | Calls a vendor |
| `enrol/capacity.py`, `enrol/focus.py`, `limits.py` | The send forecast (from what Instantly last reported), the focus shares, today's number and why | Calls a vendor |
| `contacts/`, `enrol/` | Pick, verify, render, enrol | Change a score |

Jobs talk only through tables and `accounts.status`, never by calling each other. Each one can be re-run safely.

Swapping a vendor means rewriting one client and one source module. The fact names stay the same, so scoring and the sheet don't change. The in-house page reader (`sources/pages.py`, 2 Oct 2026) is the example: it writes the `benefit`, `mental_health_provision`, `values_page` and `read_status` facts Clay's function would, under its own source key `careers_pages`, and the page signals list both sources.

## To measure in phase 1

- **Universe size per slice, and so the Apollo credits per sweep.** It has to fit in the month's 2,000 alongside enrichment and reveals.
- **Clay credits per account** (SPEC 8), and so whether 2,000 a month keeps 150 accounts a week ready.
- **How many accounts the free checks keep away from Clay.**
- **Apollo's email hit rate**, which sets how often the Clay email waterfall runs.
- **The page reader's coverage** (`us-outbound pages show`): the share of accounts with benefits text,
  with a job board, and matching each page signal. After 200 accounts it decides whether to enhance it.
- **Instantly's real behaviour on the paused campaigns, before any send:**
  - the step timing, days 0, 7, 14 and 21;
  - the analytics fields;
  - the sending-status codes;
  - whether the API reports the plan's usage.

