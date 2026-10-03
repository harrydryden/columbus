# Red team: the "2x interest per lead" plan (10-synthesis.md)

Reviewed 1 Oct 2026 against repo `cb0bf8f` (re-ran the suite: 1,273 passed, 34 skipped) and the five input reports. All four scripts were re-run from the repo's `.venv` (they fail under the system Python; `probe_synth.py` and `validate_copy.py` need `psycopg` and `tldextract`). Every code claim I checked is listed in §1–2; the ones I did not list were correct as cited.

Short version: the plan is a good design review with an honest appendix of caveats, but its headline number is not derived honestly enough to act on. The 2.2x mid case rests on one lever with no direct evidence (the ask), one lever whose multiplier is arithmetically impossible against the plan's own baseline (inbox placement), and one lever measured against a stub rather than the designed system (contact choice). Re-derived with the same method and defensible values, the mid case is about 1.45x. There are also two compliance gaps that must close before the first send and one proposed signal that should not be built at all without a privacy assessment.

---

## 1. MUST FIX

**1.1 CAN-SPAM opt-out is not operable at first send.** The footer reads "Reply STOP or use this link to opt out: {privacy_url}" (`templates/copy/footer.txt`). The link goes to a privacy notice that is a Webflow draft and "there is no opt-out page" (`docs/phase0-facts.md`, Website table). "Reply STOP" is processed by `poll_replies`, which is phase 2 and unbuilt (`ops/cli.py:81`; `ops/schedule.py:45`). The plan's go-live checklist (§7, items 1–8) requires `privacy_url` live but never requires a working opt-out mechanism or STOP processing. CAN-SPAM requires a functioning opt-out honoured within 10 business days; SPEC 13 promises same day. *Correction:* add to the gate: (a) an opt-out form or page is live at `privacy_url` or a dedicated URL; (b) `poll_replies` classifies `unsubscribe` and writes suppression and the Instantly blocklist, tested end to end on a seed inbox; (c) `sync_outcomes` pulls Instantly-side unsubscribes. No send until all three pass.

**1.2 Lever U7 / Appendix A.2 "Former Spill user at company (+30)" re-purposes UK customer-contact data and should not be built as described.** The plan proposes matching "HubSpot UK champions" to new US employers through Apollo people search and storing "former Spill user" as a scored fact (§4.3 U7; A.2 last row; 5.2 "Job Change on champions"). That is processing of identifiable people's data for a purpose they were not told about (UK GDPR Art 5(1)(b)), disclosure of their names to Apollo and Clay, and an inference stored against a named person about a mental-health service. For admin or buyer contacts a legitimate-interests assessment might pass; for anyone who was an end user it will not. *Correction:* strike the signal from A.2; if revisited in phase 4, restrict to buyer/admin contacts, document an LIA and a DPIA first, and never store the fact on a person, only "a prior buyer works here" on the account.

**1.3 Lever E's multiplier is impossible against the plan's own baseline.** §2.2 puts inbox placement at 70 / 80 / 88%. §6.1 gives lever E (reaching the inbox) 1.10 / 1.25 / 1.50. A 1.25x on an 80% placement rate is 100% placement; 1.50x on 88% is 132%. Even if E also claims some of "read" (45–60%), the plan gives no mechanism for plain text raising read rate by 20–50%. *Correction:* cap E at the placement headroom: about 1.14 conservative (70→80%), 1.10 mid (80→88%), 1.05 optimistic (88→92%). This alone takes the mid case from 2.17x to about 1.95x.

**1.4 Lever C is measured against a stub, not the designed system.** The plan's baseline funnel is "current design" with SPEC 12's 3–5% reply (§2.2), which presumes a role rule. Lever C's 1.25x mid is then justified by "the default" being the earliest-created contact (`enrol/enrol.py:241-250`). But `pick_contact` is a placeholder; any build of `pick_contacts` applies the Roles tab. The incremental lift of wider titles, seniority and the founder fallback *over the SPEC role rule* is real but smaller. *Correction:* C at 1.05 / 1.15 / 1.30, and say plainly that the title-gap fix is a bug fix to the spec'd design, not a lever over it.

**1.5 The haircut is applied to the multiplier, not the lift.** `stack.py` returns `total*(1-haircut)`. At total = 1.0 that yields 0.95, meaning "if the levers do nothing, interest falls 5%". The correct form is `1 + (total-1)*(1-haircut)`. The numeric effect here is small (2.41 → 2.27 rather than 2.17), but the formula is wrong and the error flatters the plan's "conservative" label. Fix the script before anyone reuses it.

**1.6 Test 2 (plain text vs HTML by hash split) cannot be run as designed.** `text_only` is a campaign-level Instantly setting (`clients/instantly.py:54-59`; `registry/mailboxes.py:114-179`), and there is one campaign per sender (SPEC 9). Two formats cannot coexist in one campaign, and the drift check would flag a second campaign. The same applies to `first_email_text_only`. *Correction:* either pick a step-1 format for everyone (the plan's D2 effectively does) and drop T2, or render "plain-looking HTML" (no bold, bullets or styled footer) for arm B, which is a weaker test.

**1.7 `first_email_text_only` needs a render change, not just a campaign flag.** `render.py:273` chooses HTML or text for *all* steps from `email_format`. With the flag on and `email_format = html`, the `s1_body` variable holds HTML; if Instantly sends it as text the recipient sees tags. Phase 0 must confirm which part Instantly uses, and `render_step` must emit `text` for step 1 when the flag is set.

**1.8 Code-claim corrections (all minor; the headline claims are right).**
- "How Spill works for" is in 74 of 106 email-2 subjects, not 77 (14 more read "Spill for X").
- "If it's useful, here's" is in 49 rows (51 with "If it's useful" alone; 61 with "In case it's useful"), not 60.
- "in a couple of clicks" is in 97–98 rows, not 100.
- "47% legal text": with the plan's own placeholder address the rendered tech email 1 is 214 words of which 97 (68 notice, 29 footer) are legal, 45%. The plan cites 47%, "95 of 203" and "214 of which about 95" in three places; pick one.
- `guard.py:42-56` should be `44-58` (`website_visitors.search` is line 53).
- `mailboxes.py:103-106` is `100-106` (the score check starts at 100).
- "Carrier EAP alone scores 45" is correct but 10 of the 45 is Q4; the double count itself is 35 (`defaults.py:157-167`). The §3.2 wording is fine; the §1 wording ("scores 45") invites a reader to think the EAP overlap is worth 45.
- Appendix A.2 says a visit alone "should be Standard": at +20 it already is (`standard_threshold` 20). The only change the new weights make is visit + pricing (60) reaching Priority.

Everything else I spot-checked was correct: the eight missing titles map to `None` and "HR Manager" maps to People leader (probe); Operations ranks 2 and Founder 3 at 50–249 and People leader has no 10–49 rank (`defaults.py:315-333`); Hiring reads `apollo_org` only (`defaults.py:206`, probe 0 points from `apollo_jobs`); Q4 gives every account 10 in Oct (probe); `pick_contact` sorts by `(created_at, contact_id)`; no code writes `kind="hand_check"`; `admit` has no HubSpot check while `pipeline.md:76` says it dedupes against the portal; `rescore` deletes and rewrites matches (`score.py:536-539`); Control baseline is "tier now" (`02_v_signal_value.sql:67`); unclassified replies count as human (`00_v_account_outcomes.sql:9`); `MAX_CAP = 30` with no ramp; promotion on score ≥ 90 *or* 21 days; three of four mailboxes on meetspill.org; `catch_all_valid` sendable while `allow_risky_contacts` is false; `follow_redirect`, `map_title_to_role`, `first_choice_for_size`, `fallback_order` have no callers; `hubspot_active_sequence` and `hubspot_other_activity_90d` are read in `tiers.py:93,96` and written nowhere; `proof_point` blank on 108 rows; Legal Teams `active = no`, priority 4; 76 rows have no question in any body.

---

## 2. SHOULD FIX

**2.1 The ask lever (G, 1.35x mid) is the largest lever and the weakest evidence, and it also inflates the metric.** The plan concedes (Appendix C) no study compares "no ask" with "interest ask". The Gong CTA study compares CTA *types* among emails that had one; "58% of replies from email 1" is a distribution, not a treatment effect. Two further problems: a soft question raises *negative* replies as much as positive ones, so the lift on the plan's metric (positive + referral + demo) is smaller than on total reply; and the email-4 referral ask manufactures "referrals", which the metric counts at full weight. *Correction:* G at 1.05 / 1.15 / 1.35 on positives; count a referral only when it yields a contactable name and a sent follow-up; make "non-negative reply rate" the leading metric for T1, not total reply.

**2.2 Lever F is mostly post-December.** Signal openers reach only accounts with a matched signal that has an opener (Priority and Standard, perhaps 40–60% of enrolments); the Claude opener starts mid-Nov with a 30% holdout; `proof_point` needs Harry to ask customers for permission, 36 of the 52 historic US customers have churned and the "active" flag may be stale (05 §5). For the 18 Dec window F is about 1.05–1.12, not 1.30.

**2.3 Levers A and B are the same mechanism and the Spill evidence is post-demo.** Both act by re-ordering a queue that is deeper than capacity using a score that is untested priors; narrowing NAICS is the only part that changes the universe. The 05 win rates are post-demo, UK-heavy and small (Legal n = 14, CI 33–79%; Finance n = 10; Operations at 50+ n = 8; Founder at 50+ n = 9), and "EAP in place won 32% vs 46%" says nothing about *reply* propensity (an EAP-holding company has a budget line and a benefits owner, which may raise replies). Treat A+B as one lever at 1.00 / 1.10 / 1.20 and label the Legal recommendation as a bet corroborated by a 10-customer cluster from 2020–22, of which an unknown number churned.

**2.4 Lever D will barely exist by 18 Dec and halves T1's sample.** It starts in phase 3 (16 Nov), needs reveals, and cuts weekly accounts to 107, so non-control volume falls from 128 to about 91 a week and T1's per-arm rate from 64 to 45. Hunter's 2.1x account-level figure is confounded (accounts where two people can be found are bigger and better covered). Exclude D from the December estimate; keep it as a January test.

**2.5 The Bayesian read is under-specified and peeks weekly.** P(B > A) ≥ 0.85 at 300 per arm is roughly a one-sided p of 0.15 on a single look; read weekly from week 5 onward, the chance of shifting allocation on a null effect is in the region of 30%. Shifting is reversible (20% floor), so tolerable, but "adopt B at P ≥ 0.95" with repeated looks carries perhaps 10–15% false adoption. Fix a look schedule (for example three looks: 200, 400, 600 per arm) and raise adoption to P ≥ 0.975, or say the test is exploratory.

**2.6 T1 confounds three changes.** Arm B is "soft question, no link, plain text" (§7 phase 2) while D2 recommends plain text for *all*. Decide which: if both arms are plain text, B differs by ask and link; if A is HTML, B differs by three things. A big-swing bundle is a defensible choice at this volume, but then the result is "bundle B beat bundle A", and the plan should not promise to learn the value of the ask.

**2.7 The "provider gap" test is observational.** Microsoft and Google recipients are not randomised; Microsoft 365 skews to larger, older firms. The 434-per-arm figure is the sample for a randomised comparison; a 2x gap between providers can still be a company-mix effect.

**2.8 The funnel's inbox and read rows do nothing.** `stats_synth.py` computes replies per *delivered* and ignores the inbox and read columns (the dead branch `if False`), so those rows are decoration, which is how lever E escaped the sanity check in 1.3.

**2.9 Article 14: layering is lawful in principle, but the plan makes a draft page load-bearing.** Spill is a UK controller, so UK GDPR applies to US recipients (Art 3(1)); Art 14(3)(b) makes the first email the deadline, so the notice cannot move to email 2. ICO guidance accepts layered notices, and 14(2)(f) can be satisfied by naming the sources in the linked notice; the ICO's view is that specific sources should be named where known, which they are. So D3 is workable *if* the linked notice is live before send and contains: controller identity, purposes and the legitimate-interests basis, categories of data, recipients (Instantly, Clay, Apollo, HubSpot) and that they are US processors (14(1)(f) transfers), retention (the 12-month contacts rule), rights including to object, and the ICO complaint route. Nothing in the repo or plan mentions a documented legitimate-interests assessment; write the one-page LIA before launch.

**2.10 The Apollo site tracker is being promoted to a core signal while the US privacy notice is a draft.** U1 leans on company-level identification of `/us` visitors. Report 03 itself notes 1,500+ California CIPA suits over such pixels. Spill excludes CA *contacts* but the tracker fires for CA *visitors*. Before U1, the live US privacy notice must disclose the tracker, and the plan should say so.

**2.11 The third domain and two mailboxes are a paid service and would not be Active before December.** SPEC 1.1 says stop and ask; the plan calls it a decision (D5), fine, but lever E's cost column says "0" while its multiplier includes the third domain. A domain added in phase 3 needs 21 days of warmup (`WARM_DAYS`), so it contributes nothing to the December read.

**2.12 Harry's decisions: HTML and the long form.** D2 recommends plain text for all step 1s on Validity's *marketing-mail* placement benchmark and Instantly's own guide, overriding a 1 Oct decision without a test. Given 1.6, the honest framing is: "this cannot be tested cheaply; plain text is the lower-risk default; your call." Email 2 is trimmed to about 165 words, below `style.md`'s own 180-word floor (the render rule allows 150); either change `style.md` or keep 180. D9 (size-banded price) argues against Harry's $195 with no test; price is 9% of lost reasons, and if lever A shifts the ICP toward 10–99 the flat line is right for most accounts. Lower priority than the plan implies.

**2.13 Appendix A details that need verification before they are promised as "sheet edits".** The size signal (`employees >= 10 AND employees <= 49`) needs `employees` to be readable as a fact by the condition parser from `apollo_org`; confirm, or it silently never matches, like "First People hire" today. `admit` deduping against "a daily HubSpot domain list": the CRM search API caps at 10,000 results per query, and the portal holds tens of thousands of companies, so this needs the export or incremental-sync path, not a daily search. Adding "Partner" to Founder titles via a note is a global change: `map_title_to_role` has no per-group lists.

**2.14 Two feasibility optimisms.** Phase 0 asks Harry for 16 decisions, two text approvals, Clay function builds in the UI, an Instantly session, an Apollo fix and four infrastructure tasks by Fri 9 Oct, six working days away, for a person on under three hours a week. Phase 1 needs five source modules, the resolver, `pick_contacts` and the scoring changes by a hand-check on Mon 19 Oct. Neither is impossible for the builder; both are impossible for Harry's calendar. The 10% execution haircut does not reflect this.

---

## 3. Re-derived stacked estimate

Same structure as `stack.py` (additive within a stage with overlap discount, multiplicative across stages), with the haircut applied to the lift, and with the corrections above. Values are for the full programme once everything is live, not for the December window.

| Lever | Low | Mid | High | Why it differs from the plan |
| :- | :- | :- | :- | :- |
| A+B right account and moment (one lever) | 1.00 | 1.10 | 1.20 | Same mechanism; post-demo evidence; queue deeper than capacity |
| C right person | 1.05 | 1.15 | 1.30 | Measured against the SPEC role rule, not the stub |
| D second contact | 1.00 | 1.10 | 1.20 | Confounded source; 40% of accounts; late |
| E inbox | 1.05 | 1.10 | 1.15 | Bounded by placement headroom (§1.3) |
| F relevant message | 1.05 | 1.12 | 1.25 | Coverage of accounts with openers; proof needs permissions |
| G easy ask | 1.05 | 1.15 | 1.35 | No direct evidence; lifts negatives too |
| H follow-through | 1.00 | 1.02 | 1.05 | Acts on demos held, not on the stated metric |

| Case | Who | Seen | Act | Product | Haircut on lift | Interest per lead |
| :- | :- | :- | :- | :- | :- | :- |
| Conservative (low, 30% overlap) | 1.04 | 1.07 | 1.04 | 1.15 | 15% | **about 1.1x** |
| Mid (mid, 40% overlap) | 1.21 | 1.13 | 1.10 | 1.51 | 15% | **about 1.45x** |
| Optimistic (high, 50% overlap) | 1.35 | 1.20 | 1.20 | 1.94 | 10% | **about 1.85x** |

Reading: 2x is the optimistic case, not the mid. It is reachable only if T1's soft ask is a genuine 1.3x on positives *and* today's placement is poor enough for plain text to matter *and* the opener work lands. The first of those is the only one that will actually be measured by December, and the plan's §8.3 is right that nothing else will be. Two more honest statements belong in the summary: there is no measured baseline, so "2x" can only ever be an A/B result within a period, never a before/after; and at 2x the programme produces about 17 positives, 9 bookings and 7 demos a month, which at Spill's 15–20% outbound close (05 §9 item 14) is roughly one customer a month.

On the sample-size arithmetic itself: `n_per_arm` and `power` are correctly implemented (I re-derived 2,318 for 1%→2%), the tables are right, and the "weeks at 64 per arm" column is right for 128 non-control accounts a week; see 2.4 for why that falls to 45 once D is live.

---

## 4. The minimum viable 2x set (by mid-January)

The smallest set that captures most of the plausible gain, fits Harry's hours, and leaves the test readable.

1. **Sheet only, this week (Harry, about one hour):** Roles rewrite with seniority titles and the founder fallback at 50–249 (A.1); remove "EAP; employee assistance" from "Mental health support listed"; Hiring source `apollo_jobs, apollo_org`; Q4 inactive; size signal if the fact is readable (2.13); `exclude_naics` 541512/3/9 on tech rows; Legal Teams on at priority 2 with Focus 20%. Skip the funding split, PEO, Slack/Teams and Form 5500 rows for now.
2. **Code, phase 1:** `pick_contacts` with the role rule, seniority rank and `contact_email_status = verified` filter; the `hand_check post` job; ramp caps 10/20/30 and promotion on score *and* time; step-1 plain text rendered as text (1.7). Nothing else from §7 phase 1 beyond what SPEC phase 1 already requires.
3. **Compliance, before first send:** live privacy *and* opt-out page; `poll_replies` with `unsubscribe` → suppression and blocklist tested; short Article 14 line only if the linked notice carries the full content (2.9); one-page LIA.
4. **Copy:** the launch-fix pass on the four live rows only (Tech, Agencies, Legal, General), not 106; the EAP objection in email 3 and the referral ask in email 4 as drafted.
5. **Test 1 from day one:** soft question in email 1 vs no ask, both arms plain text, both keeping the industry link (so the test is the ask alone), three pre-registered looks on non-negative reply rate.
6. **Reply desk minimum:** classification, Slack alert with draft, approvals; drafts for positive, referral and "we have an EAP"; Harry's demo calendar with slots inside 48 hours.
7. **Harry, 30 minutes:** fix the `/us/book-demo` SEO title ("The UK's Highest Rated EAP"), make the page say US and $195, and align 50,000 vs 30,000.

Defer to January or later: second contact, Claude opener, Instantly subsequences and visit pull-forward, UTMs, interest-status write-back, size-banded price, third domain, HubSpot customers module, Form 5500, former-user signal (1.2), Bayesian allocation shifting (read only, no re-allocation until the third look).

---

## 5. What's missing, ranked

1. **Harry's hours as the binding constraint.** The plan has no weekly hours budget. Phase 0 alone exceeds a month of his time at under three hours a week; in steady state he approves every copy row, every positive reply, and 30 hand-check accounts every Monday. The plan should list Harry's tasks with minutes and cut to fit, or name a second approver as a prerequisite rather than a D11 option.
2. **The demo page.** Every email 2–4 CTA, the planned demo-request metric and the 48-hour slots all land on `/us/book-demo`, whose title says "The UK's Highest Rated EAP" and whose price text differs from the emails. A 20% lift in that page's conversion multiplies every lever and costs nothing; it is mentioned only in passing (D10).
3. **Seasonality and renewal timing.** All December reads come from Nov–Dec sends, the worst reply period of the year (Belkins: December 0.35% vs February 0.54%) and US open-enrollment season, when HR is busiest. Switching Q4 off removes the only nod to timing without replacing it with a message ("Spill isn't insurance; it doesn't wait for your renewal") or a plan to re-read in February. Form 5500 renewal months are deferred to "phase 3 or 4, if on time".
4. **Brokers and PEOs.** 79% of employers choose benefits through a broker (03 §8); the plan treats PEO as a −10 signal in phase 3 and brokers only as a timing fact. At minimum: ask every positive and not-now reply for the broker and renewal month; detect PEO clients (bundled EAP) with the free technographic filter in phase 1, not 3; and decide whether a broker-referral reply is a referral or a dead end.
5. **Why 36 of 52 US customers churned.** The plan uses the 52 as proof points and ICP corroboration without asking. Recorded churn is dominated by "low usage", which puts the only allowed statistic ("30% of employees use Spill") at risk in the US and argues for usage-based onboarding, not a messaging tweak. Do not name a churned customer; verify "active" before any `proof_point`.
6. **The trial.** Trials won 63% in Spill's data and the plan never mentions one. "free trial" is a spam phrase in `copy_rules.py:300`, and reply drafts obey the copy rules, so even the reply desk cannot offer it. Exempt reply drafts or adopt "a 30-day paid pilot" wording.
7. **Referral mechanics.** The referred person is a new contact who needs an Article 14 notice, the CA/WA and personal-domain checks, suppression, and a fresh send from a mailbox; `Instantly.reply` only answers in-thread and `contact_block` refuses re-contact. The plan's "referral sideways" play has no path to the second person.
8. **Cheaper substitutes the plan did not weigh.** Three steps instead of four frees 25% of capacity (02 §7 item 9) for less cost than the second contact; a Harry-signed email to founders at 10–49 (founder-to-founder, 03 lever 12) is free; one dated trigger per account beats a Claude opener on cost and risk; "ask the renewal month in every reply" is a zero-cost timing signal.
9. **Unit economics.** Nowhere does the plan put cost (Clay credits, Apollo, four mailboxes, Harry's time) against about one customer a month at 2x.
10. **LinkedIn.** Correctly absent as automation (LinkedIn's terms prohibit it); say so, and keep any touches manual and optional.

---

## 6. Copy critique

**Would a skeptical US HR leader or founder reply?** To the base email 1: no, because it asks nothing and the only action is a click. To variant B: possibly, but three things get in the way.

- The Article 14 block is the most memorable line in the email: "we found your name, role and work email through Apollo and Clay... and read about Acme Labs on its public website." A US reader is not used to this and reads it as "we scraped you". It is legally required in email 1 (2.9), so the fix is tone and length, not removal.
- The example opener ("...so you're already investing here") is the fake-familiar flattery pattern 03 §5 says buyers now skip, and at 24 words it is longer than the pressure line. Openers should be one observed fact, no inference about the reader.
- The role line is still a mail-merge sentence ("For people leaders in tech, the test is simple...") with LLM cadence, and nothing in the body is about the company except `{{opener}}`.

**Rule breaches and claim risks in Appendix B.** `validate_copy.py` confirms the base versions pass the render rules (Legal email 2 line length fixed as the plan says). But:
- B.2 email 1 and email 2: "it never touches firm systems" / "Nothing touches firm systems" is not in `facts.md`, and it contradicts "Spill works through... Slack and Microsoft Teams"; QA should fail it as an unchecked claim.
- B.1 email 3: "most tech companies already have an EAP bundled with their health or disability cover" is a market claim that is false for the 10–49 startups lever A favours (BLS: access is far lower in small establishments). A founder with no EAP reads it as not-for-me.
- B.2 email 3: "I'll show you how other firms run it" implies named references the row does not have.
- B.1 email 3: "No phone line to find" passes the `DISPARAGING` regexes but is a dig at carrier EAPs in spirit; Harry's rule is "never disparage".
- B.1 email 2: "survivor guilt after a layoff" is heavier than the page copy; fine under `facts.md`, but a layoff-suppressed account (Layoffs signal) should never see it, and the row cannot know.
- Variant B's "no link in email 1" still carries the footer's privacy link; call it "no content link".

**A rewrite (email 1, founder at a 20–49-person firm, arm B; arm A ends after the link).** Obeys `facts.md` (no statistics, no dollar figure, no "therapy", no named customer), keeps Harry's industry link and "no demo ask", and puts the one account fact first with no inference after it.

> **Subject:** Counseling for the team at {{company}}
>
> Hi {{first_name}},
>
> {{opener}}
> *(example: Your careers page lists an EAP through ComPsych.)*
>
> In a firm your size there is usually no HR department behind you, so when someone is struggling it lands on whoever runs the place, and it stays there until a good person hands in their notice.
>
> {{role_line}}
> *(founder: Keeping the people who carry the work costs far less than replacing them.)*
>
> Spill is on-demand counseling anyone on the team books from Slack, Teams or their own phone, usually the same day, with you covered too. Here's [how firms like yours use it]({{industry_url}}).
>
> Is this on the list at {{company}} this year, or already covered?
>
> Best wishes,
> {{sender_first_name}}

About 85 words before the opener. The closing question invites "already covered" as an answer, which is a usable reply (it names the incumbent and opens the EAP conversation) rather than a dead "no".

**A shorter Article 14 line for counsel** (still names the sources, as the ICO prefers, and points at the footer link):

> Why you're hearing from us: your work contact details are listed in the business directories Apollo and Clay, and we read {company}'s public website. We email on the basis of our legitimate interests; our privacy notice (link below) says what we hold, for how long, and how to object or opt out.

About 50 words against 68 today, and it drops "we found your name, role and work email", which is the phrase that reads as surveillance.
