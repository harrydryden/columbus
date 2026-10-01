# US Outbound: design and build review, and the plan to raise interest per lead

Final version, 1 Oct 2026, revised after an independent red-team review (`11-red-team.md`). Prepared for Harry Dryden and the Spill team. Repo `/home/user/columbus` at `cb0bf8f` (1,273 tests pass). This synthesizes five research reports (01 lead quality, 02 outreach, 03 best practice, 04 tool capabilities, 05 Spill's own HubSpot evidence). Every code claim the plan rests on was re-read in the repo; the scoring and title-mapping claims were re-run against the real defaults; the copy in Appendix B was rendered through the real copy rules. Section 11 lists each red-team point and the ruling on it.

File references are relative to the repo root. "Report 05 §3.5" means that section of `05-spill-evidence.md`. The reports, the red-team critique (`11-red-team.md`), the first draft (`10-synthesis.md`) and the scripts cited (`probe_synth.py`, `stats_synth.py`, `stack_v2.py`, `looks_sim.py`, `validate_copy_v2.py`) are in this folder, `docs/gtm-review/`. Run the scripts from the repo root with the repo's `.venv`, for example `PYTHONPATH=. .venv/bin/python docs/gtm-review/stack_v2.py`. How it was made: five research agents (Opus) audited the code, researched 2025–26 practice and the tools, and read Spill's HubSpot history in aggregate; a Fable agent wrote the synthesis; a second Fable agent red-teamed it; the synthesis was then revised.

---

## 1. Verdict

**What is good, and worth keeping.** The architecture is unusually disciplined for a cold-email system. The company is the unit; one sender per account for life; the sheet is the only ICP and every vendor feature stays off; facts are stored with source and date and Python makes every decision; copy rules are enforced at render time, not only by review; a guard sits in front of every outbound call and tests prove no write leaves the allowed containers; dry-run is the default; the Control tier gives a signal-blind holdout from day one; budget pacing and the "what limits today's number" explanation are better than most production systems have. The 106 industry sequences are clean, specific about industry pressure and free of hype. None of this should change.

**What most limits interest per lead.** Four things, in order:

1. **The person.** Nothing chooses the contact yet (`pick_contacts` is a stub, `ops/cli.py:79`; `enrol/enrol.py:241-250` takes the earliest-created sendable contact). The Roles tab misses Head of HR, VP HR, CHRO, Director of People, People Ops Manager, Owner, Principal and Partner (probe, `settings/defaults.py:315-333`), puts HR Manager (22% win in Spill's data) in the first-choice list, ranks Operations ahead of the founder at 50–249 (Ops won 25% at 50+, founders 56%), and never contacts a People leader at a 10–49 company. One contact per account makes each of these mistakes fatal for the account.
2. **The ask.** Email 1's copy asks for nothing (Harry's decision, `docs/open-questions.md` #38, #69); since 1 Oct every email's signature carries a "Book a call here" link (commit `7bef045`), which is an exit for a reader already convinced, not an ask of one who is not. Emails 2–4 repeat the same demo-page link. No row asks a question in email 1 and 76 of 106 ask none anywhere.
3. **The first email as delivered.** HTML; 183 words, of which 68 (37%) are the Article 14 notice naming Apollo and Clay and 19 more are the three-line signature, so 48% sits below the sign-off (`templates/copy/article14.txt`, `signature.txt`; `enrol/render.py:253-263`); five links across four domains (the industry page, Spill, Harry's booking link, Trustpilot, Instantly's unsubscribe); 30 a day from day one with no ramp; 75% of volume on one domain; kill rules deferred to phase 3. The opt-out is Instantly's own unsubscribe link in every step (commit `8797784`); what remains is to confirm it on a seed inbox and to record Instantly's unsubscribes in the system's own suppression list (§4.1).
4. **The evidence never reaches the email.** No signal has an opener (`defaults.py:392`), `{{proof}}` and `{{place}}` appear in no row, `proof_point` is blank on all 108 industries, General and Control accounts get no opener at all (`enrol.py:364-366`), and a site visitor lands in Standard with the General angle (probe: visit plus pricing page scores 45).

**The honest estimate.** Interest per lead is a product of factors (§2). The fixes above are real, mostly cheap, and use tools already paid for. Stacked with overlap discounted and the haircut applied to the lift, the evidence-weighted mid case for the full programme is about **1.5x** on positive replies, qualified referrals and demo requests per account (conservative 1.13x, optimistic 1.98x). **2x is the optimistic case**, not the mid: it needs the soft ask to lift positives by about a third, the right-person work and a second contact at 50–249 to land at the top of their ranges, the openers to reach most accounts, and today's inbox placement to be poor enough for plain text to matter (§6.3). Only the first of those will be read by 18 Dec, and only at a first, directional look. There is also no measured baseline: "2x" can only ever be an A/B result inside a period, never a before-and-after. The first draft of this review said 2.2x; §11 explains what changed.

**Top 5 moves.**

1. Confirm the opt-out end to end before anything sends: a seed-inbox test that Instantly's `{{unsubscribe}}` tag renders and works in both formats, Instantly-side unsubscribes pulled into the system's suppression list and HubSpot by the poll job, and a reply that says "stop" classified as `unsubscribe`.
2. Build `pick_contacts` properly before anything sends: wide senior-title lists, a seniority rank, the founder as the fallback at 50–249, the People leader as a fallback at 10–49, the person named by the signal preferred.
3. Redesign the ask ladder and run the soft question in email 1 as Test 1 from day one, both arms plain text and both keeping Harry's industry link, with three pre-registered looks.
4. Make email 1 land: plain text rendered as text (`render.py:273` plus the campaign flag), a 10→20→30 ramp, bounce kill rules in phase 2, and reply rate split by mailbox provider as a diagnostic.
5. Fix the scoring and ICP before the first sweep: no double count for a carrier EAP, Hiring wired to `apollo_jobs`, Q4 off, a size signal favoring 10–99, IT-services NAICS excluded, Legal Teams on from launch as a bet, and fix the `/us/book-demo` page that every CTA lands on.

---

## 2. How interest per lead is made

### 2.1 The metric

**Interest per lead** = (positive replies + qualified referrals + demo requests) ÷ accounts with step 1 delivered, measured 28 days after step 1. A referral counts only when it yields a contactable name and a follow-up is actually sent; otherwise the email-4 referral ask would manufacture the metric. Demo requests include bookings on the demo page with no reply, read back from HubSpot. The account is the unit, so a reply from either of two contacts counts once. Secondary, and what pays: demos held per 100 accounts.

`sql/views/00_v_account_outcomes.sql` computes positive-or-referral within 28 days per account. It needs demo requests added (from `hubspot_readback`, unbuilt), a "qualified referral" condition, and it should stop counting not-yet-classified replies as human (line 9): read closed, classified windows only. The leading metric for tests is the **non-negative reply rate** (every class except negative, unsubscribe and out-of-office), because a soft question raises negatives as well as positives and total reply hides that.

### 2.2 Baseline funnel (650 accounts a month, SPEC design competently executed)

The baseline assumes the SPEC's role rule is built as written, not today's stub. Reply is expressed per *inboxed* email so that inbox placement is a real factor, not decoration (the first draft's script had placement as a dead branch; corrected here).

| Stage | Conservative | Mid | Optimistic | Basis |
| :- | :- | :- | :- | :- |
| Step 1 delivered (not bounced) | 95% | 97% | 98% | Apollo verified only; Outreach average bounce 2.8% (03 §1.1) |
| Reaches the inbox | 70% | 80% | 88% | Validity 2025: Gmail 87%, Microsoft 76%, marketing mail; new domains, HTML, four to five links per email across four domains, no ramp push toward the low end (03 §3) |
| Human reply per inboxed | 4.3% | 5.0% | 5.7% | Chosen so that reply per delivered is 3 / 4 / 5%, SPEC 12's own assumption; Instantly average 3.4%, Hunter 4.5% (03 §1.1) |
| Human reply per delivered | 3.0% | 4.0% | 5.0% | |
| Positive share of replies | 30% | 33% | 40% | Saleshandy 34–50%; the EAP objection will be common (03 §1.1, §8) |
| **Positive per delivered** | **0.9%** | **1.3%** | **2.0%** | |
| Demo booked per positive | 50% | 55% | 60% | Inference |
| Held per booked | 75% | 80% | 85% | Spill: 76% of closed qualified deals reached a demo (05 §4) |
| **Per month: positives / booked / held** | 5.6 / 2.8 / 2.1 | 8.3 / 4.6 / 3.7 | 12.7 / 7.6 / 6.5 | |

The inbox row bounds the deliverability lever: at 80% placement the most any inbox work can add is 1.25x, and realistically 80→88% is 1.10x. Belkins' 0.45% is per *email*, not per lead; over four emails it is about 1.8% per lead, which is why it is not the floor.

**2x would mean** about 2.6% positive-or-demo per delivered account: roughly 17 positives, 9 bookings and 7 held demos a month at today's capacity. At Spill's historic 15–20% close on outbound-sourced meetings (05 §9 item 14), that is about one customer a month; at the mid case of 1.5x it is about 12 positives, 6–7 bookings and 5 held demos, so three customers a quarter. The point of v1 is a measured baseline and a repeatable motion; the economics only change with more mailboxes (§6.4).

### 2.3 The factor decomposition

Interest per lead ≈ right account × right moment × right person × reaches the inbox × relevant message × easy ask × follow-through, with a second contact acting on "right person" at the account level and the demo page acting on "demo request".

| Factor | What sets it today | Where it leaks |
| :- | :- | :- |
| Right account and moment | Industries, States, size bands, hard exclusions, Hold list, Signals and `counts_for_days` | IT services inside "tech"; a carrier EAP scored +35 before Q4; complements held like competitors; 100–249 ranked above 10–19; Legal off until January; no decay; Q4 +10 for all; Hiring never fires on `apollo_jobs` |
| Right person | Roles tab and `pick_contacts` | Not built; titles miss most buyers; no seniority; one contact |
| Reaches the inbox | Mailbox registry, Instantly settings, copy rules, the signature | HTML with four to five links on every email, two of them to non-Spill domains; no ramp; 90 a day on one domain; no kill rules; catch-alls enrolled while Instantly drops risky contacts |
| Relevant message | Industry copy, role line, opener | Opener is a stock sentence or absent; no proof; email 2 is a brochure; the Article 14 block is the most memorable line in email 1 |
| Easy ask | CTA in each step, the signature's booking link | None in email 1's copy (the signature's "Book a call here" is in every email and is not an ask); same page link in 2–4; no question; no referral path |
| Follow-through | Reply desk (unbuilt), demo page | Harry alone approves; repost window ends 4 pm ET; no objection or referral drafts; no not-now path; `/us/book-demo` titled "The UK's Highest Rated EAP" |

Volume is a separate axis. All levers in §6 move interest *per account*; only the second contact trades volume for it, and only the mailbox count raises volume.

---

## 3. Design and build evaluation

### 3.1 Strengths

- One owner per job and one record (`docs/pipeline.md` "One owner per job"); vendors return observations, Python decides.
- The guard (`clients/guard.py`) and `tests/test_guardrails.py`: every write is allowlisted by container.
- Settings versioned with `effective_from/to`; invalid tabs keep yesterday's version.
- Render-time copy rules (`enrol/copy_rules.py`) with a QA hash so edited copy cannot ship unchecked (`settings/model.py:268-290`).
- Budget pacing by day and the `limited_by` explanation (`limits.py`, `budget.py`).
- The Control tier and `v_signal_value`: a holdout exists from the first send.
- The send forecast per sender (`enrol/capacity.py`), so no step ever waits for a full inbox.

### 3.2 Design flaws (would hold even if everything were built as specified)

1. **One contact per account with `stop_for_company` on** (SPEC 2; `clients/instantly.py:43`): a single point of failure per account, after the Clay spend.
2. **The role rule contradicts Spill's own data** (05 §3.5): HR Manager as a first choice, Operations before the founder at 50–249, no People leader fallback at 10–49.
3. **Scoring is static fit with binary freshness** (`scoring/score.py:119-128`): a funding round counts the same on day 1 and day 539; Q4 adds 10 to everyone.
4. **EAP is scored as a positive twice** (`defaults.py:156-168`): "EAP" and "employee assistance" are in both "Mental health support listed" (+25) and "EAP named" (+10). Probe: a carrier EAP alone scores 35, plus 10 for Q4, so 45 and Standard; a values page makes it Priority. Spill's data: EAP-in-place deals won 32% against 46% with nothing (05 §3.6). That is a post-demo rate, and a company with an EAP has a budget line and a benefits owner, so the EAP is kept as a modest positive and the angle trigger, not a negative.
5. **The ask ladder is flat**: no ask in email 1's copy, the same demo page three times, and since 1 Oct a "Book a call here" link in every signature, which does not change the ladder (02 §2).
6. **Email 1 carries the whole Article 14 notice**, in HTML, against SPEC 10's own "plain text" design. The footer is gone: every email ends with the three-line signature (Spill, "Book a call here", Trustpilot; commit `7bef045`), then on email 1 the notice in small type, then Instantly's unsubscribe link (commit `8797784`), which works without any code of ours; a reply that says "stop" still needs `poll_replies` to classify it, and Instantly's unsubscribes still need pulling into the system's suppression list.
7. **Site visits lead nowhere**: a visit after email 1 only reaches the daily post (SPEC 7 "not alerted"); a visitor's angle is General, so no opener.
8. **The first test (`t1`) tests one industry row's opener** and names versions that no longer exist (`defaults.py:360-377`; open-questions #74). Structural tests across all rows are not possible in `choose_copy` (`enrol.py:331-351`); `read_test` has no interval; and because `text_only` is a campaign-level Instantly setting with one campaign per sender (`instantly.py:54-59`; `registry/mailboxes.py:113-115`), a plain-text-versus-HTML test by hash split cannot be run at all.
9. **The learning loop is biased**: `rescore` deletes and rewrites every `signal_matched` row (`score.py:536-539`), so a 90-day signal vanishes from accounts already sent; the Control baseline is "tier is Control now" (`02_v_signal_value.sql:67`), which shifts when Q4 points fall away in January.
10. **Cadence was set by capacity** (`pipeline.md:224-231`): days 0/7/14/21 solve the weekend pile-up, not conversion. Acceptable; but each step is a new thread with its own subject (`registry/mailboxes.py:59-61`), so "following my note" has no note under it.
11. **No referral path.** `Instantly.reply` answers only in-thread (`instantly.py:709-720`); a referred person is a new data subject who needs a notice, the CA/WA and personal-domain checks and a fresh send; nothing in the design does that (§4.3 U8).

### 3.3 Build gaps by pipeline stage

| Stage | Built | Spec-only | Missing or wrong |
| :- | :- | :- | :- |
| Universe | `accounts.admit` (`accounts.py:37-62`); Named accounts (`sources/named.py`); Apollo `search_organizations` | `source_universe` slicing; IRS BMF; GPTW/B Corp | `admit` does not dedupe against HubSpot (pipeline.md:76 says it does); `follow_redirect` has no caller; job disabled (`schedule.py:36`) |
| Enrichment and resolver | Apollo enrich, Clay run/poll/parse clients | The "Which value wins" resolver (pipeline.md:354-372); `verify_in_clay` | Nothing writes `hq_state`, `employees`, `size_band` to `accounts`; a real account today would be Excluded as "HQ state unknown" (`tiers.py:201-203`) |
| Signals | Matcher, freshness, condition parser; `named` writes facts | `apollo_people`, `apollo_jobs`, `site_visits`, `job_posts`, `irs_bmf`, `layoffs` | `Hiring and growth` reads `apollo_org` only (`defaults.py:206`); tracker has `data_received: false` |
| Score, tier, angle | `score_account`, `tier`, `choose_angle`, `rescore` | | No decay; EAP double count; Q4 flat; Hold list mixes competitors and complements; two hard exclusions mis-sized (`tiers.py:220-229`) |
| Contact | `contact_block`, `pick_contact`, title→role mapper | `pick_contacts`, Apollo bulk match, Clay fallback, role rule | `pick_contacts` absent (`cli.py:79`); `map_title_to_role`, `first_choice_for_size`, `fallback_order` have no callers; no seniority; no `verified_at` on contacts |
| Queue and enrol | `order_key`, control share, focus quotas, `enrol.run`, capacity forecast | | Hand-check item is never created (`enrol.py:141-142` would always skip); `catch_all_valid` sendable (`enrol.py:57`) while `allow_risky_contacts` is false (`instantly.py:48`); size order 20–99 > 100–249 > 10–19 (`queue.py:40`) |
| Render and copy | Variables, markup, signature, Article 14, rules, QA desk | | All 106 rows draft; no signal opener; `{{proof}}`, `{{place}}` unused; `proof_point` blank on 108 rows; `render.py:273` picks HTML or text for all steps at once |
| Sending | Campaign create/drift, mailbox health, `add_leads`, Instantly's unsubscribe link in every step template (`registry/mailboxes.py:61-66`) | `first_email_text_only`; threading; variable limit (`instantly.py:89` is `None`); the `{{unsubscribe}}` tag syntax (PHASE0-CONFIRM, `instantly.py:57-59`) | No ramp (`mailboxes.py:55-56`); promotion at 21 days whatever the score (`mailboxes.py:100-106`); Active mailboxes never demoted |
| Replies and approvals | `list_emails`, `reply`, `forward`, Claude JSON client with cap | Classification schema, routing, Slack alert, approvals, escalation | `replies/` empty; `templates/prompts/` empty; no objection, referral or not-now play; Instantly's unsubscribes not yet pulled into suppression or HubSpot |
| HubSpot | Six properties, write primitives, pre-send re-check, suppression load | Reply-triggered writes (`crm/hubspot_writes.py:168-171` is a comment); readback; deal on demo | `hubspot_active_sequence` and `hubspot_other_activity_90d` are read (`tiers.py:93,96`) but never written |
| Measurement | `v_account_outcomes`, `v_readout_weekly`, `v_signal_value`, `v_mailbox_health`, `test start/read` | UTMs (SPEC 2 row 11); `sync_outcomes`; `daily_post`; `monday_readout` | No cut by step, role, sender, provider or opener; no interval on `read_test`; `learn/` empty |
| Kill rules | `v_mailbox_health`, operator `stop/start` | All eight rules (SPEC 12) | Phase 3; nothing feeds them until `sync_outcomes` exists |

---

## 4. Gaps, ranked

### 4.1 Blockers: nothing may send until these are done

| # | Blocker | Evidence | Fix | Effort |
| :- | :- | :- | :- | :- |
| B1 | **Opt-out confirmation.** The opt-out is Instantly's own unsubscribe link, appended to every step template after `{{sN_body}}` (`clients/instantly.py:59-67`; `registry/mailboxes.py:61-66`), with the List-Unsubscribe header already on. A click stops the sequence and adds the address to Instantly's workspace unsubscribe list. The mechanism exists; it is not yet confirmed, and not yet connected to the system's own records | `instantly.py:57-58` (PHASE0-CONFIRM the tag); SPEC 11 (unsubscribes into suppression and HubSpot); SPEC 13 (same-day honoring) | (a) a seed-inbox test send in both formats confirms the `{{unsubscribe}}` tag becomes a working link; (b) the poll job records Instantly-side unsubscribes into suppression and sets HubSpot opt-out where the contact exists, so a later re-contact cannot email them; (c) `poll_replies` classifies a reply that says "stop" as `unsubscribe` and does the same. (b) and (c) are part of the phase-2 reply-polling build | S–M |
| B2 | The source modules, resolver and `verify_in_clay` do not exist | `cli.py:73-77`; `schedule.py:36-40` | Phase 1 build (§7) | L |
| B3 | `pick_contacts` does not exist | `cli.py:79`; `contacts/__init__.py` empty | Build in phase 1, not phase 2: the hand-check needs a named contact and title | M |
| B4 | The weekly hand-check item is never created, so `enrol` always skips | `enrol.py:133-151`; no writer of `kind="hand_check"` | A `hand_check post` job on Mondays and a `handled` path; 5 accounts per group, not 10, to fit Harry's hours (§7.5) | S |
| B5 | No copy row is approved | `copy.csv` status all `draft` | Harry approves the four live rows after the launch-fix pass | S (Harry) |
| B6 | Instantly plan facts, key, variable limit, same-address follow-ups, which part Instantly sends under `first_email_text_only`, threading on a blank subject, the unsubscribe tag | `instantly.py:33,49,57-59,89`; open-questions #58 | One phase-0 session against a paused campaign, with a seed-inbox send | S |
| B7 | Clay "US Outbound" functions do not exist; REST path unconfirmed | `phase0-facts.md` Clay rows | Builder prepares the configuration; Harry creates them in the UI; 20-account trial | M |
| B8 | Apollo tracker: `us/pricing` slash, `/us/book-demo` missing, no data received | `phase0-facts.md` Apollo row | Harry, ten minutes | S |
| B9 | Railway, Slack app, HubSpot service key, Sheets service account | `docs/build-plan.md` setup steps | Runbook | M |

Harry decided on 1 Oct (commit `8797784`) that the emails carry no postal address and no privacy link, and `postal_address` and `privacy_url` are retired keys that no longer block sends. CAN-SPAM's postal-address requirement and Article 14's fuller information list are his call; they are noted here once and not listed as blockers.

### 4.2 Launch-depressors: shipped as designed, these would cut interest per lead

| # | Depressor | Evidence | Fix | Effort |
| :- | :- | :- | :- | :- |
| D1 | **Contact choice.** Titles miss the buyers; HR Manager is a first choice; Ops before founder at 50–249; no People leader at 10–49; no seniority; not tied to the signal; one contact | Probe: the eight titles map to `None`, "HR Manager" to People leader. 05 §3.5: founder 55%, senior HR 42% (50% at 50+), HR Manager 23%, Ops 35% (25% at 50+) | Appendix A roles; `pick_contacts` with seniority rank and signal preference; second contact at 50–249 in January. The title gap is a bug fix to the spec'd design; only seniority, the fallback order and the second contact are lifts over it | M |
| D2 | **No reply-inviting ask.** Email 1's copy asks nothing (the signature's booking link is an exit, not an ask); 2–4 repeat a page link; no email-1 question in any row; no referral ask; the one-pager was dropped | 02 §2; open-questions #38–39; Gong 304k: interest CTA 30% vs 15% meetings among emails that had a CTA; offer +28%, meeting ask −44% (03 §2.2). No study compares "no ask" with "interest ask" directly | Soft question in email 1 as Test 1; objection question in 3; referral ask in 4, counted only when it yields a sent follow-up (Appendix B) | S code, Harry decision |
| D3 | **Email 1 as delivered.** HTML; 183 words, 68 of them the notice (37%) and 19 the signature; five links across four domains (industry page, Spill, Harry's booking link, Trustpilot, Instantly's unsubscribe), two to non-Spill domains; the notice names the data brokers in the body | `defaults.py:144`; `render.py:253-263`; `article14.txt`; `signature.txt` | Step 1 rendered as text (`render.py:273`) and `first_email_text_only`; provider split as a diagnostic. The signature and notice are Harry's (1 Oct); the step-1 link count is his decision or test (§9 D19), and an optional tone edit to the notice is in §9 D3 | S |
| D4 | **No ramp, one domain, no demotion, no kill rules until phase 3** | `mailboxes.py:52-56,94-107`; `schedule.py:50` | Ramp 10→20→30 over three weeks Active; promote on score ≥ 90 *and* ≥ 14 days; bounce rules in phase 2. A third domain is a paid service (SPEC 1.1) and would not be warm before January | S–M |
| D5 | **Evidence never reaches the email.** No signal opener; General/Control get none; `{{proof}}` unused; `proof_point` blank; site visitors get no opener | `defaults.py:392`; `enrol.py:364-366`; scan: `{{proof}}` 0, `{{place}}` 0, `{{company}}` in 0 email-1 bodies | Openers on the Signals tab (Appendix A): one observed fact, no inference; `proof_point` only from verified-active US customers with permission; Claude opener in January with a holdout | S then M |
| D6 | **Scoring wiring.** EAP double count; Hiring reads `apollo_org` only; Q4 +10 flat; values page +10; funding flat 540 days; "First People hire" needs a count that may be missing | Probe: carrier EAP = 35 (+10 Q4); `open_roles=5` from `apollo_jobs` scores 0; no facts in October = 10 | Appendix A weights; all sheet edits except decay, which two rows achieve without code | S |
| D7 | **Hard exclusions mis-sized.** "Founded <2 years" removes funded seed teams; "<5 US people" relies on Apollo's thin coverage of 10–19s; every check fails open on a missing fact | `tiers.py:220-229` | Age rule to 1 year or removed; the 5-people rule only when `employees >= 50` | S |
| D8 | **Industry definition.** 15 of 16 tech labels share NAICS incl. 5415 (IT services, MSPs, IT staffing); Digital health and Healthtech let teletherapy competitors in; Legal is off until January | `industries.csv`; `tiers.py:56-84`; 05 §3.2 (Law 57%, n = 14, CI 33–79%), §5 (10 of 52 historic US customers, 2020–22, churn unknown) | `exclude_naics` 541512; 541513; 541519 on tech rows; add "teletherapy; online therapy; virtual therapy; mental health platform" to the behavioral-health partner keywords; Legal Teams on as a bet, priority 2, Focus 20% | S |
| D9 | **Size.** Queue ranks 100–249 above 10–19; one price "from $195 for the whole team" to 200-person firms | `queue.py:40`; `render.py:49`; SPEC 4 prices $995 at 101–200; 05 §3.1 | Size signal (+15 at 10–49, +10 at 50–99); rank 10–19 with 20–49. Size-banded price is a January item: price is 9% of lost reasons and the ICP shift makes the flat line right for most accounts | S |
| D10 | **Hold list treats complements as competitors**; no exit from Held | `defaults.py:170-176` | Split the row: direct competitors Hold; Calm, Headspace, Wellhub, Gympass +5 with Progressive employer | S |
| D11 | **Catch-alls** are enrolled and may never send | `enrol.py:57`; `instantly.py:48`; pipeline.md change 8 | Exclude catch-alls at launch; filter Apollo people search to `contact_email_status = verified`; measure what is lost | S |
| D12 | **Reply desk design leaks interest** once built: Harry alone approves; repost only 13:00–21:00 UK; no objection or referral drafts; `contact_block` refuses any re-contact; OOO keeps sending; reply drafts cannot offer a trial because "free trial" is a spam phrase | SPEC 11; `enrol.py:217-218`; `instantly.py:44`; `copy_rules.py:300`; trials won 63% (05 §4) | Drafts for every class; repost window to 23:00 UK; a second approver (§9 D11); "a 30-day paid pilot" wording in drafts; recontact rule read from settings | M |
| D13 | **HubSpot check after the Clay spend**; two exclusions never written | `enrol.py:376-399`; `tiers.py:93,96`; `accounts.py:37-62` | HubSpot company domains pulled incrementally by `hs_lastmodifieddate` windows (the search API caps at 10,000 results per query, so not one daily search), checked in `admit` and before `verify_in_clay` | S–M |
| D14 | **Measurement cannot see the levers.** No UTMs, no step, role, sender, provider or opener cut; `read_test` has no interval; the test covers one industry row | SPEC 2 row 11; `cli.py:471-529`; `enrol.py:346` | §8 | M |
| D15 | **The demo page.** Every email 2–4 CTA and page-based demo request lands on `/us/book-demo`, titled "The UK's Highest Rated EAP", with price text that differs from the emails (the signature's "Book a call here" goes straight to Harry's meeting link and bypasses the page) | `phase0-facts.md:80`; open-questions #67–68 | Harry, 30 minutes: US title, US price text consistent with `price_from`, one social-proof figure (50,000 or 30,000, not both), slots inside 48 hours on the HubSpot link | S (Harry) |

### 4.3 Upside levers (beyond fixing the above)

| # | Lever | Evidence | Effort |
| :- | :- | :- | :- |
| U1 | Same-day action on `/us` visits: Apollo visitor filters daily (already allowed by the guard, `guard.py:44-58`, not yet in `apollo.py`), Slack alert for enrolled accounts on pricing or demo pages, next step pulled forward via a subsequence. Company-level only; the tracker fires for CA *visitors* too, and California's CIPA suits over such pixels are a known risk (03 §4) that Harry has accepted | 04 §7.1 | M |
| U2 | Named US proof from HubSpot's 59 US customer companies, verified active, with permission; never a churned one (36 of 52 historic US customers have churned, 05 §5) | SPEC 10 | S + Harry |
| U3 | Claude per-account opener from stored facts, nightly via the Batch API, Sonnet or Haiku, about $1–3 a month, 30% holdout; one observed fact, no inference | 04 §5; Hunter +56%; Saleshandy hybrid (03 §5) | M (January) |
| U4 | Dated triggers from free Apollo lookups: People role posted in 30–60 days; growth; Slack or Teams in use; **PEO in use (Justworks, TriNet, Insperity, ADP TotalSource) in phase 1, not 3**, since a PEO bundles an EAP and 15% of 10–499 employers use one (03 §8) | 04 §1 | M |
| U5 | Renewal month and broker asked in every positive and not-now reply: a zero-cost timing signal; a broker-referral reply is logged, not chased, in v1 (79% of employers buy through a broker, 03 §8) | 03 §8 | S |
| U6 | Form 5500 plan-year start and Schedule A carriers, free | 04 §6 | M (January) |
| U7 | Second contact at 50–249 funded by lapsing Apollo credits (about 3,800 above the floor by Aug 2027) | Hunter 25k campaigns, 3.35% vs 1.61% account-level, confounded by account size and coverage (03 §4) | M (January) |
| U8 | Referral mechanics: the approved reply to the referrer asks them to loop the person in (their introduction is the best path); if an address is given, a new contact row (`source = referral`) passes `contact_block`, gets its own notice naming the referrer as the source, and is added as a new lead with a referral-specific step 1 to the same sender's campaign. PHASE0-CONFIRM whether `stop_for_company` blocks a new lead at a stopped domain; if so, a "US Outbound – {owner} / referrals" campaign under the guard prefix | §3.2 item 11 | M |
| U9 | A Harry-signed email to 10–49 founders, tested by hash assignment of 10–49 accounts to Harry versus Hannah or Sam (free, randomized, and Harry already holds half the capacity) | 03 lever 12 (hypothesis) | S |
| U10 | Three steps instead of four as the cheaper alternative to a second contact: frees 25% of capacity (200 accounts a week) at the cost of step-4 replies | 02 §7 item 9; 03 §1.3 | S (test, January) |
| U11 | Former Spill buyer now at a US company. **A legal question, not a lever**: matching UK customer contacts to new employers re-purposes personal data (UK GDPR Art 5(1)(b)) and discloses names to Apollo and Clay. Built only after an LIA and DPIA, only for buyer or admin contacts, never end users, stored on the account ("a prior buyer works here"), never on a person | 05 §3.4 (60% win) | Gated (§9 D17) |

Not recommended: LinkedIn automation (against LinkedIn's terms; any touches manual and optional); Instantly A/Z auto-optimize, AI Reply Agent and Website Visitors pixel; HubSpot buyer intent and Breeze; Claude web search at scale.

---

## 5. Under-used tools

### 5.1 Apollo (30,188 lead credits; 1,200 visitor credits; waterfall enabled)

Idle: visitor filters on company search, free organization lookup with dated job and growth filters, technographics (Slack, Teams, Justworks, TriNet, Gusto, Rippling), `contact_email_status` on people search, polled waterfall via `webhook_result_show`, job postings. About 8,800 credits lapse on 21 Aug 2027 at the 2,000-a-month pace (`pipeline.md:150-158`).

Concrete use: `sources/site_visits.py` with one daily visitor-filter call; `sources/apollo_jobs.py` tag sweeps for "People role posted ≤ 60 days" and PEO or HRIS in use; `contacts/pick.py` filtered to verified emails; the lapsing excess on second-contact reveals in January; polled waterfall for catch-alls if phase 0 confirms the path. The 3M AI credits need a read-only exception (they write collections); leave them.

### 5.2 Clay (2,000 credits a month; four existing functions)

Idle: Website Technology Stack and Website Traffic; richer fields from the same Claygent run. Concrete use: extend the Accounts output and `parse_accounts_output` (`clients/clay.py:295`) with `peo_or_broker`, `collab_tool`, `plan_year_month`, `awards`, `hiring_states`, `careers_platform`; run Tech Stack only where Apollo has no technographics; measure credits per account on the first 100, because 2,000 a month supports about 440 accounts at the 4.5-credit example, not 650 (01 §5). Spend Clay only after the free HubSpot and contact checks. Clay's Job Change signal on champions is part of U11 and gated with it.

### 5.3 Instantly

Now used: the built-in unsubscribe link in every step template, with the List-Unsubscribe header (commit `8797784`; the drift check carries it into existing campaigns). Idle: `first_email_text_only` (campaign field), subsequences, `update-interest-status`, AI reply labels, provider matching. `text_only` and `first_email_text_only` are campaign-level, and there is one campaign per sender, so neither can be split by hash; a format is chosen for everyone. `first_email_text_only` also needs `render_step` to emit the `text` rendering for step 1 (`render.py:273` chooses one format for all steps), or the variable holds HTML that Instantly may send as tags; phase 0 confirms which part Instantly uses. Not recommended: A/Z auto-optimize, AI Reply Agent, the Website Visitors pixel. Inbox placement tests are a paid add-on ($47–97 a month): Harry's call. Each new write needs a guard entry scoped to the `US Outbound –` prefix (`guard.py:247-276`).

### 5.4 HubSpot (713 customers, 59 US; 3,858 closed-lost deals)

Idle as an *input*. Concrete use: a read-only `crm/customers.py` listing US customers by group with `subscription_status` verified, proposing `proof_point` rows Harry approves by name; closed-lost reasons feed the objection drafts; `hs_analytics_*` page views on a warm lead go into the Slack alert. Buyer intent and Breeze stay off.

### 5.5 Claude ($10 a month cap; Opus writes, Sonnet checks)

Idle: Batch (50% off), prompt caching, `effort`, Haiku. Concrete use: `batch_json()` in `clients/claude.py`; cache `style.md` + `facts.md`; `output_config.effort: "low"` for classification and QA (thinking is billed as output); Sonnet for the January opener at about $2.70 a month batched (04 §5). No cap change needed.

---

## 6. The plan, and what it is worth

### 6.1 Levers

Multipliers are on interest per account as defined in §2.1, for the full programme once everything is live (February onward). The lever-by-lever values are judgments anchored on the cited evidence, not measurements.

| Lever | Factor | Low | Mid | High | Confidence | Evidence | Cost / credits | Guardrail | Effort |
| :- | :- | :- | :- | :- | :- | :- | :- | :- | :- |
| A+B. Right account and moment (one lever: IT services out; teletherapy to partners; EAP de-weighted; complements un-held; size signal; funding decay; Hiring wired; Q4 off; People-role trigger; site visits to Priority; Legal on) | Right account and moment | 1.02 | 1.10 | 1.22 | Medium-low: Spill's evidence is post-demo and small; narrowing NAICS is the only part that changes the universe; the rest re-orders a queue deeper than capacity | 05 §3.1–3.2, §3.6; 03 §2.1, §2.3; probe | 0–50 Apollo | None | S–M |
| C. `pick_contacts` over the SPEC role rule: seniority rank, founder fallback at 50–249, People leader at 10–49, signal-named person first (the title gap itself is a bug fix, not a lift) | Right person | 1.05 | 1.15 | 1.30 | Medium on direction; moves demos held more than this metric | Probe; 05 §3.5; Belkins founders reply most (03 §1.2) | 0 | None | M |
| D. Second contact at 50–249 (January) | Right person, account level | 1.00 | 1.10 | 1.20 | Medium-low: Hunter's 2.1x is confounded; about 40% of accounts | 03 §4 | +1 Apollo credit per account; volume 150→107 a week | SPEC 2 "one contact in v1": Harry | M |
| J. Harry-signed sends to 10–49 founders (randomized test) | Right person | 1.00 | 1.04 | 1.12 | Low (hypothesis) | 03 lever 12 | 0 | None | S |
| E. Reaches the inbox: step 1 as text; ramp; promotion on score and time; bounce rules in phase 2; catch-alls out. Bounded by placement headroom (80→88% is 1.10), and narrowed since 1 Oct: every email now carries four to five links across four domains through the signature, so a "few links" first email is not available without Harry's say (§9 D19) | Inbox | 1.03 | 1.07 | 1.12 | Medium on mechanism, weak on magnitude; the headroom is larger if placement is worse than 80% today | 03 §2.7, §3; §2.2 | 0 (a third domain is a paid service and a January item) | None | S–M |
| F. Relevant message: signal openers (reach perhaps half of accounts); Claude opener (January, 30% holdout); verified proof; email 2 tightened; subjects de-templated; the footer became a three-line signature on 1 Oct | Relevant message | 1.05 | 1.12 | 1.25 | Medium; mostly post-December | Hunter +56%/+18%; Woodpecker ~2x; Saleshandy hybrid (03 §2.4, §5) | Claude $1–3 | None | S then M |
| G. Easy ask: soft question in email 1 (Test 1); objection question in 3; qualified referral ask in 4 | Easy ask | 1.05 | 1.15 | 1.35 | Low-medium: no direct "no ask vs ask" evidence; a question also raises negatives | 03 §2.2 | 0 | Changes Harry's "no ask in email 1": test | S |
| H. Follow-through: drafts for every class; repost to 23:00 UK; second approver; not-now re-sequence; OOO re-time; pilot wording | Follow-through | 1.00 | 1.02 | 1.05 on this metric (1.2–1.5 on demos held) | Medium-high for demos held | 05 §4; HBR (03 §6) | Claude pennies | Approvers: Harry | M |
| I. Demo page fixed (title, US price text, one proof figure, 48-hour slots) | Demo request, booked, held | 1.01 | 1.03 | 1.08 | High that it costs nothing; small on this metric, large on booked | `phase0-facts.md:80` | 0 | None | S (Harry) |

### 6.2 Stacked estimate

Method (`stack_v2.py`): levers in the same funnel stage act on the same emails and accounts, so within a stage the combined multiplier is 1 + Σ(m − 1) × (1 − overlap); stages are multiplied; the execution haircut is applied to the *lift*, 1 + (product − 1) × (1 − haircut). The first draft applied the haircut to the multiplier itself, which was wrong (§11).

| Case | Who (A+B, C, D, J) | Seen (E, F) | Act (G, H, I) | Product | Haircut on lift | **Interest per lead** |
| :- | :- | :- | :- | :- | :- | :- |
| Conservative (low values, 30% overlap) | 1.05 | 1.06 | 1.04 | 1.15 | 15% | **1.13x** |
| Mid (mid values, 40% overlap) | 1.23 | 1.11 | 1.12 | 1.54 | 15% | **1.46x** |
| Optimistic (high values, 50% overlap) | 1.42 | 1.19 | 1.24 | 2.09 | 10% | **1.98x** |

Sensitivity of the mid case to the overlap discount: 1.55x at 30%, 1.46x at 40%, 1.37x at 50%.

**The December window** (what is live by 18 Dec: no second contact, no Harry-signed test, openers on about half of accounts): conservative 1.11x, mid 1.32x, optimistic 1.65x. And December is the worst reply month of the year (Belkins: December 0.35% against February 0.54% per email) and US open-enrollment season, so the December read will sit below the programme's run rate; re-read in February.

### 6.3 What a credible 2x requires

Holding every other lever at its mid value, 2x needs these at the top of their ranges, all at once: the ask (G at 1.35 on positives, not on total reply), the person (C at 1.30), the second contact (D at 1.20), the message (F at 1.25) and the inbox (E at 1.12, which implies placement is poor today). Mid values with only G at high give 1.60x; G and C high give 1.70x; adding D gives 1.78x; adding F gives 1.89x; E at its narrower top (1.12) adds almost nothing at mid overlap (still 1.89x, `stack_v2.py`); only the optimistic case's lighter overlap and haircut reach 1.98x. So **2x is plausible only under stated conditions**: a soft ask that genuinely lifts positives by a third, a working second contact at 50–249, openers that reach most accounts, and today's placement being poor. Only the first is measured by 18 Dec, and only at a first look (§8).

Levers that close the gap beyond the first draft's set, with the evidence for each: the demo page (I: free, acts on every CTA; `phase0-facts.md:80`); the renewal-month ask and PEO detection (U4, U5: zero-cost timing data from the first reply onward; 03 §8); narrowing to the high-win cells (10–49 founders in Tech, Agencies and Legal via the Focus tab and the size signal; 05 §3.1, §3.5: 44–55% post-demo wins against 11–24% at 100–249); the Harry-signed test (J); three steps as the cheap alternative to a second contact (U10); and the paid pilot in reply drafts (trials won 63%, 05 §4), which acts on won deals rather than on interest.

**Minimum viable set.** The smallest set that captures most of the plausible gain, fits Harry's hours, and leaves Test 1 readable:
1. Sheet, this week, about one hour of Harry's time: Roles rewrite (A.1); remove "EAP; employee assistance" from "Mental health support listed"; Hiring source `apollo_jobs, apollo_org`; Q4 inactive; the size signal (once phase 1 confirms `apollo_org` writes `employees` as a fact); `exclude_naics` 541512/3/9 on tech rows; Legal Teams on at priority 2 with Focus 20%. Skip the funding split, PEO, Slack/Teams and Form 5500 rows for now.
2. Code, phase 1: `pick_contacts` with the role rule, seniority rank and verified-email filter; the `hand_check post` job; ramp caps and promotion on score *and* time; step 1 rendered as text; the HubSpot domain check before Clay.
3. Opt-out confirmation before the first send (B1): the seed-inbox test and the sync of Instantly's unsubscribes into suppression and HubSpot.
4. Copy: the launch-fix pass on the four live rows only (Tech, Agencies, Legal, General), not 106; the objection question in email 3 and the qualified referral ask in email 4 as drafted in Appendix B.
5. Test 1 from day one: the soft question alone, both arms plain text, both keeping the industry link, three pre-registered looks on non-negative reply rate (§8).
6. Reply desk minimum: classification (including the `unsubscribe` class), Slack alert with draft, approvals, the Instantly unsubscribe sync, drafts for positive, referral, "we have an EAP" and not-now; demo slots inside 48 hours.
7. Harry, 30 minutes: the demo page (D15).

Defer to January or later: second contact, Claude opener, subsequences and visit pull-forward, UTMs, interest-status write-back, size-banded price, third domain, HubSpot customers module, Form 5500, former-buyer signal (gated), allocation shifting in tests.

### 6.4 Volume and unit economics

All levers except D leave weekly volume at 150. D cuts it to about 107 a week at four steps (120 sends a day ÷ 5.6 sends per account) or 143 at three steps; multi-threading raises interest per account but, at fixed capacity, roughly holds total interest flat. Adding two mailboxes on a third domain restores volume; it is a paid service (a domain plus two Workspace seats, roughly $15–20 a month) and needs 21 days of warmup, so it is a January decision (§9 D5).

Monthly cost of v1 at today's capacity: 2,000 Clay credits, 2,000 Apollo credits from a pool already paid for, four mailboxes already in place, Claude under $10, Railway about $5, and about five hours of Harry's time (§7.5). Output at the mid case: about 12 positives, 6–7 bookings, 5 held demos and roughly one customer a month at Spill's 15–20% outbound close, at a median US won amount near $277 a month (05 §5). The programme does not pay for itself at four mailboxes; v1 buys a measured baseline and a repeatable motion, and the scale decision in January should be made on the measured numbers, not on this estimate.

---

## 7. Roadmap to 18 Dec

Today is Thu 1 Oct. Blackouts: 23–27 Nov and 18 Dec–4 Jan. About seven send weeks from 26 Oct; roughly 700–1,000 accounts enrolled by 18 Dec and about 500 with closed 28-day windows.

### 7.1 Phase 0, to Fri 9 Oct, spilling into the following week where it must

Must be true before phase 1 starts:
- Harry's decisions D1–D7 (§9) taken this week; the sheet edited (minimum viable set item 1). The remaining decisions wait for phase 2.
- Signature and Article 14 text as Harry set them on 1 Oct (commits `8797784`, `7bef045`); a seed-inbox test send in both formats confirms the `{{unsubscribe}}` tag (B1a) and shows how the signature's links render as text.
- Instantly session on a paused campaign (B6); set `CUSTOM_VARIABLE_LIMIT`; record in `phase0-facts.md`.
- Clay functions created from the builder's configuration; 20-account trial; credits per account recorded.
- Apollo tracker fixed; Railway, Slack, HubSpot key, Sheets account done.
- Code (S): `first_email_text_only` in `CAMPAIGN_SETTINGS` and drift; `render_step` emits text for step 1 when the flag is on; `hand_check post` job and CLI; `_norm_link` to ignore query strings (for UTMs later); `apollo.py` `website_visitors.search` action; `contact_email_status` filter on people search.

### 7.2 Phase 1, Mon 12 – Fri 23 Oct: universe, contacts, scoring

- `sources/apollo_org.py` (sweep by group, a quarter of slices a week), `apollo_people.py`, `apollo_jobs.py` (dated People-role postings; PEO technographic), `site_visits.py`, `public_signals` (job posts as a source for the EAP and mental-health signals), the resolver (`pipeline.md:354-372`), `verify_in_clay` with the HubSpot check *before* the Clay call, `admit` HubSpot dedupe by incremental windows.
- `contacts/pick.py`: role rule from the sheet, seniority rank, signal-named person first, MX provider recorded on the contact, bulk match verified-only, Clay Contacts on misses; second-contact candidates stored at 50–249 but not enrolled.
- Scoring: Appendix A edits; confirm `employees` arrives as an `apollo_org` fact so the size signal matches; `tiers.py` age rule and 5-people rule; freeze `signal_matched` rows for enrolled accounts (skip `ANGLE_FIXED_STATUSES` in the delete at `score.py:536-537`).
- Mailboxes: ramp caps in `registry/mailboxes.py`; promotion on score ≥ 90 and ≥ 14 days.
- Hand-check on Mon 19 Oct: 5 accounts per group; clean name, HQ, size, role title and seniority, opener evidence ≥ 90% right.
- Measure: universe per slice, Clay credits per account, Apollo hit rate, share lost to catch-alls.

### 7.3 Phase 2, Mon 26 Oct – Fri 13 Nov: first sends

- Copy: the launch-fix pass on the four live rows (Appendix B pattern). Harry approves; QA re-stamps.
- Test 1 starts on day one (§8).
- `poll_replies` (Sonnet, effort low, SPEC 11 schema; the `unsubscribe` class and a "stop" reply → suppression, blocklist and HubSpot opt-out; drafts for positive, referral, objection and not-now; renewal month and broker asked in positive and not-now drafts), `poll_approvals`, Slack alert, HubSpot writes, `hubspot_readback` with `meeting_booked` and demo requests into the outcome view, `sync_outcomes` pulling Instantly's unsubscribe list into suppression and HubSpot (B1b), `update-interest-status` write-back.
- Kill rules brought forward: domain bounce > 3% on 100 sends; 5.7.x block; source bounce > 3%; an early stop check (non-negative reply < 2% after 400 delivered → pause for review).
- Referral path, minimum: the approved reply asks the referrer to make the introduction; the new-contact path (U8) follows in phase 3.

### 7.4 Phase 3, Mon 16 Nov – Thu 17 Dec: learning loop (blackout 23–27 Nov)

- Daily post and Monday readout with cuts by step, role, sender, provider (diagnostic) and opener present; Test 1's first look on 18 Dec.
- Referral new-contact path; not-now re-sequence on the stored date; OOO re-timing; remaining kill rules; `recontact_*` enforced.
- `proof_point` for the three live groups from verified-active customers with permission.
- Nothing else new in December. Second contact, Claude opener, visit pull-forward, Harry-signed test and Form 5500 start in January, when the first look has been read.

### 7.5 Harry's hours: the binding constraint

Harry has under three hours a week. The first draft's phase 0 asked for about 16 decisions, two text approvals, Clay builds, an Instantly session and four infrastructure tasks in six working days; that does not fit. Budget:

| Task | One-off (phase 0–2) | Steady state, per week |
| :- | :- | :- |
| Decisions D1–D7 this week; D8–D17 by phase 2 | 70 min + 90 min | |
| Signature and Article 14 text | decided 1 Oct | |
| Clay functions (builder prepares; Harry creates and runs the trial) | 60 min | |
| Instantly key and plan facts; Apollo tracker; demo page | 10 + 10 + 30 min | |
| Railway, Slack app, HubSpot key, Sheets account | 60 min | |
| Copy approval, four rows | 60 min | |
| Weekly hand-check (5 per group, 15 accounts, formatted Slack post) | | 15 min |
| Reply approvals (about 25 replies a month, 2 minutes each) | | 12 min |
| Monday readout | | 15 min |
| **Total** | **about 6.5 hours over two to three weeks** | **about 45 minutes** |

It fits only if phase 0 is allowed to spread into the week of 12 Oct, the hand-check is 5 per group, and the deferred decisions wait. Speed to lead is a separate problem from hours: a positive arriving after 4 pm ET waits overnight for Harry, which is why a second approver is a prerequisite for the follow-through lever rather than an option (§9 D11).

### 7.6 Go-live checklist (blockers in bold)

1. **Opt-out confirmed end to end: a seed-inbox test send in both formats shows Instantly's `{{unsubscribe}}` link renders and works; Instantly-side unsubscribes flow into the system's suppression list and HubSpot through the poll job; a reply that says "stop" is classified as `unsubscribe` and does the same.**
2. Signature and Article 14 notice as Harry set them on 1 Oct are what render emits (commits `8797784` and `7bef045`; the drift check carries the unsubscribe line into existing campaigns).
3. **Copy rows for Technology & Startups, Marketing & Creative Agencies, Legal Teams and General approved and QA-current.**
4. **`pick_contacts` has run; the hand-check shows ≥ 90% right titles and seniority.**
5. Step 1 confirmed to send as text; variable limit known; follow-ups confirmed on the step-1 address.
6. Mailboxes Active on score *and* time; caps at the ramp value, not 30.
7. Bounce kill rules live; `sync_outcomes` feeding `v_mailbox_health`.
8. Test 1 registered with its look schedule and thresholds (§8); the variant renderer tested against empty and maximum values.
9. Demo page fixed (D15).
10. Harry's `[ASK HARRY]` sign-off, with `live_sending = yes`.

---

## 8. Learning and measurement plan

### 8.1 The sample-size problem, in numbers

Two-sided α = 0.05, 80% power (`stats_synth.py`). Non-control volume is about 128 accounts a week, 64 per arm in a two-arm test across all rows; once a second contact is live it falls to about 45 per arm, which is one reason D waits until Test 1 has been read.

| Test on | Per arm | Weeks at 64 per arm per week |
| :- | :- | :- |
| Positive 1% → 2% | 2,318 | 36 |
| Positive 1% → 1.5% | 7,750 | 121 |
| Reply 3% → 6% | 748 | 12 |
| Reply 3% → 4.5% | 2,517 | 39 |
| Reply 4% → 6% | 1,863 | 29 |

Power of SPEC 12's design (400 per arm) to see a 2x lift: 21% at a 1% base, 38% at 2%, 53% at 3%, 77% at 5%. Positive-reply rate cannot pick winners at this volume; wording tests are undetectable; only structural swings across all rows, read on a leading metric, can be learned from before spring.

### 8.2 Design

1. **Metrics.** Primary (slow): §2.1. Leading for tests: non-negative reply rate per delivered. Health, weekly: bounce (under 2%), negative-or-unsubscribe rate (stop a variant above 1%), spam-rate warnings in Postmaster, reply by step and by mailbox, time to first human response, demos per positive, booked-to-held days (target under 3). Reply by provider (MX) is a *diagnostic*, not a test: Microsoft 365 recipients are not randomized and skew to larger, older firms, so a gap can be a company-mix effect.
2. **Test 1: the ask alone.** Arm A: Harry's email 1 (industry link, no ask in the copy). Arm B: the same email plus one closing question (Appendix B). Both arms plain text; both keep the link and the signature, so the signature's booking link is in both. Hash-split across all rows by a `kind = step1_variant` row on the Tests tab; version B appends the row's `s1_ask` column at render time (`render_sequence`), so `choose_copy` and SPEC 12's one-test-at-a-time rule stay as they are. Effort M.
3. **Pre-registered looks, no weekly peeking.** Three looks at 200, 400 and 600 delivered per arm, on closed windows only. Look 1 (about 18 Dec): no adoption; stop B only if P(B > A) ≤ 0.05 (about 5% chance under no effect). Look 2 (mid-January): shift allocation to 70/30 toward the leader if P ≥ 0.90, reversible. Look 3 (late February): adopt B if P ≥ 0.975; adopt A if P ≤ 0.10; otherwise extend to 1,200 per arm at the shifted allocation and read once more. Simulated in `looks_sim.py` with a normal approximation to the Beta posteriors: under no effect, 2% false adoption and 10% chance of a (reversible) shift; a genuine 1.3x lift is adopted at look 3 only 17% of the time and a 1.5x lift 33%, so a real but moderate effect will usually need the extension into spring. Weekly peeking with adoption at the first P ≥ 0.95, which the first draft implied, has 10% false adoption; it is not used.
4. **Holdouts.** The Control tier (15%) stays as the signal-blind baseline, with tier and matched signals frozen at enrollment. The January Claude opener runs with a 30% no-opener holdout inside Priority and Standard.
5. **Read every reply.** The classifier tags objection, competitor, renewal month, broker, trigger mentioned and persona; the Monday readout lists them. At about 25 replies a month this is the fastest learning the system has.
6. **Test order.** T1 the ask (phase 2, day one). T2 Harry-signed versus Hannah or Sam for 10–49 accounts, hash-assigned at first enrollment (January; replaces the plain-text test, which cannot be run because `text_only` is campaign-level). T3 Claude opener versus template (January, built-in holdout). T4 second contact on versus off at 50–249 (January; account-level metric). T5 long-form email 2 versus offer-style (spring). Wording and subject tests: never.
7. **Kill-rule thresholds.** SPEC 12's "industry group reply under 0.5% after 400 delivered" is far too lenient; use 2% non-negative reply after 400. The stop rule of 5 meetings per 1,500 accounts stays as the backstop.

### 8.3 What can be known by 18 Dec, and what cannot

By 18 Dec, about 500 accounts with closed windows: deliverability, bounce and complaint health; reply rate ± 1.5–2 points; the provider and step cuts; the objection mix; Test 1's look 1, which is a harm check and a direction, not a verdict. Not knowable: positive-rate lifts, signal values, copy-row differences, or whether the programme is "2x". Look 2 lands in mid-January and look 3 in late February, after the seasonal trough. Say all of this in the December readout so nobody over-reads it.

---

## 9. Decisions for Harry

| # | Decision | Recommendation | Evidence | Cost of being wrong |
| :- | :- | :- | :- | :- |
| D1 | A soft question at the end of email 1 (not a demo ask), tested against your no-ask version | Yes, as Test 1 from day one; both arms keep the industry link and no demo link | Gong CTA study (among emails with a CTA); no direct "no ask" study, hence a test | Half the accounts get a slightly more forward email 1 for some weeks; the harm rule at look 1 stops it |
| D2 | Plain text for step 1, for everyone | Plain text is the lower-risk default on placement, but less clear-cut since the signature: in text format its three links render as written-out URLs, so a plain-text email 1 shows four bare URLs where the HTML version shows four anchors. It cannot be tested cheaply because `text_only` is campaign-level; your call, taken together with D19 | 03 §3 (marketing-mail placement data; Instantly's own guide); bounded at about 1.07–1.12x | If wrong: slightly fewer clicks on the industry page, which is not measured today |
| D3 | Article 14 notice and footer | **Decided 1 Oct** (commit `8797784`): the notice names Apollo and Clay, states the legitimate-interests basis and points at the unsubscribe link; every email ends with the three-line signature (Spill, "Book a call here", Trustpilot; commit `7bef045`), with the sender's full name and the ad line gone; no postal address or privacy link. One optional tone edit, Harry's call: open the notice with "Why you're hearing from us: your work contact details are listed by the business data providers Apollo and Clay, and we read {company}'s public website." rather than "we found your name, role and work email", which reads as surveillance (02 §2). A one-page internal note of the legitimate-interests reasoning is good practice and takes 30 minutes | 02 §2; Art 14(3)(b) keeps the notice in email 1 | Low; wording only |
| D4 | Roles tab rewrite and seniority rule (Appendix A) | Yes; sheet change | Probe; 05 §3.5 | Low; reversible |
| D5 | Second contact at 50–249 (January) and the volume trade: 107 a week, 143 at three steps, or two more mailboxes on a third domain (paid, needs 21 days of warmup) | Second contact as T4 in January; decide on mailboxes after the first look | 03 §4 (confounded); §6.4 | Without new mailboxes total interest is roughly flat while interest per account rises |
| D6 | Legal Teams on from launch | Yes, as a bet with 20% Focus: 57% win on 14 UK deals (CI 33–79%) corroborated by a 10-customer US cluster from 2020–22 of which an unknown number churned | 05 §3.2, §5 | Low; 20% of volume for seven weeks |
| D7 | Narrow tech (exclude IT-services NAICS); teletherapy to partners | Yes | 01 §3; `tiers.py:56-84` | Smaller universe; still large |
| D8 | Scoring weights (Appendix A) | Yes; sheet change; confirm the size signal's fact in phase 1 | Probe; 05 §3.6 | Low; the tier-mix alert shows if thresholds need moving |
| D9 | Price line by size band | January, not launch: price is 9% of lost reasons and the ICP shift makes "$195 for the whole team" right for most accounts | SPEC 4; 05 §8 | Low either way |
| D10 | Named US customers as proof; "50,000" versus the site's "30,000" | Verified-active customers only, with permission; one figure everywhere | 04 §4; 05 §5 (36 of 52 churned) | A prospect who checks the site sees two numbers; naming a churned customer is worse |
| D11 | A second approver, and the repost window | Make Hannah and Sam approvers for replies to their own mailboxes, or allow a pre-approved positive template to send after 60 minutes unanswered in business hours; extend the repost window to 23:00 UK | HBR 7x within an hour; Spill's winners met in 2.3 days | A guardrail change (SPEC 1.3); without it, positives after 4 pm ET wait overnight |
| D12 | Threading: steps 2 and 4 in-thread, step 3 a new subject | Yes if phase 0 confirms Instantly threads on a blank subject | 02 §2 | Low |
| D13 | Visit alerts and pull-forward for enrolled accounts | Yes; company level only, and copy never mentions a visit | 04 §7.1; 03 §4 (CIPA suits over pixels, a risk Harry has accepted) | Low |
| D14 | Learning design: `kind` column on Tests, three pre-registered looks, no re-allocation before look 2 | Yes | §8 | Without it nothing is learnable before spring |
| D15 | Paid options: Instantly inbox placement ($47–97 a month); third domain and two mailboxes (January) | Placement tests optional; mailboxes after the first look | 04 §3 | Low |
| D16 | CA and WA, and FL | Keep the compliance exclusion and record its cost (12 of 52 historic US customers); FL on in wave 2 if compliance allows | 05 §3.3, §5 | Known, accepted |
| D17 | **Former Spill buyer at a US company as a signal** | Not in v1. If revisited: LIA and DPIA first; buyer or admin contacts only, never end users; stored on the account, not the person; counsel's sign-off | UK GDPR Art 5(1)(b); 05 §3.4 | Building it without the assessment is a data-protection breach in a mental-health context; not building it loses a small, high-converting cohort |
| D18 | Trial wording in reply drafts | Allow "a 30-day paid pilot" in drafts (or exempt drafts from the "free trial" phrase rule) | Trials won 63% (05 §4); `copy_rules.py:300` | Low |
| D19 | **The signature's links on step 1.** Since 1 Oct every email carries the industry or demo link plus three signature links (Spill, Harry's booking link, Trustpilot) plus Instantly's unsubscribe: five links across four domains on a cold first email, two of them to non-Spill domains | Your decision stands for emails 2–4. For step 1, two options to choose between: (a) keep the full signature everywhere and watch placement by provider and in the weekly seed inboxes; (b) a step-1-only rendering that keeps the three lines but makes only the booking link live, with "Spill" and "Trustpilot" as plain words, hash-split against (a). Because open tracking is off, the comparison is read on seed-inbox placement of both renderings (cheap and directional) and on reply rate, which would need thousands per arm to show a 10% placement difference. This is a render change (S), per step and per account, so unlike `text_only` it can be split | 03 §2.7, §3: link count and external domains are filter signals, with weak evidence on magnitude; §2.2 headroom | If (a) is wrong: some first emails land in Promotions or spam and the inbox lever stays capped; if (b) is wrong: readers of email 1 cannot click through to Trustpilot, which they can from email 2 |

---

## 10. Appendices

### Appendix A. Scoring and ICP rework

**A.1 Roles tab.** Titles are semicolon lists; `map_title_to_role` matches whole phrases, longest first, so "Head of HR" also catches "Head of HR and Operations". Seniority is new code in `pick_contacts`: C-level > VP > Head or Director > Lead or Manager, with the person named by a signal first. Title lists are global; there is no per-group list, so "Partner" (noisy outside law) waits until a per-group mechanism exists.

| Role | Titles (add to today's) | 10–49 | 50–249 |
| :- | :- | :- | :- |
| People leader | Chief Human Resources Officer; CHRO; VP Human Resources; VP HR; Head of HR; Head of Human Resources; Head of Talent; People Director; Director of People; Director of Human Resources; Director of People Operations; Head of People Operations; VP People & Culture; Total Rewards Director. **Remove HR Manager** | 3rd | **1st** |
| People manager (new) | HR Manager; People Operations Manager; People Manager; HR Business Partner; HR Generalist; Benefits Manager; HR Lead | not contacted | 3rd |
| Founder or executive | Owner; Co-Owner; Principal; Managing Principal; Founding Partner; General Manager | **1st** | 2nd |
| Operations | VP Operations; Operations Manager; Practice Manager; Studio Manager; Director of Finance and Operations | 2nd | 4th |
| Finance | unchanged | not contacted | not contacted |

Reasoning (05 §3.5): founders and CEOs won 55% at every size; senior People titles 50% at 50+; HR Manager and generalist titles 22–26%; Operations 25% at 50+ (n = 8). Today's defaults never reach a People leader at 10–49 and reach Operations before the founder at 50–249. Second contact at 50–249 (January): whichever of founder and senior People leader was not first.

**A.2 Signals tab.** Sheet edits unless marked. Openers are one observed fact with no inference about the reader.

| Signal | Now | Proposed | Why |
| :- | :- | :- | :- |
| Mental health support listed | +25; terms include "EAP; employee assistance" | +15; remove those two terms; add `job_posts` as a source | Stop the double count; keep it as a budget-and-brand signal |
| EAP named | +10 | +5; add Optum; Carelon; Cigna; Aetna Resources For Living; TELUS Health; Health Advocate; add `job_posts`; opener "Your benefits page lists an employee assistance program." | Angle trigger, reframed around "alongside or instead", never disparagement |
| Modern mental-health vendor named | Hold, one list | Two rows: competitors (Talkspace, Lyra, Modern Health, Spring Health, BetterUp, Nivati, Tava, Wellbound) Hold; complements (Headspace, Calm, Wellhub, Gympass) +5 Score, Progressive employer | Calm and Headspace buyers buy counseling too |
| Progressive benefits | +10 each, max +30 | +5 each, max +15; add plurals | Standard tech perks; max +30 skews Priority toward VC-backed startups that most often already have a modern vendor |
| Culture or values page | +10 | 0 (inactive) | Nearly universal |
| People leader in place | +10 | +10 | |
| New People leader | +30, 90 d | +30; opener "Congratulations on the new role." | UserGems 3x within 30 days (03 §2.3); 05's 2-of-10 is a post-demo trigger on a tiny sample. Confirm Apollo returns a start date in phase 1 |
| First People hire | +25 | +25; plus "People role open" +15, `apollo_jobs`, `open_people_roles >= 1`, 60 d, Growing team, opener "You're hiring for a People role." | A missing count never matches; the new row fires anyway |
| Recent funding | +20, 540 d | "Funding in the last 6 months" +20 (`days_since_funding <= 180`); "Funding 6–12 months ago" +10; nothing beyond; opener "Congratulations on the recent round." | Decay without code (January if time is short) |
| Hiring and growth | +15, source `apollo_org` | source `apollo_jobs, apollo_org` | Wiring bug confirmed by probe |
| Visited the US site | +20 | +35 | A visit alone is already Standard at +20; the change makes visit plus pricing (60) Priority. Angle stays General; copy never mentions the visit |
| Viewed US pricing or demo page | +15 | +25 | As above |
| Q4 plan-year window | +10 | inactive | No power to tell accounts apart; shifts every tier on 1 January. Seasonality is handled by reading in February, not by points |
| Team of 10–49 (new) | | +15, `apollo_org`, `employees >= 10 AND employees <= 49` | 05 §3.1. `employees` is a declared `apollo_org` field (`model.py:43-46`); confirm the source module writes it as a fact, or the row silently never matches |
| Team of 50–99 (new) | | +10, `employees >= 50 AND employees <= 99` | 05 §3.1 |
| On a PEO (new, phase 1) | | −10, Upgrade the EAP, from the free technographic filter | Bundled EAP; 15% of 10–499 employers |
| Uses Slack or Teams (new, January) | | +10 | Where Spill is delivered |
| Renewal in 60–120 days (new, January) | | +20, Form 5500 plan-year start | Brokers decide 8–12 weeks before enrollment |

Struck from the first draft: "Former Spill user at company" (§9 D17). Thresholds stay at 50 and 20; check the tier-mix alert after the first rescore.

**A.3 Hard exclusions and queue (code, S).** Age rule: founded less than 1 year ago, or removed. The 5-US-people rule only when `employees >= 50`. `SIZE_BAND_RANK`: 10–19 with 20–49, then 50–99, then 100–249. `admit`: dedupe against HubSpot company domains pulled incrementally.

**A.4 Industries.** Tech rows: `exclude_naics` 541512; 541513; 541519. Legal Teams: `active yes`, `priority 2`. Partner keywords: add "teletherapy; online therapy; virtual therapy; mental health platform". Focus tab at launch: Tech 50%, Agencies 30%, Legal 20%.

### Appendix B. Two worked sequences

Both were rendered through `render.render_sequence` with the real copy rules (`validate_copy_v2.py`); every email passes as written, and a scan for the first draft's unchecked claims ("never touches firm systems", "most tech companies already have an EAP", "how other firms run it", "no phone line to find", "survivor guilt") comes back clean. Email 2 is 191 words (Tech) and 209 (Legal), above `style.md`'s 180-word floor. Openers are one observed fact; `pick_opener` dropped "Your careers page lists unlimited PTO." for the banned word, as designed. Arm B of email 1 differs from arm A only by the closing question; the industry link stays. Role lines were tightened but remain Harry's structure. Every email then ends with the three-line signature Harry set on 1 Oct (Spill, "Book a call here", Trustpilot), email 1 adds the Article 14 notice in small type, and Instantly appends its unsubscribe link; as sent, email 1 carries five links, email 2 six and emails 3–4 five. The bodies below stop at the sign-off, as the Copy tab does.

#### B.1 Technology & Startups (sent by Hannah to a People leader at a 60-person company)

**Email 1 (day 0, plain text). Subject: Counseling that lives in Slack** (82 words; arm B 95)

> Hi {{first_name}},
>
> {{opener}} *(example: Your careers page lists an EAP through ComPsych.)*
>
> Tech teams move fast and burn out quietly. Between layoff whiplash, always-on Slack and building against a runway, the strain usually stays hidden until someone strong gives notice.
>
> {{role_line}} *(people leader: If you're the one people come to when things get hard, you want support they'll actually use and a rollout that doesn't eat your week.)*
>
> Spill is on-demand counseling that anyone on the team can book from Slack or Teams, often for the same day. Here's [how Spill works for tech companies and startups]({{industry_url}}).
>
> *Arm B adds:* Is this on the list at {{company}} this year, or already covered?
>
> Best wishes,
> {{sender_first_name}}

The closing question invites "already covered" as an answer, which names the incumbent and opens the EAP conversation, rather than a dead "no".

**Email 2 (day 7, in-thread if D12). Subject: What Spill does for tech teams** (191 words)

> Hi {{first_name}},
>
> Following my note last week, here's the short version of Spill for tech companies and startups.
>
> **What is Spill?**
> Spill is an on-demand counseling service, [trusted by over 50,000 employees]({{site_url}}). It helps tech teams stay productive, cuts absenteeism and frees up whoever carries HR, by dealing with the issues that most often derail work.
>
> **Who is Spill for?**
> Anyone at the company: engineers, designers, sales and support, and the founders themselves. That might be burnout from a sprint that never ends, the strain after a reorg, imposter feelings, anxiety or ADHD, or life events like a new baby or losing someone close.
>
> **What makes Spill unique**
> - Support the same day, in a couple of clicks. No waiting lists, phone trees or referrals.
> - Remote-native: video sessions across US time zones, early mornings, evenings and weekends.
> - Booking lives in Slack and Microsoft Teams, as well as email and any phone.
> - Managers get training and tools to support anyone on their team who's struggling.
> - {{price_line}} We don't lock you in.
>
> If you'd like to see it, [book a short demo]({{demo_url}}).
>
> Best wishes,
> {{sender_first_name}}

**Email 3 (day 14, new subject). Subject: Does Spill work alongside an EAP?** (82 words)

> Hi {{first_name}},
>
> A quick one, since this comes up a lot.
>
> If you already have an EAP through your carrier, Spill works alongside it. If you don't, it works on its own. Either way, the difference is where it lives: someone opens Slack or Teams, picks a time and talks to a counselor, often the same day. Founders are covered on the same plan as everyone else.
>
> Would it help to see how that looks for a team like yours? [Book a quick demo]({{demo_url}}).
>
> Best wishes,
> {{sender_first_name}}

**Email 4 (day 21, in-thread if D12). Subject: Who looks after benefits at {{company}}?** (57 words)

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

What changed from the current row and why: email 2's subject stops saying "brochure" (74 of 106 rows say "How Spill works for…" and 14 more "Spill for…"); email 2 drops the stock phrases ("in a couple of clicks" is in 98 rows, "it's useful" in 61 email-1s); email 3 asks the EAP question honestly for both the have-EAP and no-EAP reader (the first draft's "most tech companies already have an EAP" was false for the 10–49 startups the ICP favors); email 4 adds the referral ask, counted only when a follow-up is sent. The existing row's "survivor guilt after a layoff" is gone: a layoff-suppressed account could see it after 90 days.

#### B.2 Legal Teams (sent by Hannah to the managing partner of a 30-person firm)

**Email 1, arm A (day 0, plain text). Subject: Confidential counseling for {{company}}** (87 words)

> Hi {{first_name}},
>
> {{opener}}
>
> In most law firms the billable hour sets the pace, client emergencies own the evenings, and asking for help can still feel like weakness. So people push on until they burn out or leave.
>
> {{role_line}} *(founder: If you lead the firm, keeping good associates spares you the recruiting, ramp-up and lost billables that follow a departure.)*
>
> Spill is confidential counseling that attorneys and staff book privately from their own phones, often for the same day; the firm sees only anonymized, aggregate numbers. Here's [how Spill works for law firms]({{industry_url}}).
>
> Best wishes,
> {{sender_first_name}}

**Email 1, arm B for a 20–49-person firm** (103 words; adapted from the red team's rewrite). One observed fact, no inference after it; the link stays; one question.

> Hi {{first_name}},
>
> {{opener}} *(example: Your careers page lists an EAP through ComPsych.)*
>
> In a firm your size there is usually no HR department behind you, so when someone is struggling it lands on whoever runs the place, and it stays there until a good person hands in their notice.
>
> {{role_line}}
>
> Spill is on-demand counseling anyone at the firm books privately from Slack, Teams or their own phone, often for the same day, with you covered too. Here's [how Spill works for law firms]({{industry_url}}).
>
> Is this on the list at {{company}} this year, or already covered?
>
> Best wishes,
> {{sender_first_name}}

**Email 2 (day 7). Subject: What Spill does for law firms** (209 words)

> Hi {{first_name}},
>
> Following my note last week, here's the short version of Spill for law firms.
>
> **What is Spill?**
> Spill is an on-demand counseling service, [trusted by over 50,000 employees]({{site_url}}). It helps firms keep people productive, reduce absence and free up partners' and HR time by dealing with the issues that most often derail work.
>
> **Who is Spill for?**
> Everyone at the firm: attorneys, paralegals and business staff, and the owners too. That could be billable-hour burnout, the strain of adversarial work, perfectionism, anxiety or depression, or life events like having a baby or losing someone close.
> There's no minimum headcount, so a boutique gets the same support as a national firm.
>
> **What makes Spill unique**
> - Support the same day, in a couple of clicks. No waiting lists, callbacks or referrals.
> - Sessions early mornings, evenings and weekends, so they fit around court schedules and client emergencies.
> - People book privately, from their own phone or from Slack or Teams, and the firm sees only anonymized, aggregate data.
> - Partners and managers get training and tools to support anyone who's struggling.
> - {{price_line}} We don't lock you in.
>
> To see how it would work at {{company}}, [book a short demo]({{demo_url}}).
>
> Best wishes,
> {{sender_first_name}}

(The existing row's "nothing is reported to bar associations" and the first draft's "never touches firm systems" are gone: the first is a page-only claim flagged in open-questions #72; the second is not in `facts.md` and contradicts delivery through Slack and Teams.)

**Email 3 (day 14). Subject: Who covers paralegals and staff?** (54 words)

> Hi {{first_name}},
>
> The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?
>
> They work the same deadlines and client emergencies as the attorneys they support. Spill covers everyone at the firm, booked privately from a personal device, often for the same day, with the owners covered too.
>
> Is that a gap at {{company}}? If so, [book a quick demo]({{demo_url}}) and I'll walk you through it.
>
> Best wishes,
> {{sender_first_name}}

**Email 4 (day 21). Subject: Who handles benefits at {{company}}?** (60 words)

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

### Appendix C. Evidence caveats

**On the reports.**
- Report 01 is accurate on every claim re-run here. Its Clay arithmetic (2.5 credits an account to reach 650 a month) is a projection until phase 1 measures it.
- Report 02 is accurate on the code. Its threading claim is Instantly behavior that phase 0 must confirm. Its "47% legal text" was measured with the old footer; with the 1 Oct signature and notice, email 1 is 183 words of which 68 (37%) are the notice and 87 (48%) sit below the sign-off, 190 words once Instantly appends its unsubscribe line.
- Report 03's figures are mostly vendor datasets with self-selected users; Belkins' rates are per email; the "signal-based 15–25%" cohorts are small and hand-built; Validity's placement figures are for marketing mail. Direction is better supported than size throughout.
- Report 04 is accurate.
- Report 05's win rates are post-demo, UK-heavy, and small in the cells that matter most (Legal n = 14; Operations at 50+ n = 8; founders at 50+ n = 9); US data is 2020–22; persona counts are contact-to-deal links; 36 of 52 historic US customers have churned and recorded churn is dominated by "low usage", which also puts the only allowed statistic ("30% of employees use Spill") at some risk with US buyers and argues for usage-led onboarding. BigQuery was unreachable, so usage by segment is still missing.

**On this review.**
- No cold-email reply data exists for Spill in any system read. The baseline is inferred; its first real read is in late November, in the worst reply month of the year.
- The multipliers in §6 are judgments. The overlap discount moves the mid case between about 1.4x and 1.6x; the lever values move it more. "2x" is the optimistic case under the stated conditions and can only ever be shown as an A/B result inside a period.
- The soft-ask recommendation rests on CTA-type evidence, not "no ask versus ask" evidence; it is a test. The ask lever is also the only one the December data can speak to, and only at a first look.
- The "right person" multiplier is smaller on the interest metric than on demos held, because referrals count as interest; the demo-held effect is where Spill's own data is strongest.
- Everything that depends on Instantly (text for step 1, threading, the variable limit, subsequences, whether a stopped domain accepts a new lead) is unverified until the phase-0 session.
- Documentation drift that will mislead the next person: `docs/phase0-runbook.md:70` says `price_from = 250` while the default and Harry's decision are 195; `enrol/capacity.py:8`, SPEC.md:429 and open-questions #57 still say days 3, 8 and 15; `style.md` gives email 2 a 180-word floor while the render rule allows 150.

---

## 11. What changed after review

Rulings on each point in `11-red-team.md`. "Accept" means the text, numbers or plan above were changed accordingly.

**MUST FIX**
- 1.1 Opt-out not operable at first send: **accept, then overtaken.** Harry's 1 Oct decision (commit `8797784`) puts Instantly's own unsubscribe link in every step, so the mechanism exists; B1 is now the seed-inbox confirmation and the sync of Instantly's unsubscribes into suppression and HubSpot, built with reply polling.
- 1.2 Former-user signal: **accept.** Struck from A.2 and the levers; now D17, gated on an LIA and DPIA, buyer contacts only, stored on the account.
- 1.3 Lever E impossible against the baseline: **accept.** Funnel re-expressed per inboxed email; E bounded at 1.05 / 1.10 / 1.15 by placement headroom; the funnel script's dead branch noted (2.8).
- 1.4 Lever C measured against a stub: **accept.** C at 1.05 / 1.15 / 1.30 over the SPEC role rule; the title gap is called a bug fix.
- 1.5 Haircut applied to the multiplier: **accept.** `stack_v2.py` applies it to the lift; `stack.py` is superseded.
- 1.6 Test 2 cannot run (`text_only` is campaign-level): **accept.** Dropped; D2 reframed as a default that cannot be cheaply tested; T2 is now the Harry-signed sender test.
- 1.7 `first_email_text_only` needs a render change: **accept.** `render.py:273` change added to phase 0 code and the go-live checklist.
- 1.8 Counts and line references: **accept all** (74 + 14 brochure subjects; 49 / 61 "it's useful"; 98 "in a couple of clicks"; re-measured under the 1 Oct signature as 68 of 183 (37%) in Appendix C; `guard.py:44-58`; `mailboxes.py:100-106`; the 35 + 10 wording; a visit alone is already Standard).

**SHOULD FIX**
- 2.1 Ask lever too large and metric inflated: **partly accept.** G at 1.05 / 1.15 / 1.35 on positives; referrals count only when a follow-up is sent; non-negative reply rate is the leading metric. Kept G as the first test because it is the only lever December can read.
- 2.2 F mostly post-December: **accept.** F at 1.05 / 1.12 / 1.25 for the programme and 1.05 in the December window.
- 2.3 A and B one mechanism on post-demo evidence: **partly accept.** Merged at 1.02 / 1.10 / 1.22 rather than 1.00 / 1.10 / 1.20: excluding IT services removes a cohort whose copy is wrong for them, and re-ordering a queue deeper than capacity changes which accounts are contacted at all. Legal is labeled a bet with its sample sizes.
- 2.4 D barely exists by December and halves T1's sample: **accept.** Excluded from the December estimate; January test; sample effect stated.
- 2.5 Bayesian read under-specified: **accept.** Three pre-registered looks, P-based harm rule, adoption at 0.975, no re-allocation before look 2; error rates simulated (`looks_sim.py`), including the low power for a 1.3x effect.
- 2.6 T1 confounds three changes: **accept.** Both arms plain text, both keep the link; B differs by the question alone.
- 2.7 Provider gap observational: **accept**; a diagnostic, not a test.
- 2.8 Funnel rows did nothing: **accept**; re-expressed.
- 2.9 Article 14 layering makes a draft page load-bearing: **overtaken.** Harry has decided there is no privacy page or postal address in the emails; the notice names the sources, states the basis, points at the unsubscribe link and stays in email 1 (§4.1).
- 2.10 Tracker promoted while the notice is a draft: **partly accept.** Company level only; the disclosure gate went with the privacy page; the CIPA point is noted once.
- 2.11 Third domain is paid and late: **accept.** Cost column corrected; January.
- 2.12 HTML, long form, price: **partly accept.** D2 reframed as Harry's call; email 2 kept above `style.md`'s 180 words (191 / 209); size-banded price moved to January. The drift between `style.md` (180) and the render rule (150) is noted rather than resolved.
- 2.13 Appendix A promises: **accept.** Size signal flagged for phase-1 confirmation; HubSpot dedupe by incremental windows; "Partner" withdrawn pending per-group lists.
- 2.14 Feasibility: **accept.** Hours budget (§7.5); phase 0 allowed to spread; decisions split into this week and phase 2; the 10% haircut raised to 15% in the conservative and mid cases.

**Missing items (§5 of the red team)**
- Harry's hours: **accept**, §7.5 and the minimum viable set.
- Demo page: **accept**, D15 and lever I.
- Seasonality and renewal: **accept**; Q4 points stay off, February re-read, renewal month asked in every reply, Form 5500 in January.
- Brokers and PEOs: **accept**; PEO detection in phase 1 (U4), broker ask (U5), broker referrals logged not chased.
- Churned customers and the 30% claim: **accept**; verified-active proof only; the usage risk noted in Appendix C.
- Trial: **accept**, D18.
- Referral mechanics: **accept**, U8 and §3.2 item 11.
- Cheaper substitutes: **partly accept.** Three steps (U10) and the Harry-signed test (U9) added; "ask the renewal month" added; the Claude opener kept for January with its holdout because one dated trigger covers few accounts.
- Unit economics: **accept**, §6.4.
- LinkedIn: **accept**; manual only.

**Copy critique (§6 of the red team)**
- All claim and tone points: **accept.** Appendix B rewritten and re-rendered: no "never touches firm systems", no false market claim about EAP prevalence, no "other firms", no dig at carrier EAPs, no "survivor guilt", openers reduced to one observed fact, role lines tightened; the red team's founder email adapted as Legal arm B; "no content link" variant dropped; the shorter Article 14 line adopted with "business data providers" rather than "business directories".

**Rejected:** nothing outright. The two places where this review keeps a slightly higher value than the red team (A+B's low and mid; keeping the Claude opener in January) are explained above.

**Harry's decision of 1 Oct, received after the red team (commit `8797784`):** no postal address and no privacy link in the emails; the opt-out is Instantly's own unsubscribe link in every step template, with the List-Unsubscribe header; `postal_address` and `privacy_url` retired; the footer reduced to the sender and the ad line (superseded the same day, below); the Article 14 notice points at the unsubscribe link. Reflected in §1 (verdict item 3 and moves 1 and 4), §2.3, §3.2 item 6, §3.3, §4.1 (B1 narrowed to the seed test and the unsubscribe sync; the privacy-page and LIA blockers removed; blockers renumbered), §4.2 D3, §4.3 U1, §5.1, §5.3, §6.1 lever F, §6.3, §7.1–7.3, §7.5, §7.6, §9 D3 and D13, and Appendix C.

**Harry's second decision of 1 Oct (commit `7bef045`), "footer = signature":** every email now ends with "Best wishes,", the sender's first name and three signature lines (Spill linking the US site, "Book a call here" linking Harry's meeting link, Trustpilot reviews), then on email 1 the notice in small type, then Instantly's unsubscribe link; the sender's full name and the ad line are gone. Re-rendered under this commit, email 1 is 183 words (37% notice, 48% below the sign-off) with five links across four domains, and all Appendix B copy still passes with no violations; the suite passes. Reflected in §1, §2.2–2.3, §3.2, §3.3, §4.2 D2, D3 and D15, §6.1 lever E (narrowed to 1.03 / 1.07 / 1.12 for the link count), §6.2–6.3 (mid 1.46x, optimistic 1.98x, December mid 1.32x), §7.1, §7.5, §7.6, §8.2, §9 D2, D3 and the new D19, Appendix B and Appendix C.
