# US Outbound: outreach and conversion audit (queue → booked demo)

Repo `/home/user/columbus` at `cb0bf8f`, read 1 Oct 2026. I only read files and ran tests locally (1,273 passed, 34 skipped). I rendered sequences offline from the default settings, using a placeholder postal address and privacy URL. File:line references are relative to the repo root.

**Status.** The pipeline is built up to the point where a lead is handed to Instantly. Nothing after the first send is built. That covers reply polling, classification, approvals, HubSpot writes, meeting read-back, kill rules and the readout. All 106 copy rows are still `status = draft` (`us_outbound/settings/data/copy.csv`), although each one carries a QA pass stamp. The code itself does not stop enrolment from going live, but other gaps do:
- The weekly hand-check gate needs a `hand_check` item (`enrol/enrol.py:133-151`), and nothing in the code creates one.
- No copy row is approved.
- `privacy_url` and `postal_address` are blank, and render blocks a send while they are (`enrol/render.py:52-57`).

---

## 1. Built vs spec-only vs missing

| Area | Built (file:line) | Spec only / missing |
| :- | :- | :- |
| Enrolment | `enrol/enrol.py:593-683` (`run`):<br>• gates at 154-163<br>• candidates at 253-274<br>• copy choice at 304-351<br>• render and HubSpot re-check at 437-489<br>• `Instantly.add_leads` at `clients/instantly.py:569-614`<br>• capacity forecast at `enrol/capacity.py:206-277`<br>• limits at `limits.py:53-75` | The job is disabled (`ops/schedule.py:44`).<br>The upstream feeds are not built: `pick_contacts` (`contacts/` is empty; `ops/cli.py:79`), `verify_in_clay` and the source modules.<br>Nothing creates the weekly hand-check item, so enrol would always skip with "has not been posted" (`enrol.py:141-142`). |
| Sequence sending | One campaign per owner:<br>• `registry/mailboxes.py:190-233`<br>• settings at `clients/instantly.py:41-50`<br>• `STEP_DAYS = (0, 7, 14, 21)` at `instantly.py:65`<br>• each step is `{{sN_subject}}` / `{{sN_body}}` (`mailboxes.py:59-61`) | No campaigns exist yet.<br>These are still PHASE0-CONFIRM:<br>• the custom-variable length limit (`instantly.py:89`, `None`)<br>• whether follow-ups stay on the step-1 address<br>• whether HTML renders in a custom variable<br>• the schedule day keys (`instantly.py:141-160`) |
| Reply polling and classification | Plumbing only:<br>• `Instantly.list_emails` (`instantly.py:657-698`)<br>• `reply` (709-720)<br>• `forward` (722-747)<br>• a generic structured-JSON Claude client with a spend cap (`clients/claude.py:167-253`) | `replies/` holds only `__init__.py`.<br>`templates/prompts/` is empty.<br>`poll_replies` is marked "not built yet" (`ops/cli.py:81`) and disabled (`schedule.py:45`).<br>The classification schema, the confidence threshold and the routing (SPEC.md:498-526) are not built. |
| Human in the loop | The `hitl_items` table (`sql/ddl/09_hitl_items.sql`), the reply-pause gate (`enrol.py:114-122`), and Slack `post` / `replies` (`clients/slack.py:73-120`) | No alert poster.<br>`poll_approvals` is not built (`cli.py:82`).<br>No 2-hour repost, no 24-hour escalation, no forward-or-task fallback (SPEC.md:540-552). |
| HubSpot writes | The six properties (`crm/hubspot_writes.py:109-150`), write primitives (`clients/hubspot.py:277-383`), the pre-send exclusion re-check (`enrol.py:376-399`) and the opt-out/bounce suppression load (`suppression.py:132-147`) | Writes triggered by replies are only a placeholder comment (`hubspot_writes.py:168-171`).<br>The deal-on-demo logic is not built. |
| Meeting booking and attribution | The links exist as settings only: `booking_page` and `booking_link` (`settings/defaults.py:98-100`) | `hubspot_readback` is not built (`cli.py:83`).<br>The HubSpot client has no meeting reads.<br>No code anywhere writes a `meeting_booked` event.<br>**No UTMs anywhere**, although SPEC.md:54 promises them. |
| Kill rules | `v_mailbox_health` (bounce rate over the last 100 sends; `sql/views/04_v_mailbox_health.sql`), `heartbeat_check`, and operator `stop` / `start` (`cli.py:322-361`) | `kill_rules` is not built (`cli.py:86`).<br>`sync_outcomes` is not built (`cli.py:84`), so no sent or bounced events exist and the health view stays empty.<br>The stop rule (SPEC.md:609) is not built. |
| A/B testing and readout | Hash split (`enrol/queue.py:97-99`), test-aware copy choice (`enrol.py:331-351`), `test start` / `read` (`cli.py:430-545`), and the views `v_account_outcomes`, `v_readout_weekly` and `v_signal_value` | `learn/` is empty.<br>`daily_post` and `monday_readout` are not built (`cli.py:87-88`).<br>`read_test` returns raw rates with no significance or interval.<br>The default test t1 names `eap-v1`, which does not exist (`defaults.py:361-368`; open-questions.md:74). |

---

## 2. The sequence and the copy

### What a prospect actually receives

I rendered all 106 rows for the People leader role:

| Step | Day | Words written (median) | Words delivered, with legal text and footer | Links | Call to action |
| :- | :- | :- | :- | :- | :- |
| 1 | 0 | 107 (style target 60–110) | **203** | 2 (industry page, privacy) | None: "If it's useful, here's [how Spill works for X]" |
| 2 | 7 | 235 | 263 | 3 (site, demo page, privacy) | "book a short demo" |
| 3 | 14 | 74 | 102 | 2 | "book 20 minutes for a demo" or "book a quick demo" |
| 4 | 21 | 60 | 89 | 2 | "book a demo" |

Email 1 is about 47% legal text and footer. That comes from three pieces:
- The UK GDPR Article 14 notice is appended to step 1 (`render.py:254-257`), and it is about 70 words long: *"Where we got your details: we found your name, role and work email through Apollo and Clay, which provide business contact data… We contact you on the basis of our legitimate interests…"* (`templates/copy/article14.txt`).
- A four-line footer follows it, with a UK postal address and *"This is a marketing email from Spill. Reply STOP or use this link to opt out"* (`templates/copy/footer.txt`).
- In HTML, both are set in 12px grey (`copy_markup.py:37`).

Naming the data brokers in the first touch invites "how did you get my data" replies and spam complaints. Whether a short layered notice with a link is enough under Article 14 is a legal question for Harry. It is the largest single change to how email 1 reads.

### Subject lines
- **Email 1 (often fine, but repetitive).** 67 of 106 subjects contain "support", for example *"Support for the team at {{company}}"*, *"Support for account teams at {{company}}"* and *"Support through peak season at {{company}}"*. That reads as a vendor category, not as a reason to open. The better ones are specific and curiosity-led:
  - *"Counseling that lives in Slack"*
  - *"Before call-outs turn into resignations"*
  - *"For the people who listen all day"*
- **Email 2 is the weakest.** 74 of 106 are *"How Spill works for X"* and 14 are *"Spill for X"*. Both say "brochure" in the preview.
- **Email 3 is the strongest.** 41 of 106 are objection-led questions:
  - *"Will partners know who's using it?"*
  - *"Who supports the founder?"*
  - *"Too small for a benefit like this?"*
- **Email 4** uses standard break-up subjects: *"One last note for X"*, *"Leaving this with you"*, *"One more thought, then I'll stop"*.
- **Threading.** Every step has its own subject (`mailboxes.py:59-61`). In Instantly, a follow-up with a non-empty subject usually goes out as a new thread, not as a reply under email 1, so emails 2 to 4 arrive with no context. I found no decision about this anywhere in the repo, so it should be decided and checked in phase 0.

### First line and preview text
For an account with a signal, the preview reads: *"Hi Dana, I saw your team already has an employee assistance program. In most CPA firms, busy season isn't a spike, it's the calendar…"*

The opener is the angle's **generic default** (`defaults.py:245-276`), because no default signal has an opener template (`defaults.py:392`, `"opener": ""`). The other defaults are just as thin: *"It's clear you already invest in your people."* and *"It looks like your team is growing fast."*

General-angle and Control accounts get no opener at all (`enrol.py:365-366`). Their preview opens straight on an industry generalisation: *"In most marketing agencies, account teams sit between what the client wants this week and what the creative team can realistically deliver."* That reads like a blog intro, not a note to one person.

### Relevance and personalisation
The real variables are industry, role bucket, first name and, in some subjects only, the company name:
- `{{company}}` appears in **no** email-1 body. It is in 46 of 106 email-1 subjects.
- `{{place}}` and `{{proof}}` are never used in any row.
- `proof_point` is blank on all 108 Industries rows.
- The angle's `argument`, for example *"30% of employees use Spill"* for Upgrade the EAP, is never rendered anywhere in the code.

So the scoring engine collects evidence and then drops almost all of it. Benefits-page quotes, a new Head of People, funding and open roles all reduce to one stock sentence.

**Role lines are segmentation, not personalisation.** Each industry has three lines, picked by title bucket. Many are conditional, which is a mail-merge tell:
- General, People leader: *"If you're the person everyone comes to when things get hard, you want support people actually use, and a benefit that's easy to roll out."*
- CPA firms, People leader: *"If you're the one staff come to when busy season gets heavy…"*

The founder lines are better because they name a concrete cost:
- Technology & Startups: *"For founders, keeping the senior engineers who carry the product usually costs far less than recruiting and ramping their replacements."*
- Plumbing & HVAC: *"Experienced techs are the business…"*

Role lines are worth keeping as a fallback, but they are not "why you, why now".

### Length and format
- Email 2 is a brochure. It has a bridge line, three bold headings (**What is Spill? / Who is Spill for? / What makes Spill unique**), four bullets, a price line and three links: about 263 words in HTML.
- Sent on day 7 in a new thread, it reads like a newsletter. It has the highest Promotions-tab risk in the sequence and the lowest reply intent.
- SPEC.md:459-462 had planned email 2 as "one proof point and the demo link".
- The price line, *"Plans start from $195 a month for the whole team"*, is the same for every size. For a 200–249 person account, SPEC.md:112 prices at $5 per employee, about $1,000–1,250 a month, so the demo starts with a large surprise.

### The CTA ladder is flat and high-friction
- Email 1 asks for nothing (Harry's decision, open-questions.md:69). Its only action is a click on the industry page, which nobody can measure: tracking is off and there are no UTMs.
- Emails 2 to 4 repeat the same ask, a link to a demo page, three times.
- No body in email 1 or email 4 contains a question. 76 of 106 rows never ask the reader a question; email 2's question marks are only its headings.
- The low-friction asks were removed:
  - SPEC.md:478 had asks by role ("worth a look for the team?", "happy to send the one-pager").
  - Open-questions.md:38-39 records the decision to drop them, along with the one-pager.
- No email offers a referral path, such as "who handles benefits?".
- In cold email, the reply is the conversion. This sequence pushes clicks onto a page whose US SEO title reads *"The UK's Highest Rated EAP"* (`docs/phase0-facts.md:80`).

### How much it sounds like a template or like AI
- The prose is clean, with no hype words and no exclamation marks.
- It does have essay-like LLM rhythm:
  - *"busy season isn't a spike, it's the calendar"*
  - *"it's a retention lever, not a perk"*
  - *"the owner's stress often becomes the weather everyone works in"*
  - triplets such as *"in focus, in sick days, sometimes in a resignation"*
- Stock phrases recur across the library:
  - "If it's useful, here's…": 50 email-1s, plus 10 with "In case it's useful".
  - "often the same day": 72 email-1s.
  - "in a couple of clicks": 100 email-2s.
  - "We don't lock you in": 106 email-2s.
- Every row has the same skeleton, required by `templates/copy/style.md:32-49`.
- A single prospect will not notice the sameness across rows. They will notice that nothing in the email is about their own company.

### What is good
- The industry pressure lines are specific and credible, for example *"Plumbing and HVAC run on emergency callouts and seasonal surges"* and *"the billable hour sets the pace"*.
- The email-3 objection content is strong, for example *"Will partners know who's using it? They won't…"*.
- The nonprofit grant-budget angle (*"Can staff counseling be written into a grant or funder budget?"*) is a genuine insight.
- The copy rules are enforced at render time, which is unusually rigorous.

---

## 3. Deliverability

| Item | Current state (evidence) | Risk or gap |
| :- | :- | :- |
| Mailboxes and volume | 4 mailboxes on 2 domains (meetspill.org ×3, tryspill.org ×1; SPEC.md:622). The cap is 30 a day, hard-maxed at 30 (`mailboxes.py:55-56`). 150 accounts × 4 steps = 600 a week = **120 a day, 100% of capacity** (`docs/pipeline.md:229`). meetspill.org carries 90 a day. | No ramp: a mailbox goes straight to 30 a day when it becomes Active. 75% of capacity sits on one domain, with no spare warmed domains. |
| Warmup | Kept on and re-enabled daily (`mailboxes.py:497-498`). A mailbox is promoted at score ≥ 90 **or** after 21 days whatever its score (`mailboxes.py:100-106`). | A mailbox can go Active with a poor health score. |
| Health gating | `mailbox_health` only promotes Warming mailboxes and retires Paused ones (`mailboxes.py:486-495`). | An Active mailbox whose warmup is "banned" or whose health score falls stays Active; it is only reported to Slack. The kill rules (SPEC.md:601-609) are not built. |
| Tracking | Open and link tracking are off, and the guard refuses to turn them on (`instantly.py:45-46`, `253-257`). | Good for inbox placement. It also means no engagement signal and, with no UTMs, no click attribution. |
| HTML vs text | `email_format = html` (`defaults.py:144`), against SPEC.md:433 ("Text only: On"). Bold, bullets, anchors, and a 12px grey footer. | Promotions-tab and filter risk. Phase 0 must confirm that Instantly sends a `text/plain` part along with the HTML. |
| Links | Email 1 has 2 links, where SPEC.md:459 allowed one. Email 2 has 3; emails 3–4 have 2. The link domain is spill.chat; the sending domains are meetspill.org and tryspill.org. | A first touch with no links is best practice. Each extra link adds risk. |
| Bounce controls | `catch_all_valid` counts as sendable (`enrol.py:57`). Instantly verification is off (`instantly.py:81`). `allow_risky_contacts: False` may silently drop catch-alls (`pipeline.md:340`). | The 3% bounce kill rules and the per-source pause are not built. |
| Unsubscribe | The List-Unsubscribe header is on (`instantly.py:47`). The footer's opt-out link points at the privacy notice, and no opt-out page exists (`phase0-facts.md:81`). The blocklist takes addresses only (`instantly.py:751-768`). | "Reply STOP" depends on `poll_replies`, which is not built. Instantly-side unsubscribes don't reach suppression until `sync_outcomes` is built. |
| Spam-phrase rules | 11 phrase regexes (`copy_rules.py:296-302`), plus no "!", no "$" outside the price line, no "Re:" and no emoji (`copy_rules.py:310-331`). | Narrow. There are no checks on link count, HTML weight, or health-condition words, and email 2 lists "anxiety, depression or ADHD". |
| Missing | — | Automated seed or inbox-placement tests: SPEC.md:630 makes them a manual weekly check, with no code. DNS and blacklist checks are manual (runbook.md:85). There is no Postmaster or SNDS monitoring, no domain rotation or spare pool, no reply rate per mailbox as a health proxy, and no complaint tracking. |

---

## 4. Channels and multi-threading
- **The system is email only.**
  - LinkedIn appears only as "company-list audiences" (ads) in phase 4 (SPEC.md:764).
  - No phone: Apollo direct dials are unused (phase0-facts.md:38).
  - No retargeting.
- **Website visits.** When an enrolled account visits the site, it is logged and listed under "Warm accounts" in the daily post, and is "not alerted" (SPEC.md:317). `site_visits` itself is not built. Email 1's only action is a site visit, so that intent signal leads nowhere.
- **One contact per account.**
  - `pick_contact` returns "the account's one contact in v1" (`enrol.py:241-250`).
  - `stop_for_company` is on (`instantly.py:43`).
  - A second contact comes only in phase 3, only for Priority accounts, and only after the first sequence ends (SPEC.md:752).
  - At 10–249 staff, the founder or People lead chosen by title often doesn't own benefits or is the wrong person. The one contact is a single point of failure.

---

## 5. Reply handling and speed-to-lead
None of this is built (section 1). The design itself also leaks interest:
- **Speed.** The chain is: polling every 15 minutes (`schedule.py:45`), classification, a Slack alert, then **Harry alone** approves. That makes Harry the only approver.
  - The 2-hour repost runs only 13:00–21:00 UK (SPEC.md:547), so a reply after 4pm ET waits overnight.
  - Escalation comes at 24 hours.
  - The 20-minute target in SPEC.md:47 covers classification and the Slack alert. Nothing targets how quickly the prospect gets an answer.
- **Objections** go to the daily post only, with no draft (SPEC.md:523). "We already have an EAP" is the obvious top objection, and the copy already holds a good answer ("works instead of, or alongside, an EAP"), but there is no response path for it.
- **Not now.** The date is stored and a reminder goes in the daily post (SPEC.md:522). There is no automatic re-sequence or nurture. `recontact_*` exist only as settings (`settings/model.py:128-129`), and `contact_block` refuses any contact ever enrolled (`enrol.py:217-218`).
- **Out of office.** `stop_on_auto_reply: False` (`instantly.py:44`), so steps keep landing while the contact is away. The return date is stored, but nothing re-times the steps.
- **Referral** ("talk to our HR person"):
  - The reply endpoint can only answer in the existing thread (`instantly.py:709-720`). There is no path to email the person they named from the sender's mailbox.
  - `wrong_person` waits for a new contact through the weekly hand-check (SPEC.md:526), which can take days.

---

## 6. Measurement and statistical power
**What is measured.** All of the following depends on `sync_outcomes` and `poll_replies`, neither of which is built:
- `v_account_outcomes`: a human reply within 28 days of step 1.
- `v_readout_weekly`: cuts by all, industry group and copy version.
- `v_signal_value`.
- `read_test`.

**What is not measured.** Replies by step, by role or title, by sender or mailbox, and by whether the opener was present; time to first response; clicks and visits by step (there are no UTMs).

**The A/B maths.** Two-sided α = 0.05 at 80% power. Volume is about 150 accounts a week, or about 128 excluding Control. Note that the test only covers accounts whose most specific row is `version_a`, and excludes Control (`enrol.py:346`). In practice that is one industry's row.

| Baseline | Lift | Accounts needed (both arms) | Weeks if all 150/wk went to the test |
| :- | :- | :- | :- |
| 1% positive | 2× | 4,632 | 31 |
| 1% positive | 1.5× | 15,494 | 103 |
| 2% positive | 2× | 2,278 | 15 |
| 3% reply | 2× | 1,492 | 10 |
| 5% reply | 2× | 864 | 6 |
| 5% reply | 1.5× | 2,936 | 20 |

Power of the SPEC design (400 accounts per arm, SPEC.md:585-587) to detect a 2× lift:

| Baseline | Power at 400/arm | Expected events per arm |
| :- | :- | :- |
| 5% | 77% | 20 vs 40 |
| 3% | 54% | 12 vs 24 |
| 2% | 38% | 8 vs 16 |
| 1% | 21% | 4 vs 8 |

So SPEC's claim that the test "detects 2×" holds only if replies run at about 5%.
- Realistic copy lifts of 1.2–1.5× are undetectable at this volume.
- Positive reply rate can never pick a winner at this volume.
- A single industry row gets only tens of accounts a week, so 800 test accounts could take many months.
- The signal check ("below Control after 200 accounts") compares two rates of about 3% with a ±2.4-point 95% interval at n = 200. That is noise.

**What the system learns from today:** nothing automatic. `learn/` is empty, and nothing re-weights itself (by design, SPEC.md:66). Harry reads views that will only fill once the reply jobs exist.

---

## 7. Weaknesses that lower interest per lead, ranked by likely impact
1. **No reply-inviting call to action.** Email 1 asks nothing, and emails 2–4 repeat a demo-page link (§2). *Direction:* an interest CTA in email 1, a referral ask in email 4, and the one-pager back in.
2. **The account evidence never reaches the email.** Signals collapse to stock openers, and there are no proof points (§2). *Direction:* a first line built from the account's own evidence, with the role line as fallback.
3. **The legal text doubles email 1.** The Article 14 notice and UK footer are about 95 of its 203 words (`render.py:254-260`). *Direction:* a short layered notice with a link, if counsel agrees.
4. **Deliverability is fragile** (§3): HTML with 2–3 links, inboxes at 100% of their cap with no ramp, 75% of volume on one domain, and no health gating or kill rules. Poor placement caps every other lever. *Direction:* plain text, a ramp, spare domains, and automatic demotion plus seed tests.
5. **One contact per account** (`enrol.py:241-250`). *Direction:* a second contact in parallel or staggered. The cost is capacity: two contacts × 3 steps ≈ 100 accounts a week.
6. **The reply desk is not built, and its design leaks** (§5). *Direction:* drafts for objections and referrals, an approver per mailbox, and pre-approved positive-reply templates.
7. **Little proof, and a UK-facing landing.** `proof_point` is blank everywhere, and emails say "50,000 employees" while the pages say 30,000 (open-questions.md:68). The demo page title says "The UK's Highest Rated EAP", and the footer gives a UK postal address.
8. **Email 2 is a brochure in a new thread** (§2). *Direction:* move the email-3 objection content earlier and shorten email 2.
9. **The cadence was set by capacity, not conversion** (`pipeline.md:224-231`). *Direction:* test 3 steps, which frees 25% of capacity.
10. **Measurement can't separate wins from noise** (§6). There are no UTMs (SPEC.md:54), no reply-by-step cut, and no significance in `read_test` (`cli.py:471-529`). *Direction:* test large structural changes across all rows at once, read them sequentially or with Bayesian methods, and add UTMs.

### Smaller inconsistencies
- Stale docstrings or docs still give days 3, 8 and 15: `enrol/capacity.py:8`, open-questions.md:57, and SPEC.md:46/429/459.
- The runbook says `price_from = 250` (`docs/phase0-runbook.md:70`), but the default and Harry's decision are 195 (`defaults.py:103`).
- The General row's own QA note flags its link text "how Spill works in your industry" as vague.
