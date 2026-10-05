# US Outbound: design and build review, and the plan to double interest per lead

Prepared 1 Oct 2026 for Harry Dryden and the Spill team. Repo `/home/user/columbus` at `cb0bf8f` (1,273 tests pass). This synthesizes five research reports (01 lead quality, 02 outreach, 03 best practice, 04 tool capabilities, 05 Spill's own HubSpot evidence). Every code claim the plan rests on was re-read in the repo, and the scoring and title-mapping claims were re-run against the real defaults. Where a report was wrong or overstated, this document says so (Appendix C).

File references are relative to the repo root. "Report 05 §3.5" means that section of `05-spill-evidence.md`.

---

## 1. Verdict

**What is good, and worth keeping.** The architecture is unusually disciplined for a cold-email system. The company is the unit; one sender per account for life; the sheet is the only ICP and every vendor feature stays off; facts are stored with source and date and Python makes every decision; copy rules are enforced at render time, not only by review; a guard sits in front of every outbound call and tests prove no write leaves the allowed containers; dry-run is the default; the Control tier gives a signal-blind holdout from day one; the budget pacing and the "what limits today's number" explanation are better than most production systems have. The 106 industry sequences are clean, specific about industry pressure, and free of hype. None of this should change.

**What most limits interest per lead.** Four things, in order:

1. **The person.** Nothing chooses the contact yet (`pick_contacts` is a stub, `ops/cli.py:79`; `enrol/enrol.py:241-250` takes the earliest-created sendable contact). The default title lists miss Head of HR, VP HR, CHRO, Director of People, People Ops Manager, Owner, Principal and Partner (verified by probe; `settings/defaults.py:315-333`), put HR Manager (22% win in Spill's data) in the first-choice list, rank Operations ahead of the founder at 50–249 (Ops won 25% at 50+, founders 56%), and never contact a People leader at a 10–49 company. One contact per account makes every one of those mistakes fatal for the account.
2. **The ask.** Email 1 asks for nothing (Harry's decision, `docs/open-questions.md` #38, #69). Emails 2–4 repeat the same demo-page link. 76 of 106 rows never ask the reader a question. In cold email the reply is the conversion, and email 1 is where most replies come from.
3. **The first email as delivered.** It is HTML with two links, 47% legal text by word count (the Article 14 notice names Apollo and Clay, `templates/copy/article14.txt`; `enrol/render.py:254-257`), sent at 30 a day from day one with no ramp, 75% of volume on one domain, and no kill rules until phase 3.
4. **The evidence never reaches the email.** No signal has an opener (`defaults.py:392`), `{{proof}}` and `{{place}}` appear in no row, `proof_point` is blank on all 108 industries, General and Control accounts get no opener at all (`enrol.py:364-366`), and a site visitor lands in Standard with the General angle (probe: visit + pricing page = 45 points). The scoring engine collects evidence and then throws it away at the door of the email.

**The 2x thesis, in one paragraph.** Interest per lead is a product of eight factors (§2). The current design is strong on the plumbing and weak on four of the eight: who we write to, what we ask, whether it lands, and whether it sounds like it was written for them. Each of those is fixable before the first send, mostly with sheet changes and small code, using tools already paid for. Stacked with a 40% overlap discount and a 10% execution haircut, the evidence-weighted mid case is about 2.2x on positive replies plus demo requests per account; the uniformly conservative case is about 1.5x, and the optimistic case about 3.4x (§6). Two levers decide which case we land in, the ask and the person, so they go first and are tested first with big-swing designs that this volume can actually read (§8).

**Top 5 moves.**

1. Build `pick_contacts` properly before anything sends: wide senior-title lists, a seniority rank, the founder as the fallback at 50–249, the People leader as a fallback at 10–49, the person named by the signal preferred, and a second contact (founder or senior People leader) at 50–249.
2. Redesign the ask ladder and test it as the first experiment: a soft interest question in email 1 (not a demo ask), an offer in email 2, an objection question in email 3, a referral ask in email 4.
3. Make email 1 land: plain text (`first_email_text_only`), a short layered Article 14 line, a 10→20→30 ramp, bounce kill rules in phase 2, and reply rate split by mailbox provider.
4. Put the evidence in the email: signal openers in the sheet now, a Claude-written per-account opener (Batch, under $3 a month) in phase 3 with a holdout, and named US customer proof from HubSpot's 59 US customers.
5. Fix the scoring and ICP before the first sweep: no double count for a carrier EAP, Hiring wired to `apollo_jobs`, Q4 off, funding decayed, site visits to Priority, a size signal favoring 10–99, IT-services NAICS excluded, Legal Teams on from launch.

---

## 2. How interest per lead is made

### 2.1 The metric

**Interest per lead** = (positive replies + referrals to the right person + demo requests, including bookings on the demo page with no reply) ÷ accounts with step 1 delivered, measured 28 days after step 1. The account is the unit, so a reply from either of two contacts counts once. Secondary: demos held per 100 accounts, which is what pays.

`sql/views/00_v_account_outcomes.sql` already computes positive-or-referral within 28 days per account. It needs demo requests added (from `hubspot_readback`, unbuilt), and "human reply" should exclude `unsubscribe` and `negative` from the *positive* numerator (it does) but the view also counts not-yet-classified replies as human (line 9), which inflates early reads; count only classified, closed windows.

### 2.2 Baseline funnel (650 accounts a month, current design)

| Stage | Conservative | Mid | Optimistic | Basis |
| :- | :- | :- | :- | :- |
| Step 1 delivered (not bounced) | 95% | 97% | 98% | Apollo verified only; Outreach average bounce 2.8% (03 §1.1) |
| Reaches the inbox | 70% | 80% | 88% | Validity 2025: Gmail 87%, Microsoft 76%; new domains, HTML, 2 links, no ramp push toward the low end (03 §3) |
| Read | 45% | 55% | 60% | Untracked; inferred |
| Human reply per delivered | 3.0% | 4.0% | 5.0% | SPEC 12's own assumption; Instantly avg 3.4%, Hunter 4.5% (03 §1.1) |
| Positive share of replies | 30% | 33% | 40% | Saleshandy 34–50%; the EAP objection will be common (03 §1.1, §8) |
| **Positive per delivered** | **0.9%** | **1.3%** | **2.0%** | |
| Demo booked per positive | 50% | 55% | 60% | Inference; Spill books fast when it books |
| Held per booked | 75% | 80% | 85% | Spill: 76% of closed qualified deals reached a demo; 24% dropped before (05 §4) |
| **Per month: positives / booked / held** | 5.6 / 2.8 / 2.1 | 8.3 / 4.6 / 3.7 | 12.7 / 7.6 / 6.5 | |

SPEC 12's working assumption (3–5% reply, about 1% positive, 3–6 meetings a month) sits inside this range. Note Belkins' 0.45% is per *email*, not per lead; over four emails it is about 1.8% per lead, which is why it is not used as the floor.

**2x means** about 2.6% positive-or-demo per delivered account: roughly 17 positives, 9 bookings and 7 held demos a month at today's capacity, from about 8, 4.6 and 3.7.

### 2.3 The factor decomposition

Interest per lead ≈ right account × right moment × right person × reaches the inbox × relevant message × easy ask × follow-through, with multi-threading acting on "right person" at the account level.

| Factor | What sets it today | Where it leaks |
| :- | :- | :- |
| Right account | Industries, States, size bands, hard exclusions, Hold list, HubSpot checks | IT services inside "tech"; carrier EAP scored as a plus; complements held like competitors; 100–249 ranked above 10–19; Legal off until January |
| Right moment | Signals and `counts_for_days` | No decay; Q4 +10 for all; Hiring never fires on `apollo_jobs`; renewal month unknown; site visits land in Standard |
| Right person | Roles tab and `pick_contacts` | Not built; titles miss most buyers; no seniority; one contact |
| Reaches the inbox | Mailbox registry, Instantly settings, copy rules | HTML + 2 links; no ramp; 90 a day on one domain; no kill rules; catch-alls enrolled while Instantly drops risky contacts |
| Relevant message | Industry copy, role line, opener | Opener is a stock sentence or absent; no proof; email 2 is a brochure; Article 14 doubles email 1 |
| Easy ask | CTA in each step | None in email 1; same page link in 2–4; no question; no referral path |
| Follow-through | Reply desk (unbuilt) | Harry alone approves; 2-hour repost ends 4 pm ET; no objection or referral drafts; no not-now path |

Volume is a separate axis. Levers A–H in §6 all move interest *per account*. Only the second contact (D) trades volume for it, and only the mailbox count changes volume.

---

## 3. Design and build evaluation

### 3.1 Strengths

- One owner per job and one record (`docs/pipeline.md` "One owner per job"); vendors return observations, Python decides.
- The guard (`clients/guard.py`) and `tests/test_guardrails.py`: every write is allowlisted by container.
- Settings versioned with `effective_from/to`; invalid tabs keep yesterday's version.
- Render-time copy rules (`enrol/copy_rules.py`) with a QA hash so edited copy cannot ship unchecked (`settings/model.py:268-290`).
- Budget pacing by day and the `limited_by` explanation (`limits.py`, `budget.py`).
- The Control tier and `v_signal_value`: a holdout exists from the first send.
- The send forecast per sender (`enrol/capacity.py`) so no step ever waits for a full inbox.

### 3.2 Design flaws (would hold even if everything were built as specified)

1. **One contact per account with `stop_for_company` on** (SPEC 2; `clients/instantly.py:43`): a single point of failure per account, after the Clay spend.
2. **The role rule contradicts Spill's own data** (05 §3.5): HR Manager as a first choice, Operations before the founder at 50–249, and no People leader fallback at 10–49.
3. **Scoring is static fit with binary freshness** (`scoring/score.py:119-128`): a funding round counts the same on day 1 and day 539; Q4 adds 10 to everyone.
4. **EAP is scored as a positive twice** (`defaults.py:156-168`): the terms "EAP" and "employee assistance" are in both "Mental health support listed" (+25) and "EAP named" (+10). Probe: a carrier EAP alone scores 45 in October (Standard); add a values page and it is Priority. Spill's data says EAP-in-place deals won 32% against 46% with nothing (05 §3.6).
5. **The ask ladder is flat**: no ask, then the same demo page three times (02 §2).
6. **Email 1 carries the whole Article 14 notice plus a four-line footer**, in HTML, with two links, against SPEC 10's own "plain text, one link" design.
7. **Site visits lead nowhere**: a visit after email 1 only reaches the daily post (SPEC 7 "not alerted"); a visitor's angle is General, so no opener.
8. **The first test (`t1`) tests one industry row's opener** and names versions that no longer exist (`defaults.py:360-377`; open-questions #74). Structural tests across all rows are not possible in the current `choose_copy` (`enrol.py:331-351`), and `read_test` has no significance or interval.
9. **The learning loop is biased**: `rescore` deletes and rewrites every `signal_matched` row (`score.py:536-539`), so a 90-day signal vanishes from accounts already sent; the Control baseline is "tier is Control now" (`02_v_signal_value.sql:67`), which shifts when Q4 points fall away in January.
10. **Cadence was set by capacity** (`pipeline.md:224-231`): days 0/7/14/21 solve the weekend pile-up, not conversion. Acceptable, but each step is a new thread with its own subject (`registry/mailboxes.py:59-61`), so "following my note" has no note under it.

### 3.3 Build gaps by pipeline stage

| Stage | Built | Spec-only | Missing or wrong |
| :- | :- | :- | :- |
| Universe | `accounts.admit` (`accounts.py:37-62`); Named accounts (`sources/named.py`); Apollo `search_organizations` | `source_universe` slicing; IRS BMF; GPTW/B Corp | `admit` does not dedupe against HubSpot (pipeline.md:76 says it does); `follow_redirect` has no caller; job disabled (`schedule.py:36`) |
| Enrichment and resolver | Apollo enrich, Clay run/poll/parse clients | The "Which value wins" resolver (pipeline.md:354-372); `verify_in_clay` | Nothing writes `hq_state`, `employees`, `size_band` to `accounts`; a real account would be Excluded as "HQ state unknown" (`tiers.py:201-203`) |
| Signals | Matcher, freshness, condition parser; `named` writes facts | `apollo_people`, `apollo_jobs`, `site_visits`, `job_posts`, `irs_bmf`, `layoffs` | `Hiring and growth` reads `apollo_org` only (`defaults.py:206`); tracker has `data_received: false` |
| Score, tier, angle | `score_account`, `tier`, `choose_angle`, `rescore` | | No decay; EAP double count; Q4 flat; Hold list mixes competitors and complements; two hard exclusions mis-sized (`tiers.py:220-229`) |
| Contact | `contact_block`, `pick_contact`, title→role mapper | `pick_contacts`, Apollo bulk match, Clay fallback, role rule | `pick_contacts` absent (`cli.py:79`); `map_title_to_role`, `first_choice_for_size`, `fallback_order` have no callers; no seniority; no `verified_at` on contacts |
| Queue and enrol | `order_key`, control share, focus quotas, `enrol.run`, capacity forecast | | Hand-check item is never created (`enrol.py:141-142` would always skip); `catch_all_valid` sendable (`enrol.py:57`) while `allow_risky_contacts` is false (`instantly.py:48`); size order 20–99 > 100–249 > 10–19 (`queue.py:40`) |
| Render and copy | Variables, markup, footer, Article 14, rules, QA desk | | All 106 rows draft; `privacy_url`, `postal_address` blank (blocks sends); no signal opener; `{{proof}}`, `{{place}}` unused; `proof_point` blank on 108 rows |
| Sending | Campaign create/drift, mailbox health, `add_leads` | `first_email_text_only`; threading; variable length limit (`instantly.py:89` is `None`) | No ramp (`mailboxes.py:55-56`); promote at 21 days whatever the score (`mailboxes.py:103-106`); Active mailboxes never demoted |
| Replies and approvals | `list_emails`, `reply`, `forward`, Claude JSON client with cap | Classification schema, routing, Slack alert, approvals, escalation | `replies/` empty; `templates/prompts/` empty; no objection, referral or not-now play |
| HubSpot | Six properties, write primitives, pre-send re-check, suppression load | Reply-triggered writes (`crm/hubspot_writes.py:168-171` is a comment); readback; deal on demo | `hubspot_active_sequence` and `hubspot_other_activity_90d` are read (`tiers.py:93,96`) but never written |
| Measurement | `v_account_outcomes`, `v_readout_weekly`, `v_signal_value`, `v_mailbox_health`, `test start/read` | UTMs (SPEC 2 row 11); `sync_outcomes`; `daily_post`; `monday_readout` | No cut by step, role, sender, provider or opener; no interval on `read_test`; `learn/` empty |
| Kill rules | `v_mailbox_health`, operator `stop/start` | All eight rules (SPEC 12) | Phase 3; nothing feeds them until `sync_outcomes` exists |

---

## 4. Gaps, ranked

### 4.1 Blockers: nothing can send until these are done

| # | Blocker | Evidence | Fix | Effort |
| :- | :- | :- | :- | :- |
| B1 | The source modules, resolver and `verify_in_clay` do not exist | `cli.py:73-77`; `schedule.py:36-40` | Phase 1 build (§7) | L |
| B2 | `pick_contacts` does not exist | `cli.py:79`; `contacts/__init__.py` empty | Build it in phase 1, not phase 2: the hand-check needs a named contact and title | M |
| B3 | The weekly hand-check item is never created, so `enrol` always skips | `enrol.py:133-151`; no writer of `kind="hand_check"` | A `hand_check post` job on Mondays (10 random queued accounts per active group, rendered email 1, evidence) and a `handled` path | S |
| B4 | `privacy_url` and `postal_address` are blank; render blocks sends | `render.py:52-57`; `phase0-facts.md:81` (privacy page is a Webflow draft) | Harry publishes the page and gives the address | S (Harry) |
| B5 | No copy row is approved | `copy.csv` status all `draft` | Harry approves after the launch-fix pass (§7) | S (Harry) |
| B6 | Instantly plan facts, key, variable limit, same-address follow-ups, HTML in variables, threading, `first_email_text_only` | `instantly.py:33,49,89`; open-questions #58 | One phase-0 session against a paused campaign | S |
| B7 | Clay "US Outbound" functions do not exist; REST path unconfirmed | `phase0-facts.md` Clay rows | Build with the extended schema (§5.2) and run on 20 accounts | M (Harry/Clay UI) |
| B8 | Apollo tracker: `us/pricing` slash, `/us/book-demo` missing, no data received | `phase0-facts.md` Apollo row | Harry, five minutes | S |
| B9 | Railway, Slack app, HubSpot service key, Sheets service account | `docs/build-plan.md` setup steps | Runbook | M |

### 4.2 Launch-depressors: shipped as designed, these would cut interest per lead

| # | Depressor | Evidence | Fix | Effort |
| :- | :- | :- | :- | :- |
| D1 | **Contact choice.** Titles miss the buyers; HR Manager is a first choice; Ops before founder at 50–249; no People leader at 10–49; no seniority; not tied to the signal; one contact | Probe of `map_title_to_role` with defaults: Head of HR, VP HR, CHRO, Director of People, People Ops Manager, Owner, Principal, Partner → None. 05 §3.5: founder 55%, senior HR 42% (50% at 50+), HR Manager 23%, Ops 35% (25% at 50+) | Appendix A roles; `pick_contacts` with seniority rank and signal preference; second contact at 50–249 | M |
| D2 | **No reply-inviting ask.** Email 1 asks nothing; 2–4 repeat a page link; no question in 76 rows; no referral ask; the one-pager was dropped | 02 §2; open-questions #38–39; Gong 304k: interest CTA 30% vs 15% meetings; offer +28%, meeting ask −44% (03 §2.2) | Soft question in email 1 (test vs Harry's no-ask), offer in 2, question in 3, referral in 4 (Appendix B) | S code, Harry decision |
| D3 | **Email 1 as delivered.** HTML, 2 links, 95 of 203 words legal, names the data brokers | `defaults.py:144`; `render.py:254-263`; `article14.txt` | `first_email_text_only`; short layered Article 14 (counsel); one link; monitor by provider | S |
| D4 | **No ramp, one domain, no demotion, no kill rules until phase 3** | `mailboxes.py:52-56,94-107`; `schedule.py:50` | Ramp 10→20→30 over 3 weeks Active; promote on score ≥90 *and* ≥14 days; bounce rules in phase 2; third domain (decision) | S–M |
| D5 | **Evidence never reaches the email.** No signal opener; General/Control get none; `{{proof}}` unused; `proof_point` blank; site visitors get no opener | `defaults.py:392`; `enrol.py:364-366`; `copy.csv` scan: `{{proof}}` 0, `{{place}}` 0, `{{company}}` in 0 email-1 bodies | Openers on the Signals tab (Appendix A); `proof_point` from HubSpot US customers; Claude opener in phase 3 | S then M |
| D6 | **Scoring wiring.** EAP double count; Hiring reads `apollo_org` only; Q4 +10 flat; values page +10; funding flat 540 days; "First People hire" needs a count that may be missing | Probe: carrier EAP = 45 (Standard), +values page = 55 (Priority); `open_roles=5` from `apollo_jobs` scores 0; no facts in October = 10 | Appendix A weights; all sheet edits except decay, which two rows achieve without code | S |
| D7 | **Hard exclusions mis-sized.** "Founded <2 years" removes funded seed teams; "<5 US people" relies on Apollo's thin coverage of 10–19s; every check fails open on a missing fact | `tiers.py:220-229` | Drop the age rule to 1 year or remove; apply the 5-people rule only at 50+ | S |
| D8 | **Industry definition.** 15 of 16 tech labels share NAICS incl. 5415 (IT services, MSPs, IT staffing); Digital health and Healthtech let teletherapy competitors in; Legal (57% win, 2nd largest US cluster) is off until January | `industries.csv`; `tiers.py:56-84`; 05 §3.2, §5 | `exclude_naics` 541512; 541513; 541519 on tech rows; add "teletherapy; online therapy; virtual therapy; mental health platform" to behavioral-health partner keywords; Legal Teams `active = yes` | S |
| D9 | **Size.** Queue ranks 100–249 above 10–19; one price "from $195 for the whole team" to 200-person firms | `queue.py:40`; `render.py:49`; SPEC 4 prices $995 at 101–200; 05 §3.1: 10–49 won 44% (US 33%), 100–249 won 24% (US 11%) | Size signal (+15 at 10–49, +10 at 50–99); rank 10–19 with 20–49; size-banded `price_line` (decision) | S |
| D10 | **Hold list treats complements as competitors**; no exit from Held; a carrier EAP counts as "modern vendor" nowhere, but a Calm subscription does | `defaults.py:170-176` | Split the row: direct competitors Hold; Calm, Headspace, Wellhub, Gympass Score +5 with Progressive employer | S |
| D11 | **Catch-alls** are enrolled and may never send | `enrol.py:57`; `instantly.py:48`; pipeline.md change 8 | Decide: exclude catch-alls at launch; filter Apollo people search to `contact_email_status = verified` | S |
| D12 | **Reply desk design leaks interest** even once built: Harry alone approves; repost only 13:00–21:00 UK; no objection or referral drafts; `contact_block` refuses any re-contact; OOO keeps sending | SPEC 11; `enrol.py:217-218`; `instantly.py:44` | Drafts for every class; repost window to 23:00 UK; referral and not-now plays; recontact rule read from settings | M |
| D13 | **HubSpot check after the Clay spend**; two exclusions never written | `enrol.py:376-399`; `tiers.py:93,96`; `accounts.py:37-62` | Daily HubSpot domain list (read-only) checked in `admit` and before `verify_in_clay` | S |
| D14 | **Measurement cannot see the levers.** No UTMs, no step, role, sender, provider or opener cut; `read_test` has no interval; test only covers one industry row | SPEC 2 row 11; `cli.py:471-529`; `enrol.py:346` | §8 | M |

### 4.3 Upside levers (beyond fixing the above)

| # | Lever | Evidence | Effort |
| :- | :- | :- | :- |
| U1 | Same-day action on `/us` visits: Apollo visitor filters daily (already in the guard's `APOLLO_READ_ACTIONS`, `guard.py:42-56`), Slack alert for enrolled accounts on pricing or demo pages, next step pulled forward via an Instantly subsequence | 04 §7.1; the only intent signal the system will ever see | M |
| U2 | Named US proof: 59 US customer companies in HubSpot, 7 notable active ones named in 05 §5 | SPEC 10 "use a named US customer once one exists" | S + Harry's permission |
| U3 | Claude per-account opener from stored facts, nightly via the Batch API, Sonnet or Haiku, cached system prompt, about $1–3 a month, with a 30% no-opener holdout | 04 §5; Hunter +56% for two attributes; Saleshandy hybrid 1.7x human-only (03 §5) | M |
| U4 | Dated triggers from free Apollo lookups: People role posted in 30–60 days; growth 6 months; Slack or Teams in use; PEO or HRIS in use | 04 §1 | M |
| U5 | Form 5500: plan-year start (renewal month) and Schedule A carriers (carrier EAP likely), free | 04 §6; brokers decide 8–12 weeks before enrollment (03 §8) | M (phase 3 or 4) |
| U6 | Second contact at 50–249 funded by lapsing Apollo credits (about 3,800 above the floor by Aug 2027) | Hunter 25k campaigns: 3.35% vs 1.61% account-level (03 §4) | M |
| U7 | Former Spill users now at US companies (+30): HubSpot UK champions matched by Apollo people search | 05 §3.4: "buyer used Spill before" won 60% | M (phase 4) |
| U8 | Reply plays with drafts: objection "we have an EAP", referral sideways, not-now date, OOO re-time; slots within 48 hours | 05 §4: won deals met in 2.3 days, lost in 5.9; HBR 7x within an hour (03 §6) | M |

---

## 5. Under-used tools

### 5.1 Apollo (30,188 lead credits; 1,200 visitor credits; 3M AI credits; waterfall enabled)

Paid for and idle: visitor filters on company search (`website_visitors_from_domains`, `_pages`, `_from_past`), free organization lookup with dated job and growth filters, technographics (Slack, Teams, Justworks, TriNet, Gusto, Rippling), `contact_email_status` on people search, polled waterfall via `webhook_result_show`, job postings and news. About 8,800 credits will lapse on 21 Aug 2027 at the 2,000-a-month pace (`pipeline.md:150-158`).

Concrete use: (1) `sources/site_visits.py` with one daily visitor-filter call instead of per-organization aggregates; (2) `sources/apollo_jobs.py` tag sweeps for "People role posted ≤ 60 days" and "grew ≥ 15% in 6 months"; (3) `contacts/pick.py` filters people search to verified emails so reveals land and Clay Contacts runs less; (4) spend the lapsing excess on second-contact reveals; (5) polled waterfall for catch-alls, if phase 0 confirms the REST path. The AI credits need a read-only exception (they write collections), so leave them unless Harry wants it.

### 5.2 Clay (2,000 credits a month; four existing functions)

Paid for and idle: Website Technology Stack and Website Traffic functions; richer fields from the same Claygent run at near-zero extra cost.

Concrete use: extend the Accounts function's output and `parse_accounts_output` (`clients/clay.py:295`) with `peo_or_broker`, `collab_tool`, `plan_year_month`, `awards`, `hiring_states`, `careers_platform`. Run Tech Stack only where Apollo has no technographics. Measure credits per account on the first 100 (SPEC 8), because 2,000 a month supports about 440 accounts at the 4.5-credit example, not 650 (01 §5). Spend Clay only after the free HubSpot and contact checks (D13).

### 5.3 Instantly

Paid for and idle: `first_email_text_only` (campaign field), subsequences with API moves, `update-interest-status`, AI reply labels as a second opinion, provider matching. Not recommended: A/Z auto-optimize (conflicts with SPEC 12), AI Reply Agent (conflicts with SPEC 1.3), the Website Visitors pixel (conflicts with contact-level tracking off). Inbox placement tests are a paid add-on ($47–97 a month): a decision for Harry.

Concrete use: set `first_email_text_only: true` in `CAMPAIGN_SETTINGS` (`instantly.py:41`) and the drift check; a "US Outbound – {owner} / warm" subsequence for visit-triggered pull-forward and not-now re-engagement; write the classifier's verdict back with `update-interest-status` so the shared Unibox agrees with Postgres. Each needs a guard allowlist entry scoped to the `US Outbound –` prefix (`guard.py:247-276`).

### 5.4 HubSpot (713 customers, 59 US; 3,858 closed-lost deals)

Paid for and idle as an *input*: customer history and lost reasons. Concrete use: a read-only `crm/customers.py` that lists US customers by industry group and proposes `proof_point` rows for Harry to approve by name; closed-lost reasons feed the objection drafts; `hs_analytics_*` page views on a warm lead go into the Slack alert for demo prep. Buyer intent and Breeze stay off (they break "warm leads only").

### 5.5 Claude ($10 a month cap; Opus writes, Sonnet checks)

Idle: Batch API (50% off), prompt caching, `effort` control, Haiku. Concrete use: `batch_json()` in `clients/claude.py`; cache `style.md` + `facts.md` as the system prompt; `output_config.effort: "low"` for classification and QA (thinking is billed as output); Sonnet for the nightly opener at about $2.70 a month batched (04 §5, design A). Everything fits under $10 with room; no cap change is needed. Web search at scale does not fit and is not recommended.

---

## 6. The 2x plan

### 6.1 Levers

Multipliers are on interest per account (positive + referral + demo request per delivered account). "Factor" is from §2.3. Credits are monthly.

| Lever | Factor | Low | Mid | High | Confidence | Evidence | Cost / credits | Guardrail | Effort |
| :- | :- | :- | :- | :- | :- | :- | :- | :- | :- |
| A. ICP and scoring rework: IT-services NAICS out; teletherapy to partners; EAP de-weighted; complements un-held; size signal; 10–19 ranked with 20–49; Legal on | Right account | 1.05 | 1.15 | 1.30 | Medium (direction strong; most Spill evidence is post-demo) | 05 §3.1–3.2, §3.6; 03 §2.1; probe | 0 | None | S |
| B. Right moment: funding decay via two rows; Hiring wired to `apollo_jobs`; Q4 off; People-role-posted trigger; site visits to Priority; renewal month later | Right moment | 1.05 | 1.15 | 1.30 | Medium-low (signal cohorts 1.5–2.5x, 15–30% of volume) | 03 §2.3; UserGems 3x; 04 §7.4–7.5 | 0–50 Apollo | None | S–M |
| C. `pick_contacts` with wide senior titles, seniority rank, founder fallback at 50–249, People leader fallback at 10–49, signal-named person first | Right person | 1.10 | 1.25 | 1.45 | Medium-high that the default is costly; the interest metric moves less than demo rate | Probe; 05 §3.5; Belkins founders reply most (03 §1.2) | 0 | None | M |
| D. Second contact at 50–249 (about 40% of accounts), staggered, same sender and version | Right person (account level) | 1.08 | 1.18 | 1.35 | Medium | Hunter 25k campaigns 2.1x account-level (03 §4) | +1 Apollo credit per account; volume 150→107 a week at 4 steps | SPEC 2 "one contact in v1": a change for Harry | M |
| E. Reaches the inbox: plain-text step 1; one link; ramp; promotion on score and time; bounce kill rules in phase 2; third domain; provider split | Inbox | 1.10 | 1.25 | 1.50 | Medium (mechanism clear, magnitude vendor-sourced) | 03 §3; Validity; Instantly's guide | 0 (third domain is a decision) | None | S–M |
| F. Relevant message: signal openers now; Claude opener in phase 3; named US proof; shorter Article 14; email 2 tightened; subject rework | Relevant message | 1.15 | 1.30 | 1.60 | Medium | Hunter +56%/+18%; Woodpecker ~2x; Saleshandy hybrid (03 §2.4, §5) | Claude $1–3 | Article 14 wording: counsel | S then M |
| G. Easy ask: soft question in email 1; offer in 2; objection question in 3; referral ask in 4 | Easy ask | 1.15 | 1.35 | 1.70 | Medium | Gong 304k CTA study; offer +28%, meeting ask −44% (03 §2.2); Instantly 58% of replies from email 1 | 0 | Changes Harry's "no ask in email 1": test it | S |
| H. Follow-through: 1-hour SLA in business hours; drafts for every class; referral sideways; not-now re-sequence; OOO re-time; slots within 48 hours | Follow-through | 1.03 | 1.08 | 1.15 | Medium-high (moves demos held more than "interest") | 05 §4; HBR (03 §6) | Claude pennies | Approvers: a decision | M |

### 6.2 Stacked estimate with overlap discounted

Levers in the same funnel stage act on the same emails and accounts, so they are not multiplied. Within a stage: 1 + Σ(m − 1) × (1 − overlap). Stages are then multiplied, and an execution haircut applied. Computed in `stack.py`.

| Case | Who (A+B+C+D) | Seen (E+F) | Act (G+H) | Product | Haircut | **Interest per lead** |
| :- | :- | :- | :- | :- | :- | :- |
| Conservative (low values, 30% overlap) | 1.20 | 1.18 | 1.13 | 1.58 | 5% | **1.5x** |
| Mid (mid values, 40% overlap) | 1.44 | 1.33 | 1.26 | 2.41 | 10% | **2.2x** |
| Optimistic (high values, 50% overlap) | 1.70 | 1.55 | 1.42 | 3.75 | 10% | **3.4x** |

For reference, the naive product of the low values is 1.96x and of the mid values 4.6x; neither should be believed.

**What this means.** The plan is 2x-capable, not 2x-guaranteed. Under uniformly conservative assumptions it lands near 1.5x; 2x is the evidence-weighted mid case. Sensitivity: if the ask lever (G) delivers nothing, the conservative case falls to 1.36x; at its mid value the conservative case rises to 1.69x. The ask and the person are therefore the two levers to build first and read first. On the metric that pays, demos held per 100 accounts, levers C and H add more than they add to "interest", because Spill's own data shows seniority and speed drive post-demo conversion (05 §3.5, §4).

**Volume.** Levers A–C and E–H leave weekly volume at 150. Lever D cuts it to about 107 a week at four steps (120 sends a day ÷ 5.6 sends per account), or 143 a week at three steps. Multi-threading raises interest per account but, at fixed mailbox capacity, roughly holds total interest flat (Hunter: per-email effectiveness is flat to slightly up). It is the right call for the 50–249 band because that is where the buying committee exists and where a wrong single contact wastes the most Clay; it is the wrong call at 10–49, where the founder is the committee. Adding two mailboxes restores volume and is a cost decision for Harry (§9, D5).

---

## 7. Roadmap to 18 Dec

Today is Thu 1 Oct. Blackouts: 23–27 Nov and 18 Dec–4 Jan. That leaves about seven send weeks from 26 Oct, so roughly 700–1,000 accounts will have been enrolled by 18 Dec and only about 500 will have closed 28-day windows. The roadmap is built for that: fix the depressors before the first send, start one big-swing test on day one, and make the readout able to see what matters.

### Phase 0, to Fri 9 Oct: foundations and decisions

Must be true before phase 1 starts:
- Harry's decisions D1–D8 (§9) taken; the sheet edited: Roles (Appendix A), Signals (Appendix A), Industries (`exclude_naics` on tech rows; Legal Teams on; `priority` for Legal), the Hold split.
- `article14.txt` and `footer.txt` approved in their short form, or counsel's answer on layering.
- Instantly session on a paused campaign: variable limit, same-address follow-ups, HTML in variables, `first_email_text_only`, whether a blank subject threads a step, sending-status codes, plan usage. Record results in `phase0-facts.md`; set `CUSTOM_VARIABLE_LIMIT`.
- Clay functions built with the extended schema; 20-account trial; credits per account recorded.
- Apollo tracker fixed; Railway, Slack, HubSpot key, Sheets account done.
- Code (S): `first_email_text_only` in `CAMPAIGN_SETTINGS` and drift; `hand_check post` job and CLI (B3); `_norm_link` to ignore query strings so UTMs can be added; `apollo.py` `website_visitors.search` action; `contact_email_status` filter on people search.

### Phase 1, Mon 12 – Fri 23 Oct: universe, contacts, scoring

- `sources/apollo_org.py` (sweep by group, quarter of slices a week, Proposed 9), `apollo_people.py`, `apollo_jobs.py` (with dated People-role postings), `site_visits.py` (visitor filter), `public_signals` (job posts as a source for the EAP and mental-health signals), the resolver (`pipeline.md:354-372`), `verify_in_clay` with the HubSpot domain check *before* the Clay call (D13), `admit` HubSpot dedupe.
- `contacts/pick.py`: role rule from the sheet, seniority rank (C-level > VP > Head/Director > Manager), signal-named person first, MX provider recorded on the contact, bulk match verified-only, Clay Contacts on misses, candidates for a second contact stored at 50–249 but not enrolled yet.
- Scoring: two funding rows, Hiring source fix, Q4 off, size signal, site-visit weights, Hold split, openers on signals; `tiers.py` age rule to 1 year and the 5-people rule only at 50+; freeze `signal_matched` rows for enrolled accounts (skip accounts in `ANGLE_FIXED_STATUSES` in the delete at `score.py:536-537`).
- Mailboxes: ramp caps (10/20/30 by weeks Active) in `registry/mailboxes.py`; promotion needs score ≥ 90 and ≥ 14 days.
- Hand-check on Mon 19 Oct with real accounts: clean name, HQ, size, *role title and seniority*, opener evidence ≥ 90% right.
- Measure: universe per slice, Clay credits per account, Apollo hit rate, share lost to catch-alls.

### Phase 2, Mon 26 Oct – Fri 13 Nov: first sends

- Copy: the launch-fix pass across all rows (Appendix B pattern): subjects de-templated ("support" in 67 of 106 email-1 subjects; "How Spill works for" in 77 of 106 email-2 subjects), email 2 tightened toward 160 words, an objection question in email 3, a referral ask in email 4, `{{company}}` in email 4. Harry approves; QA re-stamps. Effort M for Opus + Sonnet via the copy desk, within the cap over two weeks, or in a build session.
- Test 1 starts on day one: email-1 ask variant (Harry's no-ask-with-link as A; soft question, no link, plain text as B), hash-split across *all* rows (§8).
- `poll_replies` (Sonnet, effort low, SPEC 11 schema, drafts for positive, referral, objection and not-now), `poll_approvals`, Slack alert with "why this account" and HubSpot analytics, HubSpot writes, `hubspot_readback` with `meeting_booked` events and demo requests into the outcome view, `sync_outcomes`, UTMs on the two links, `update-interest-status` write-back.
- Kill rules brought forward: domain bounce > 3% on 100 sends; 5.7.x block; source bounce > 3%; plus an early stop check (reply < 2% after 400 delivered → pause for review).
- Go-live gate: hand-check approved, bounce < 3% on the first 200 sends, Postmaster shows no spam-rate warning, and both provider cohorts receiving replies.

### Phase 3, Mon 16 Nov – Thu 17 Dec: learning loop (blackout 23–27 Nov)

- Daily post and Monday readout with cuts by step, role, sender, provider, opener present, and the Bayesian read of Test 1.
- Claude opener (Batch) with a 30% no-opener holdout; `proof_point` filled from HubSpot for the three live groups.
- Second contact at 50–249, staggered 3–4 send days behind the first, founder or senior People leader whichever the first was not.
- Visit-triggered Slack alert and subsequence pull-forward; not-now re-sequence on the stored date; OOO re-timing.
- Remaining kill rules; `recontact_*` enforced in `contact_block`.
- Form 5500 only if phases 1–2 landed on time; otherwise January.

### What must be true before the first live send (checklist)

1. `privacy_url` live, `postal_address` set, footer and Article 14 approved.
2. Copy rows for Technology & Startups, Marketing & Creative Agencies, Legal Teams and General approved and QA-current.
3. `pick_contacts` has run; the hand-check shows ≥ 90% right titles and seniority.
4. `first_email_text_only` confirmed; variable limit known; follow-ups confirmed on the step-1 address.
5. Mailboxes Active on score *and* time; caps at the ramp value, not 30.
6. Bounce kill rules live; `sync_outcomes` feeding `v_mailbox_health`.
7. Test 1 registered with its decision rule (§8) and the variant renderer tested against empty and maximum values.
8. Harry's `[ASK HARRY]` sign-off, with `live_sending = yes`.

---

## 8. Learning and measurement plan

### 8.1 The sample-size problem, in numbers

Two-sided α = 0.05, 80% power, computed in `stats_synth.py`. Non-control volume is about 128 accounts a week (150 less 15% Control), or 64 per arm in a two-arm test across all rows.

| Test on | Per arm | Weeks at 64 per arm per week |
| :- | :- | :- |
| Positive 1% → 2% | 2,318 | 36 |
| Positive 1% → 1.5% | 7,750 | 121 |
| Reply 3% → 6% | 748 | 12 |
| Reply 3% → 4.5% | 2,517 | 39 |
| Reply 5% → 7.5% | 1,470 | 23 |
| Provider gap: Microsoft 5% vs Google 10% | 434 | 7 |

Power of SPEC 12's design (400 per arm) to see a 2x lift: 21% at a 1% base, 38% at 2%, 53% at 3%, 77% at 5%. A single industry row gets tens of accounts a week, so `t1` as designed could take a year. Conclusion: positive-reply rate cannot pick winners at this volume; wording tests are undetectable; only structural swings across all rows, read on total reply as the leading metric, can be learned from before spring.

### 8.2 Design

1. **Metrics.** Primary (slow): positive + referral + demo request per delivered account at 28 days. Leading (weekly): human reply rate per delivered, reply rate by step, positive share of replies, negative-or-unsubscribe rate (stop a variant above 1%), bounce rate (under 2%), reply rate by provider (MX) and by mailbox, time to first human response, demos per positive, demo held ratio, days booked-to-held (target under 3; Spill's winners met in 2.3 days).
2. **One structural test at a time, across all rows.** Add a `kind` column to the Tests tab: `copy_row` (today's behavior), `step1_variant`, `format`. For `step1_variant`, version B is a render-time transformation applied to every row's email 1 (a new `s1_ask` column holds the question; the industry link is dropped; `first_email_text_only` applies). `choose_copy` stays; `render_sequence` applies the variant for hash-B accounts. Effort M. This keeps SPEC 12's "one test at a time" and hash split (`queue.py:97-99`).
3. **Read sequentially, Bayesian.** Weekly, on closed windows only, compute P(B > A) on reply rate with Beta(1,1) priors. Pre-registered rules: after ≥ 300 per arm, if P(B > A) ≥ 0.85 and positive share is not lower by more than 5 points, shift allocation to 80/20; at ≥ 500 per arm or P ≥ 0.95, adopt B for all and start the next test; if P ≤ 0.15, adopt A. Keep a 20% exploration floor until adoption. Worked example from `stats_synth.py`: 9/300 vs 18/300 gives P = 0.96; 9/300 vs 14/300 gives 0.85.
4. **Holdouts.** The Control tier (15%) stays as the signal-blind baseline, but freeze each account's tier and matched signals at enrollment (D in §3.2 item 9) so the baseline does not move in January. The Claude opener runs with a 30% no-opener holdout inside Priority and Standard.
5. **Read every reply.** The classifier tags objection, competitor, trigger mentioned and persona; the Monday readout lists them. At 25 replies a month this qualitative stream is the fastest learning the system has.
6. **Test order.** T1 email-1 ask (phase 2, day one). T2 plain-text-only sequence vs HTML from step 2 (phase 3, if provider split shows a Microsoft gap). T3 Claude opener vs template (phase 3, built-in holdout). T4 second contact on vs off at 50–249 (phase 3; account-level metric). T5 long-form email 2 vs offer-style (January). Wording and subject tests: never.
7. **Kill-rule thresholds.** SPEC 12's "industry group reply under 0.5% after 400 delivered" is far too lenient; 0.5% is a broken campaign. Use 2% after 400, and the stop rule of 5 meetings per 1,500 accounts stays as the backstop (at 15–20% close on outbound meetings, that is about one customer; 05 §9 item 14).

### 8.3 What can be known by 18 Dec

About 500 accounts with closed windows: reply rate ± 1.5–2 points, bounce and complaint health, the provider split (detectable if the gap is large), step mix, objection mix, and a first Bayesian read of T1 at perhaps P ≈ 0.8. Not knowable by then: positive-rate lifts, signal values, copy-row differences. Say so in the readout so nobody over-reads December.

---

## 9. Decisions for Harry

| # | Decision | Recommendation | Evidence | Cost of being wrong |
| :- | :- | :- | :- | :- |
| D1 | A soft interest question at the end of email 1 (not a demo ask), tested against your no-ask version | Yes, as Test 1 from day one; keep "no demo link in email 1" in both arms | Gong 304k: interest CTA 2x meetings vs specific ask; Instantly: 58% of replies from email 1; your objection was to a demo ask, which this is not | If wrong: a few weeks of a slightly more forward email 1; the hash split limits exposure to half the accounts |
| D2 | Plain-text step 1 (`first_email_text_only`) and the link policy | Yes to plain text for all; test "no link in email 1" as part of T1; keep your industry link in arm A | 03 §3: Microsoft placement 76%; Instantly's own guide | If wrong: the industry page gets fewer visits for 6 weeks, which you cannot measure today anyway |
| D3 | Shorten the Article 14 notice to a layered line that points to the privacy page, and stop naming Apollo and Clay in the body | Yes, subject to counsel; Article 14(2)(f) asks for the source, and "business contact data providers and your company's public website" with a link may satisfy it | 02 §2: the notice is 70 of email 1's 203 words and invites "how did you get my data" replies | If counsel disagrees, keep the long form; the cost is a longer, colder email 1 |
| D4 | Roles tab rewrite and seniority rule (Appendix A) | Yes; sheet change | Probe; 05 §3.5 | Low; reversible in the sheet |
| D5 | Second contact at 50–249 and the volume trade: 107 a week, or 143 at three steps, or two more mailboxes | Second contact yes; keep four steps; add two mailboxes on a third domain if the cost is acceptable (a domain plus two Workspace seats, roughly $15–20 a month) | 03 §4; capacity maths in §6.2 | Without new mailboxes total interest is roughly flat while interest per account rises; with them, +33% volume |
| D6 | Legal Teams on from launch, not January | Yes, as the third group with `priority 2` | 05 §3.2, §5: 57% win, 10 of 52 US customers, 60% active, highest median amount | Low; 14 UK deals is a small sample, but the US customer cluster corroborates |
| D7 | Narrow tech: exclude IT-services NAICS; add teletherapy keywords to partners | Yes | 01 §3; `tiers.py:56-84` | Smaller universe; still tens of thousands of companies across seven states |
| D8 | Scoring weights (Appendix A), including EAP de-weight and Q4 off | Yes; sheet change | Probe; 05 §3.6 | Low; the tier-mix alert will show if thresholds need moving |
| D9 | Price line by size band versus one "from $195 for the whole team" | Size-banded line from SPEC 4 ("from $495 a month" at 51–100) | SPEC 4; a 200-person firm hearing $195 then $995 at the demo is a trust cost | Price is only 9% of lost reasons (05 §8), so either way is survivable; the banded line is more honest |
| D10 | Named US customers as proof, and "50,000" versus the site's "30,000" | Name customers per group with permission; align the site and the emails on one figure | 04 §4; open-questions #68 | A prospect who checks the site sees two numbers |
| D11 | Who may approve replies, and a speed target | Keep you as approver; extend the repost window to 23:00 UK; add Hannah and Sam as approvers for their own mailboxes, or allow a pre-approved positive template to send after 60 minutes unanswered | HBR 7x within an hour; Spill's winners met in 2.3 days | A guardrail change (SPEC 1.3). Cost of not changing: positives wait overnight when they arrive after 4 pm ET |
| D12 | Threading: steps 2 and 4 as in-thread replies (blank subject), step 3 as a new subject | Yes if phase 0 confirms Instantly threads on a blank subject; copy rules then allow a blank subject on those steps | 02 §2 "Threading"; email 2 says "following my note" | Low |
| D13 | Visit alerts for enrolled accounts (SPEC 7 says "not alerted") | Alert on pricing or demo visits, and pull the next step forward; no copy mentions the visit | 04 §7.1 | Low; no send happens after a reply without approval |
| D14 | Learning design: sequential Bayesian reads and a `kind` column on Tests, replacing "read once on the date" | Yes | §8.1 | Without it, nothing is learnable before spring |
| D15 | Paid options: Instantly inbox placement ($47–97 a month); Claude cap stays $10 | Placement tests optional; cap unchanged | 04 §3, §5 | Low either way |
| D16 | CA and WA, and FL | Keep the compliance exclusion and record its cost (12 of 52 US customers); turn FL on in wave 2 if compliance allows, since FL joins the 20% share test while off (`tiers.py:213-218`) | 05 §3.3, §5 | Known, accepted |

---

## 10. Appendices

### Appendix A. Scoring and ICP rework

**A.1 Roles tab.** Titles are semicolon lists; `map_title_to_role` matches whole phrases and takes the longest match, so adding "Head of HR" catches "Head of HR and Operations". Seniority is a new concept for `pick_contacts`, ranked C-level > VP > Head or Director > Lead or Manager, with a tie to the person the signal names.

| Role | Titles (add to today's) | 10–49 | 50–249 |
| :- | :- | :- | :- |
| People leader | Chief Human Resources Officer; CHRO; VP Human Resources; VP HR; Head of HR; Head of Human Resources; Head of Talent; People Director; Director of People; Director of Human Resources; Director of People Operations; Head of People Operations; VP People & Culture; Total Rewards Director. **Remove HR Manager** | 3rd | **1st** |
| People manager (new) | HR Manager; People Operations Manager; People Manager; HR Business Partner; HR Generalist; Benefits Manager; HR Lead | not contacted | 3rd |
| Founder or executive | Owner; Co-Owner; Principal; Managing Principal; Founding Partner; General Manager (keep "Partner" for Legal Teams only, via a note until per-group titles exist) | **1st** | 2nd |
| Operations | VP Operations; Operations Manager; Practice Manager; Studio Manager; Director of Finance and Operations | 2nd | 4th |
| Finance | unchanged | not contacted | not contacted (05 §3.5: 30%, n = 10) |

Reasoning (05 §3.5): founders and CEOs won 55% at every size; senior People titles won 50% at 50+; HR Manager and generalist titles won 22–26%; Operations won 25% at 50+. Today's defaults never reach a People leader at 10–49 and reach Operations before the founder at 50–249. Second contact at 50–249: whichever of founder and senior People leader was not first.

**A.2 Signals tab.** All are sheet edits except where marked. Two funding rows give decay without code.

| Signal | Now | Proposed | Why |
| :- | :- | :- | :- |
| Mental health support listed | +25; terms include "EAP; employee assistance" | +15; remove those two terms; add `job_posts` as a source | Stop the double count; EAP-in-place deals won 32% vs 46% (05 §3.6); keep it as a budget-and-brand signal |
| EAP named | +10 | +5; add Optum; Carelon; Cigna; Aetna Resources For Living; TELUS Health; Health Advocate; add `job_posts`; opener "I noticed your benefits page mentions an employee assistance program." | Trigger for the Upgrade angle, reframed around under-use, never disparagement |
| Modern mental-health vendor named | Hold, one list | Two rows: competitors (Talkspace, Lyra, Modern Health, Spring Health, BetterUp, Nivati, Tava, Wellbound) Hold; complements (Headspace, Calm, Wellhub, Gympass) +5 Score, Progressive employer | Calm and Headspace buyers are buyers of counseling too |
| Progressive benefits | +10 each, max +30 | +5 each, max +15; add plurals | Standard tech perks; max +30 skews Priority to VC-backed startups that most often already have Lyra or Modern Health |
| Culture or values page | +10 | 0 (inactive) | Nearly universal |
| People leader in place | +10 | +10 | Fine |
| New People leader | +30, 90 d | +30, 90 d; opener "Congratulations on the new role." | Keep: UserGems 3x within 30 days of a job change (03 §2.3). Report 05's 2-of-10 is a post-demo trigger on a tiny sample, not reply propensity. Confirm Apollo returns a start date in phase 1 |
| First People hire | +25 | +25; add a second row "People role open" +15, `apollo_jobs`, `open_people_roles >= 1`, 60 d, Growing team, opener "I saw you're hiring for a People role." | A missing `people_leader_count` never matches; the new row fires anyway |
| Recent funding | +20, 540 d | "Funding in the last 6 months" +20 (`days_since_funding <= 180`); "Funding 6–12 months ago" +10 (`> 180 AND <= 365`); nothing beyond | Decay without code |
| Hiring and growth | +15, source `apollo_org` | source `apollo_jobs, apollo_org` | Wiring bug confirmed by probe |
| Visited the US site | +20 | +35 | A visit alone should be Standard; with the pricing page (+25) Priority. Angle stays General, since the copy must never mention the visit |
| Viewed US pricing or demo page | +15 | +25 | As above |
| Q4 plan-year window | +10 | inactive | No power to tell accounts apart; shifts every tier on 1 January |
| Team of 10–49 (new) | | +15, `apollo_org`, `employees >= 10 AND employees <= 49` | 05 §3.1 |
| Team of 50–99 (new) | | +10, `employees >= 50 AND employees <= 99` | 05 §3.1 |
| Uses Slack or Teams (new, phase 3) | | +10 from Apollo technographics or Clay tech stack | Where Spill is delivered |
| On a PEO (new, phase 3) | | −10, Upgrade the EAP | Bundled EAP; 15% of 10–499 employers (03 §8) |
| Renewal in 60–120 days (new, phase 3–4) | | +20, Form 5500 plan-year start | Brokers decide 8–12 weeks before enrollment |
| Former Spill user at company (new, phase 4) | | +30 | 58–60% win (05 §3.4) |

Thresholds stay at 50 and 20; check the tier-mix alert after the first rescore.

**A.3 Hard exclusions and queue (code, S).** Age rule: founded less than 1 year ago (or remove; it conflicts with the funding signal). The 5-US-people rule: apply only when `employees >= 50`. `SIZE_BAND_RANK`: 10–19 with 20–49, then 50–99, then 100–249. `admit`: dedupe against a daily HubSpot domain list.

**A.4 Industries.** Tech rows: `exclude_naics` 541512; 541513; 541519. Legal Teams: `active yes`, `priority 2`. Partner keywords: add "teletherapy; online therapy; virtual therapy; mental health platform". Focus tab at launch: Tech 50%, Agencies 30%, Legal 20%.

### Appendix B. Two worked sequences

Both were rendered through `render.render_sequence` with the real copy rules (`validate_copy.py`); the base versions pass every rule as written. Role lines are the existing ones. Openers are examples of what the Signals tab (or the Claude opener) would supply; the line disappears when there is none. Variants marked **TEST** change a rule Harry set and were rendered to show exactly which rule they trip.

#### B.1 Technology & Startups (sent by Hannah to a People leader at a 60-person company)

**Email 1 (day 0, plain text). Subject: Counseling that lives in Slack**

> Hi {{first_name}},
>
> {{opener}} *(example: I noticed your benefits page mentions an employee assistance program through ComPsych alongside mental health days, so you're already investing here.)*
>
> Tech teams move fast and burn out quietly. Between layoff whiplash, always-on Slack and building against a runway, the strain usually stays hidden until someone strong gives notice.
>
> {{role_line}}
>
> Spill is on-demand counseling that anyone on the team can book from Slack or Teams, often for the same day. Here's [how Spill works for tech companies and startups]({{industry_url}}).
>
> Best wishes,
> {{sender_first_name}}

**Email 1, variant B (TEST: soft ask, no link).** Trips "email 1 has 0 links to the industry page; it has exactly one" (`copy_rules.py:389-390`) and "the sequence never links the industry page" (`render.py:283-289`). Needs those two checks relaxed for `step1_variant` arm B.

> …Spill is on-demand counseling anyone on the team can book from Slack or Teams, often for the same day, for one flat monthly fee.
>
> Worth a look for {{company}}?

**Email 2 (day 7, in-thread if D12). Subject: What Spill does for tech teams** (about 165 words; passes the 150–300 rule)

> Hi {{first_name}},
>
> Following my note last week, here's the short version of Spill for tech companies and startups.
>
> **What is Spill?**
> Spill is an on-demand counseling service, [trusted by over 50,000 employees]({{site_url}}). It helps tech teams stay productive, cuts absenteeism and frees up whoever carries HR, by dealing with the issues that most often derail work.
>
> **Who is Spill for?**
> Anyone at the company: engineers, designers, sales and support, and the founders themselves. Burnout from a sprint that never ends, survivor guilt after a layoff, imposter feelings, anxiety, or life events like a new baby or losing someone close.
>
> **What makes Spill unique**
> - Support the same day, in a couple of clicks. No waiting lists, phone trees or referrals.
> - Remote-native: video sessions across US time zones, early mornings, evenings and weekends.
> - Booking lives in Slack and Microsoft Teams, as well as email and any phone.
> - {{price_line}} We don't lock you in.
>
> If you'd like to see it, [book a short demo]({{demo_url}}).
>
> Best wishes,
> {{sender_first_name}}

**Email 2, offer variant (TEST, January).** 73 words; trips "email 2 has 73 words; it should have 150 to 300" (`copy_rules.py:293`). Needs the floor lowered for a `format` test and Harry's agreement to offer the one-pager again (open-questions #39).

> Following my note last week: Spill is on-demand counseling, [trusted by over 50,000 employees]({{site_url}}). Anyone on the team opens Slack or Teams, picks a time and talks to a counselor, often the same day. {{price_line}}
>
> I have a one-page overview written for tech teams. Want me to send it, or would you rather [see it in a short demo]({{demo_url}})?

**Email 3 (day 14, new subject). Subject: Already have an EAP through your carrier?**

> Hi {{first_name}},
>
> A quick one, since most tech companies already have an EAP bundled with their health or disability cover.
>
> Spill works alongside an EAP, or instead of one. The difference is where it lives: someone opens Slack or Teams, picks a time and talks to a counselor, often the same day. No phone line to find, and founders are covered on the same plan as everyone else.
>
> Would it help to see how the two sit together? [Book a quick demo]({{demo_url}}).
>
> Best wishes,
> {{sender_first_name}}

**Email 4 (day 21, in-thread if D12). Subject: Who looks after benefits at {{company}}?**

> Hi {{first_name}},
>
> Last note from me for now.
>
> If someone else looks after benefits or people at {{company}}, could you point me to them? And if the timing's wrong, no problem; this will keep.
>
> When it moves up the list, Spill can be live in Slack or Teams within hours. [See it in a short demo]({{demo_url}}) whenever works.
>
> Best wishes,
> {{sender_first_name}}

What changed and why: the subject on email 2 stops saying "brochure"; email 2 drops 50 words and the stock phrases; email 3 leads with the top objection and asks a question; email 4 adds the referral ask (Hunter: routing sideways beats the same department) and names the company; "If it's useful, here's" (in 60 rows) and "in a couple of clicks" (in 100 rows) are cut back.

#### B.2 Legal Teams (sent by Hannah to the managing partner of a 30-person firm)

**Email 1 (day 0, plain text). Subject: Confidential counseling for {{company}}**

> Hi {{first_name}},
>
> {{opener}} *(example from a dated trigger: I saw you're hiring for an office administrator, which is usually when a firm looks at benefits properly.)*
>
> In most law firms the billable hour sets the pace, client emergencies own the evenings, and asking for help can still feel like weakness. So people push on until they burn out or leave.
>
> {{role_line}} *(founder line: If you lead the firm, keeping good associates spares you the recruiting, ramp-up and lost billables that follow a departure.)*
>
> Spill is confidential counseling that attorneys and staff book from their own phones, often for the same day, and it never touches firm systems. Here's [how Spill works for law firms]({{industry_url}}).
>
> Best wishes,
> {{sender_first_name}}

**Email 1, variant B (TEST).** Same rule trips as B.1. Closing lines: "…and it never touches firm systems. Would it be worth a look for the firm?"

**Email 2 (day 7). Subject: What Spill does for law firms.** As rendered, line 9 ran to 307 characters (limit 300); the "Who is Spill for?" paragraph is split into two lines below, which fixes it.

> Hi {{first_name}},
>
> Following my note last week, here's the short version of Spill for law firms.
>
> **What is Spill?**
> Spill is an on-demand counseling service, [trusted by over 50,000 employees]({{site_url}}). It helps firms keep people productive, reduce absence and free up partners' and HR time by dealing with the issues that most often derail work.
>
> **Who is Spill for?**
> Everyone at the firm: attorneys, paralegals and business staff, and the owners too. Billable-hour burnout, the strain of adversarial work, perfectionism, anxiety, or life events like having a baby or losing someone close.
> There's no minimum headcount, so a boutique gets the same support as a national firm.
>
> **What makes Spill unique**
> - Support the same day, in a couple of clicks. No waiting lists, callbacks or referrals.
> - Sessions early mornings, evenings and weekends, around court schedules and client emergencies.
> - People book privately from their own phone. Nothing touches firm systems, and the firm sees only anonymized, aggregate data.
> - {{price_line}} We don't lock you in.
>
> To see how it would work at {{company}}, [book a short demo]({{demo_url}}).
>
> Best wishes,
> {{sender_first_name}}

(The existing row's "nothing is reported to bar associations" is a page-only claim flagged in open-questions #72; it is left out until confirmed.)

**Email 3 (day 14). Subject: Who covers paralegals and staff?**

> Hi {{first_name}},
>
> The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?
>
> They work the same deadlines and client emergencies as the attorneys they support. Spill covers everyone at the firm, booked privately from a personal device, often for the same day, with the owners covered too.
>
> Is that a gap at {{company}}? [Book a quick demo]({{demo_url}}) and I'll show you how other firms run it.
>
> Best wishes,
> {{sender_first_name}}

**Email 4 (day 21). Subject: Who handles benefits at {{company}}?**

> Hi {{first_name}},
>
> Last note from me for now.
>
> If someone else handles benefits or staff well-being at {{company}}, could you point me their way? And if the timing's wrong, no problem; this will keep.
>
> When it moves up the list, Spill can be set up in hours for a firm of any size. [See it in a short demo]({{demo_url}}) whenever works.
>
> Best wishes,
> {{sender_first_name}}

**Opener behavior checked.** `pick_opener` kept the three example openers and dropped "Saw your benefits page mentions unlimited PTO." for the banned word, as designed (`render.py:161-182`). The Claude opener job must therefore write from stored facts only, with the banned list in its prompt, and fall back to the signal template when the rules reject it.

### Appendix C. Evidence caveats, and where the reports were wrong or overstated

**On the reports.**
- Report 01 is accurate on every claim re-run here (title mapping, the 45-point carrier EAP, the `apollo_jobs` wiring, flat Q4, funding without decay, site visitors in Standard). Its Clay arithmetic (2.5 credits an account to reach 650 a month) is a projection; credits per account are unmeasured until phase 1.
- Report 02 is accurate on the code. Its threading claim (each step opens a new thread) is Instantly behavior that phase 0 must confirm, not something the repo decides. Its "47% legal text" holds: the rendered email 1 in Appendix B is 214 words of which about 95 are notice and footer.
- Report 03's figures are mostly vendor datasets with self-selected users and inconsistent definitions; Belkins' reply rates are per email, not per lead, and the "signal-based 15–25%" cohorts are small and hand-built. Its multipliers were discounted here (§6.1), and the direction of each lever is better supported than its size.
- Report 04 is accurate; `website_visitors.search` is already allowed by the guard but not yet in the Apollo client, as it says.
- Report 05's win rates are post-demo and UK-heavy; the US data is 2020–22; size bands disagree between "employees covered" and enrichment headcount at 50–99; the "New People leader" finding (2 of 10) is too small to override the reply-side evidence and is treated here as a flag for the readout, not a weight cut. Persona counts are contact-to-deal links, so a deal with two contacts counts twice. BigQuery was unreachable, so usage by segment, the best lookalike evidence, is still missing.

**On this review.**
- No cold-email reply data exists for Spill in any system read. The baseline funnel is inferred from vendor benchmarks and SPEC 12's own assumption; its first real read comes in late November.
- The multipliers in §6 are judgments anchored on the cited evidence, not measurements. The overlap discounts (30–50%) are the main source of uncertainty in the stacked estimate; a different discount moves the mid case between roughly 1.9x and 2.5x.
- The soft-ask recommendation (D1) rests on Gong's CTA study, which compared CTA *types* among emails that all had one; no study compares "no ask" with "interest ask" directly. That is why it is a test, not a change.
- The "right person" multiplier is smaller on the interest metric than on demos held, because referrals count as interest; the demo-held effect is where Spill's own data is strongest.
- Everything that depends on Instantly (plain-text step 1, threading, the variable limit, subsequences) is unverified until the phase-0 session; the plan assumes the API behaves as its documentation says.
- `docs/phase0-runbook.md:70` still says `price_from = 250` while the default and Harry's decision are 195; `enrol/capacity.py:8`, SPEC.md:429 and open-questions #57 still say days 3, 8 and 15. These are documentation drift, not behavior, but they will mislead whoever picks the work up.
