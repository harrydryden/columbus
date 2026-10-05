# 03 — B2B cold outbound best practice and benchmarks (2025–26), applied to Spill US

Prepared 1 October 2026. Scope: how to roughly double positive replies and booked demos per lead contacted, for Spill's US SMB outbound (10–249 employees, one contact per account, 4 mailboxes, about 150 new accounts a week, a 4-email HTML sequence on days 0/7/14/21).

**How this was researched (read first).** About 75 web searches covering more than 45 distinct sources. The sandbox's egress proxy blocked direct page fetches on every domain tried (instantly.ai, hunter.io, lemlist.com, woodpecker.co, support.google.com and others), so figures come from search-engine extracts of the cited pages, not full reads. Spot-check any number before external use.

**Evidence labels.** **I** = independent (mailbox providers, regulators, government, academic/HBR, Gartner, SHRM, LIMRA, NAPEO, journalism). **V-data** = large vendor platform datasets (self-selected users, inconsistent metric definitions, published as marketing). **V-claim** = vendor or agency claims with small or undisclosed samples.

**Capacity context.** 150 accounts × 4 emails ≈ 600 emails a week ≈ 30 per mailbox per business day, already at Instantly's recommended cap (section 3). Multi-threading or extra touches needs more mailboxes first.

---

## 1. Benchmarks

### 1.1 Headline reply rates

| Source | Metric | Value | Sample / period | Type |
|---|---|---|---|---|
| Instantly Cold Email Benchmark 2026 ([link](https://instantly.ai/cold-email-benchmark-report-2026), pub. early 2026) | Average reply rate; top quartile; elite | 3.43%; 5.5%; 10%+ | "Billions" of emails, 1 Jan–18 Dec 2025 | V-data |
| Hunter State of Email Outreach 2026 ([link](https://hunter.io/the-state-of-cold-email)) | Average sequence reply rate (sales use case) | 4.5% (≈3%) | 31M emails sent in 2025, plus surveys of senders and recipients | V-data |
| Belkins 2026 response-rate study ([link](https://belkins.io/blog/cold-email-response-rates)) | Replies ÷ emails sent | 0.45% (February peak 0.54%, December low 0.35%) | 7.5M emails in 2025; about 1,200 meetings booked (≈1 per 6,250 emails) | V-data (agency) |
| Gong, "Does cold email even work any more?" ([link](https://www.gong.io/blog/does-cold-email-even-work-any-more-heres-what-the-data-says), 2025, 2024 data) | Rep reply rate, baseline vs top performers | 1.8% vs 3.9%; 344 emails per meeting | 25M cold emails | V-data |
| Saleshandy ([link](https://www.saleshandy.com/blog/cold-email-statistics/), 2026) | Average; top 10% | 3.7%; 8–12% | 53M emails | V-data |
| Woodpecker benchmarks tool ([link](https://woodpecker.co/cold-email-benchmarks/)) | Platform median reply | 1.5% | Platform-wide | V-data |
| Outreach ([link](https://www.outreach.ai/resources/blog/sales-2025-data-analysis), 2025) | Average sequence reply (all sequence types) | 2.9%; bounce 2.8%; opt-out 1.1% | All customers | V-data |
| Clay's own automated 4-step campaign ([link](https://www.clay.com/blog/ai-lead-scoring), 2026) | **Positive** reply rate | 5.1% | Not disclosed | V-claim |

**Positive replies** are rarely published. lemlist calls more than 5% positive "good", more than 8% "excellent" and under 3% "broken" ([lemcoach](https://help.lemlist.com/en/articles/13942005-lemcoach-cold-email-benchmarks-and-metrics-what-good-looks-like), 2026; V-claim). In Saleshandy's 12k-email test, positives were 34–50% of replies (AI-only 1.4% of 4.1%; human 4.2% of 10.4%; hybrid 7.3% of 14.7%) ([link](https://www.saleshandy.com/blog/ai-vs-human-cold-emails/), 2026; V-data, single test).

**Planning baseline for Spill:** about 2–4% total reply and about 0.8–1.5% positive per lead. Doubling means about 2–3% positive per lead.

### 1.2 By personalization and targeting
- **Personalization depth.** Woodpecker (20M+ emails): advanced personalization (a recent hire, a funding round, a specific pain point) reaches about 17–18% reply against 7–9% for basic ([link](https://woodpecker.co/blog/cold-email-statistics/), 2026; V-data). Hunter: two personalized body attributes give +56% ([2026 report](https://hunter.io/the-state-of-cold-email); V-data).
- **Signal-based versus list-based.** Instantly: emails referencing signals (funding, leadership change, hiring surges) reach 15–25%, about 5x average ([link](https://instantly.ai/cold-email-benchmark-report-2026); V-claim). These cohorts are small, hand-built and run by sophisticated senders; plan on about 1.5–3x.
- **Segment size (a targeting-quality proxy).** Hunter: campaigns of 50 recipients or fewer reply at 5.8% against 2.1% for 1,000+ (2.8x); the average sequence had 449 recipients. Saleshandy: under 200 prospects reply at about 2x those over 500. Both confounded by effort.
- **Company size and seniority.** Belkins 2025: 0–10 employees replied at 0.72%, 11–50 at 0.49%, 10,000+ at 0.22%; founders and owners at 0.57% against 0.42% for C-level and about 0.32% for VPs ([link](https://belkins.io/blog/cold-email-response-rates)). This supports Spill's SMB and founder targeting.

### 1.3 By sequence step
- Instantly: 58% of replies come from email 1, 42% from follow-ups; 4–7 touches perform best ([link](https://instantly.ai/cold-email-benchmark-report-2026)).
- Saleshandy: 44% of **positive** replies come from follow-ups, 26% from the first follow-up alone; winning sequences ran 4–6 follow-ups over 20–21 days ([link](https://www.saleshandy.com/blog/cold-email-statistics/)).
- Belkins 2026 (7.5M emails, 15M LinkedIn touches, 46k calls): over 53% of email-sourced **meetings** come from step 3 or later, and step 3 alone books more than steps 1 and 2 combined ([link](https://belkins.io/blog/sales-follow-up-statistics)). Its older data: follow-up 1 adds +49% replies, follow-up 2 +9%, follow-up 3 −20% ([link](https://belkins.io/blog/b2b-cold-email-subject-line-statistics)).
- Hunter: sequences with follow-ups reply at 4.9%, against 3.0% without.

So Spill's demo asks in emails 3 and 4 are well placed, but email 1 having *no ask* wastes the step that produces most replies.

### 1.4 HR, benefits and wellbeing to SMBs
No public benchmark covers cold email selling EAP or mental-health benefits to SMBs (an evidence gap). Closest proxies: Instantly puts recruiting/HR recipients at 5–8% reply and lemlist puts healthcare lowest at 1.9% (both V-claim); Expandi's LinkedIn HR benchmark shows 30.5% acceptance and 9.7% message reply (260,081 requests by 213 HR accounts, May 2025–April 2026; [link](https://expandi.io/industry-benchmarks/human-resources/); V-data).

**Strength of evidence (section 1):** moderate for the "3–5% average, more than 10% elite" band, because several large vendor datasets agree. Weak for positive-reply and signal-based numbers.

---

## 2. What moves reply rates, ranked by strength of evidence

1. **List quality and ICP fit** (strong in direction, magnitude confounded). The segment-size and company-size gradients above are consistent across vendors. Gartner (632 buyers, surveyed August–September 2024, published 25 June 2025): 73% of B2B buyers actively avoid suppliers who send irrelevant outreach ([link](https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-sales-survey-finds-61-percent-of-b2b-buyers-prefer-a-rep-free-buying-experience); I). Hunter's recipients cite lack of relevance 61% of the time. **Spill:** 150 accounts a week across 108 industries is about 1.4 per industry per week, too thin for relevant copy or learning; concentrate on 6–10 segments.
2. **Offer and CTA design** (moderate; large samples, one older).
   - Gong (304,174 emails, c. 2020–21): interest CTAs ("worth a look?") booked meetings 30% of the time, against 15% for specific-time and 13% for open-ended asks; inside an active deal the specific ask wins, 37% ([link](https://www.gong.io/blog/this-surprising-cold-email-cta-will-help-you-book-a-lot-more-meetings); V-data).
   - Gong and 30MPC (85M emails): an offer gives +28% reply, a meeting request −44%, pitching up to −57%, offering case studies up to −47% ([30MPC](https://www.30mpc.com/newsletter/the-data-backed-cold-email-formula-the-exact-words-length); [Gong guide](https://www.gong.io/resources/guides/how-to-master-cold-email-get-the-data-backed-guide-based-on-85-million-emails)). Hunter 2026: 65% of decision makers say cold emails are "too pushy".
   - **Spill:** a soft interest or offer ask in every email, including email 1 (for example "want the 1-page cost comparison against your insurer EAP?").
3. **Timing signals and triggers** (moderate for job changes, weak otherwise).
   - UserGems: among 40,000 Manager+ prospects, contact within 30 days of a new job converted 3x better; champions who changed jobs gave 114% higher win rates ([link](https://www.usergems.com/product/contact-tracking); V-data).
   - A first HR or People hire opens a roughly 90-day buying window (practitioner consensus, e.g. [Artisan](https://www.artisan.co/blog/how-to-use-first-hire-in-department-signals-for-outbound); V-claim).
   - Mental-health triggers are supported for the *need*, not for reply lift: job insecurity significantly affects 54% of US workers' stress (APA 2025, [link](https://www.apa.org/pubs/reports/work-in-america/2025); I).
4. **Relevance over first-line personalization** (moderate). Hunter: human-edited emails beat fully automated ones by 18% (5.2% vs 4.4%), and 69% of decision makers dislike AI-written email "unless it feels genuinely human". Saleshandy: hybrid AI-plus-human produced 1.7x the positives of human-only and 5x those of AI-only; AI-only copy was spam-flagged at 7.8% against 2.9%. A specific, verifiable fact tied to a plausible problem beats a flattering first line.
5. **Follow-up count and spacing** (moderate). Four to seven touches is the consensus (Instantly; Saleshandy 4–6 follow-ups over 20–21 days), with early gaps of 2–3 working days (Belkins; Instantly cites +31% for 3 days over 24 hours, [link](https://instantly.ai/blog/how-many-times-should-you-really-follow-up-with-a-prospect/); V-claim). A breakup email adds +89% reply ([30MPC](https://www.30mpc.com/newsletter/the-data-backed-blueprint-for-multi-touch-prospecting)). **Spill:** 7-day gaps and 4 touches sit at the slow, short end.
6. **Email length** (moderate). Gong and 30MPC: replies drop sharply above 100 words; the best run 50–100 words (3–4 sentences). Instantly: elite first touches stay under 80 words. **Spill:** the long-form explainer in email 2 contradicts every dataset; offer it on request instead.
7. **Plain text versus HTML, links and images in email 1** (moderate mechanism, weak magnitude). HubSpot's A/B tests: HTML and images cut opens (a GIF by 37%) and plain text won on clicks ([link](https://blog.hubspot.com/marketing/plain-text-vs-html-emails-data); V-data, older marketing email). Instantly's own guide: plain text, no links, no open tracking in the first email ([link](https://instantly.ai/blog/how-to-achieve-90-cold-email-deliverability-in-2025/)). **Spill:** HTML plus an industry-page link in email 1 is the main deliverability risk in the current setup.
8. **Open and click tracking** (moderate). Snov.io (44M emails, February–March 2025): turning open tracking off raised reply from 1.08% to 2.36% ([link](https://snov.io/blog/we-analyzed-44-million-emails/); V-data, confounded). Belkins dropped the pixel in 2025 and saw 3% higher reply. Opens are unreliable anyway (Apple Mail generates 49–65% of tracked opens), and security scanners auto-click links, so 20–80% of B2B "clicks" may be bots ([example](https://emailcalculator.com/blog/bot-clicks-are-lying-to-you); V-claim).
9. **Subject lines** (moderate on opens, weak on replies). Gong: 1–4 words, lowercase; an empty subject raises opens 30% but cuts replies 12% ([30MPC](https://www.30mpc.com/newsletter/4-data-backed-subject-lines-to-get-your-cold-emails-opened)). Belkins: personalized subjects lift reply from 3% to 7%. Subject lines rarely move replies by more than about 20%.
10. **Days and times** (moderate, small effect of about 10–20%). Belkins 2026: Wednesday and Thursday reply most (0.48%); 8am–noon replies best (0.54%) and books meetings at 4x other windows ([link](https://belkins.io/blog/best-time-to-send-email)). Instantly: Wednesday peaks, Friday brings an OOO surge. Send in the recipient's time zone.
11. **Sender persona** (weak). No large public study isolates the sender's title. On the recipient side, founders and owners reply most (Belkins) and C-level recipients are 30.2% *less* likely to reply (Gong, [link](https://www.gong.io/blog/do-execs-really-reply-to-cold-email-here-s-what-the-data-says)). Founder-to-founder email for 10–49-employee accounts is a hypothesis to test.

---

## 3. Deliverability in 2025–26

**Mailbox-provider rules [I]**
- **Google** ([sender guidelines](https://support.google.com/a/answer/81126); [FAQ](https://support.google.com/mail/answer/14229414)). All senders: SPF *or* DKIM, valid PTR, TLS, and user-reported spam below 0.3% (target under 0.1%). Bulk senders (about 5,000+ a day to personal Gmail) also need aligned SPF, DKIM and DMARC, plus RFC 8058 one-click unsubscribe on marketing mail, honored within 48 hours. Above 0.3% spam, a sender loses mitigation; enforcement moved to SMTP rejections in November 2025 ([Red Sift](https://redsift.com/blog/gmails-enforcement-ramps-up-what-bulk-senders-need-to-know)). Postmaster Tools v2 replaced reputation scores with a pass/fail compliance view ([Folderly](https://folderly.com/blog/google-postmaster-tools-v2-migration-guide-2025)).
- **Yahoo** ([senders.yahooinc.com](https://senders.yahooinc.com/best-practices/), February 2024): the same spam thresholds; bulk senders need SPF, DKIM and DMARC (p=none or stronger) and one-click unsubscribe honored within 2 days.
- **Microsoft Outlook.com** ([Tech Community](https://techcommunity.microsoft.com/blog/microsoftdefenderforoffice365blog/strengthening-email-ecosystem-outlook%E2%80%99s-new-requirements-for-high%E2%80%90volume-senders/4399730), effective 5 May 2025): domains sending more than 5,000 a day to consumer Outlook, Hotmail and Live need SPF, DKIM and DMARC (at least p=none, aligned). Non-compliant mail goes to Junk first, then is rejected with "550 5.7.515".
- **US law:** CAN-SPAM requires an opt-out, a valid postal address and opt-out processing within 10 business days. It does not require opt-in for B2B email ([FTC](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business)).

**What applies to Spill.** At about 120 emails a day, mostly to Workspace and Microsoft 365 business domains, Spill is far below the bulk thresholds, but filters use the same signals (complaints, authentication, engagement). RFC 8058 one-click unsubscribe ([RFC](https://www.rfc-editor.org/rfc/rfc8058.html)) isn't mandatory at this volume, but it is cheap insurance: a native unsubscribe replaces a spam-button press. Add the List-Unsubscribe header and a plain-text "reply 'no' and I'll stop" line.

**Placement reality.** Validity 2025: inbox placement 87.2% at Gmail and 75.6% at Microsoft, with 14.6% Microsoft spam placement ([PDF](https://www.validity.com/wp-content/uploads/2025/03/2025-Benchmark-Report-FINAL.pdf), March 2025; I-ish, marketing mail). GlockApps Q1 2025 seeds: Outlook/Hotmail 26.8% inbox, Office 365 down from 77% to 51% year on year (via [Digital Bloom](https://thedigitalbloom.com/learn/b2b-email-deliverability-benchmarks-2025/); V-data, seed-based). Saleshandy: Microsoft is hardest on every metric. **Action:** split all reporting by recipient mailbox provider (MX lookup); if Microsoft reply rates are under half of Google's, placement, not copy, is the problem.

**Sends per mailbox and infrastructure.** Instantly recommends 30 campaign plus about 10 warmup emails per account per day, with slow warmup on ([help center](https://help.instantly.ai/en/articles/6248612-account-and-campaign-limits); V-data); some vendors allow 50–100 (Smartlead), but 30 is the conservative consensus. Run 2–3 mailboxes per lookalike secondary domain redirecting to spill.chat ([EmailBison](https://emailbison.com/blogs/cold-email-secondary-domains); V-claim). **Spill:** today's volume fills 4 mailboxes; two contacts per account needs about 8–10 on 4–5 domains, and three contacts or 5–6 touches needs 12–15.

**Other practices (moderate to weak evidence):**
- **Warmup** helps ramping and maintenance, but practitioners report providers now discount pool-based warmup engagement ([LiteMail](https://litemail.ai/blog/does-email-warmup-work-2026); V-claim, no provider confirmation found). Real replies matter more.
- **Seed tests** (GlockApps, 70–115 seeds) only observe seeds with no engagement history, perhaps 5–10% off real placement ([Suped](https://www.suped.com/learn/email-deliverability/how-reliable-are-glockapps-deliverability-placement-reports-and-what-are-the-alternatives)). Use them as a pre-launch smoke test; reply rate by provider, mailbox and step is the real monitor.
- **Custom tracking domain:** if links are tracked at all, use a branded CNAME (the "up to 20%" lift is a V-claim). Better: no tracked links in email 1.
- **Bounces** under 2% (Outreach average 2.8%); have Clay exclude catch-all and risky addresses from email 1.

**Strength of evidence (section 3):** strong on the rules; moderate on the practices.

---

## 4. Multi-threading and multichannel

**Multi-threading** (moderate). Hunter, 25,000 campaigns ([link](https://hunter.io/blog/should-you-email-more-than-one-person-per-company); V-data): accounts with 2+ people contacted replied at 3.35% against 1.61% for one (2.1x account-level); different departments beat the same department (2.30% vs 1.65%); going down the org chart doubled replies against going straight to the top; each extra email is individually less effective. Hunter's conclusion: multi-thread when accounts are scarce, as in a defined SMB universe. SMB buying groups are about 3–5 people, often overlapping roles at 10–49 employees ([summary](https://tractioncomplete.com/articles/mapping-the-b2b-buying-committee/); V-claim). **Spill:** pair the founder with the people or ops lead, staggered about a week apart with different angles. Expect about 1.4–2x per account and 1.0–1.2x per email.

**Phone** (moderate).
- Gong (300M+ calls): reps who also call see email reply of 3.44% against 1.81%, even without connecting; voicemails raised email reply from 2.73% to 5.87% ([link](https://www.gong.io/blog/the-hidden-power-of-cold-calling-insights-from-300m-calls); V-data).
- Belkins 2025 (175k+ calls): 9.9% connect per dial, 24.5% per prospect after repeat attempts, about 370 dials per meeting; calls produced 33.6% of its 2025 appointments ([link](https://belkins.io/b2b-outreach)). Orum (1.5M calls, 2023): 4.6–5.5% connect; Nooks: mobiles pick up 45% more ([summary](https://saleshive.com/blog/b2b-sales-cold-calling-benchmarks-teams-2025)).
- Instantly's cost per meeting ($153 email, $2,778 calling) has no stated method ([link](https://instantly.ai/blog/email-sequence-benchmarks-2026-whats-a-good-open-rate-reply-rate-and-cost-per-meeting/); V-claim). **Spill:** at $195/month, call only high-signal accounts with mobile numbers.

**LinkedIn** (weak to moderate). Expandi (13.2M requests, 6.7M messages): 28.5% acceptance and 10.4% message reply; three messages perform best and 5+ perform worse than one; connection-note replies fell from 3.5% to 2.2% between May 2025 and April 2026 ([link](https://expandi.io/blog/linkedin-outreach-benchmarks-2026/)). Belkins: 7%+ LinkedIn reply through 2025. Hunter's survey: 50.5% of decision makers prefer LinkedIn outreach against 25% for email. Combined-channel lift claims of 2–3.5x are vendor-only (La Growth Machine, Landbase).

**Retargeting ads** (weak evidence, low priority). LinkedIn needs matched audiences of at least 300 members and recommends company lists of 1,000+ ([LinkedIn Help](https://www.linkedin.com/help/lms/answer/a423102); I). The only evidence of lift is platform marketing (LinkedIn's pilot claims 3x conversion).

**Direct mail** (weak). The ANA/DMA cold-list response figure of about 4.4% is old and consumer-heavy, and per-piece cost doesn't fit a $195/month product outside top-tier accounts.

**Website visitor de-anonymization.** Realistic match rates are 30–65% at company level and 5–20% at person level ([Warmly](https://www.warmly.ai/p/blog/visitor-identification-match-rates); V-data); RB2B claims 15–45% at person level. More than 1,500 California CIPA pixel suits have been filed since 2022, with $5,000 statutory damages ([ABA](https://www.americanbar.org/groups/business_law/resources/business-law-today/2024-august/californias-invasion-privacy-act/)). **Spill:** use company-level identification plus HubSpot form and known-contact activity, and treat email clicks with suspicion (scanner bots).

---

## 5. Signal-based and AI-researched personalization; the AI-SDR backlash

**Evidence that per-account research beats templates** (all vendor): Woodpecker about 2x; Hunter +56% for two personalized attributes and +18% for human edits; Outreach 2x for customized emails; Saleshandy's 12k test 7.3% positive for hybrid, 4.2% human-only, 1.4% AI-only; Clay's automated 4-step campaign 5.1% positive. Direction consistent, magnitudes vendor-sourced.

**Backlash.** TechCrunch (March 2025) reported alleged 70–80% churn and inflated customer claims at 11x, and Artisan's CEO conceded first-generation AI SDRs had a "pretty low response rate" and "relatively high churn" (via [Salesmotion](https://salesmotion.io/blog/turns-out-ai-sdrs-are-too-good-to-be-true-11x-might-face-legal-action); I via secondary). Gartner: 61% of buyers prefer a rep-free experience, 67% in a 2026 follow-up ([Digital Commerce 360](https://www.digitalcommerce360.com/2026/03/17/gartner-b2b-buyers-rep-free-purchasing-ai-reshapes-sales/); I). Hunter: 69% of decision makers dislike detectably AI-written email. Buyers now ignore fake-familiar openers ("saw your post…"), "hope this finds you well", flattery, generic industry claims, long AI-sounding paragraphs and obvious merge fields (practitioner consensus; weak).

**Research inputs for Claude** (observe a fact, then infer the problem):
- careers page and job posts: hiring velocity, a first HR or People role, and whether "EAP" is already a listed benefit (then lead with "most insurer EAPs see 2–5% use");
- Glassdoor or Indeed reviews mentioning burnout, workload or management;
- news: funding, acquisitions, layoffs, relocation;
- LinkedIn: founder posts on culture; remote or distributed team (Slack/Teams fit);
- headcount crossing 15, 20 or 50 (HR formalizes; hypothesis).

**Guardrail:** never reference sensitive incidents (deaths, mental-health events); tone mistakes cost more in this category.

---

## 6. Reply handling

**Speed to lead.** HBR audited 2,241 US firms: responding within an hour made qualifying the lead about 7x more likely than an hour later, and more than 60x more likely than after 24h+; the average response took 42 hours ([HBR, March 2011](https://hbr.org/2011/03/the-short-life-of-online-sales-leads); I). The "5 minutes beats 30 by 21x" figure is the 2007 MIT and InsideSales phone study ([summary](https://www.expertise.ai/stats/speed-to-lead-statistics)). Both cover inbound leads, so applying them to cold-email positives is an inference. **Spill:** a 1-hour business-hours SLA on positives (Claude drafts, a human sends), then *specific* time slots, which Gong found best inside an active conversation (37% vs 25%).

**Playbooks (Claude classification categories):**
- **Referral ("not me"):** thank them, ask who owns benefits or wellbeing, cc the referrer where possible (Hunter's multi-department data supports routing sideways).
- **"We already have an EAP through our insurer":** the dominant objection (section 8). Agree, share the utilization gap, position Spill as a complement or replacement, ask for the renewal month.
- **"Not now":** capture a date and reason (renewal, budget, hiring freeze); nurture monthly with one useful asset; re-trigger on new signals. No data found on not-now conversion (gap).
- **Out of office:** parse the return date and resume 2 business days after it (Instantly reports a Friday OOO surge).
- **Negative or unsubscribe:** suppress immediately (CAN-SPAM allows 10 business days; Google and Yahoo require 2 days for bulk senders).

---

## 7. Experimentation at low volume

**Sample-size math** (two-sided α = 0.05, 80% power, computed for this report):

| Test (per lead) | Leads per arm | Weeks at 75 leads/arm/week |
|---|---|---|
| Positive reply 1.0% → 2.0% (2x) | ~2,320 | ~31 |
| Positive reply 1.0% → 1.5% | ~7,750 | ~103 |
| Total reply 3% → 6% (2x) | ~750 | ~10 |
| Total reply 3% → 4.5% | ~2,520 | ~34 |

At 150 accounts a week, Spill cannot detect subject-line-sized effects. Vendor guides reach the same conclusion: "below about 1,000 per variant, expect noise" ([Unify](https://www.unifygtm.com/explore/cold-email-ab-testing-statistical-rigor); V-claim).

**How small teams learn fast:**
1. **Test only big swings** (segment or trigger, offer or CTA, sender, channel), never wording.
2. **Use a leading metric:** total and "non-negative" replies weekly; confirm positive-rate wins over quarters.
3. **Use Thompson-sampling (Bayesian bandit) allocation** across 2–4 arms, shifting traffic once an arm's probability of being best passes 80–90% ([Agrawal & Goyal 2012](http://proceedings.mlr.press/v23/agrawal12/agrawal12.pdf)). Replies arrive over 21 days, so update in weekly batches and keep a 10–20% exploration floor.
4. **Read every reply:** have Claude tag objection, trigger mentioned and persona, turning qualitative signal into data.
5. **Watch health indicators:** bounces under 2%; reply rate by mailbox and provider; negative and unsubscribe rate (stop a variant above about 1%); step-level reply share. Ignore opens.

---

## 8. Buyer specifics: US SMB buyers of EAP and mental-health support

**Market facts**
- **The incumbent is usually a "free" EAP.** 83% of employers offer an EAP (SHRM, [2025 benefits survey](https://www.shrm.org/content/dam/en/shrm/topics-tools/research/employee-benefits/2025_annual_benefits_survey_executive_summary.pdf); I); 59% of private-industry workers had access in March 2025, lower in small establishments ([BLS](https://www.bls.gov/ebs/publications/employee-benefits-in-the-united-states-march-2025.htm); I). Carriers bundle EAPs with life and disability lines (Sun Life with ComPsych, Guardian, Symetra; [Sun Life](https://www.sunlife.com/us/en/employers/products-and-services/employee-assistance-program/)).
- **Use and awareness are low.** Embedded EAP use is about 2–3% and standalone about 5.5% (widely repeated V-claim, no clear primary). SHRM 2025: about 70% of workers are unaware (32%) or only somewhat aware (35%) of their mental-health resources, and HR confidence in their organization's support fell from 70% to 65% ([SHRM](https://www.shrm.org/topics-tools/news/benefits-compensation/what-to-know-about-the-state-of-workplace-mental-health); I).
- **Brokers and PEOs gatekeep.** LIMRA: 79% of employers use brokers or advisers to pick benefits ([Benefit News](https://www.benefitnews.com/advisers/news/limra-shows-employer-reliance-on-brokers-and-advisers); I). EBRI and Morgan Health: about three-quarters would be more likely to adopt an arrangement their broker or a peer recommended. NAPEO: about 233,000 SMBs (5.4M employees, about 15% of employers with 10–499 staff) use a PEO, and PEOs such as TriNet and Insperity bundle an EAP ([NAPEO](https://napeo.org/intro-to-peos/industry-overview/); I).

**Timing.** Benefits decisions follow the plan year, most often 1 January; open enrollment runs 30–60 days before, and brokers prepare 8–12 weeks before that, so 1 January plans are decided in September–October ([Word & Brown](https://brokerblog.wordandbrown.com/sales-marketing-tips/small-business-open-enrollment-checklist); V-claim). No good data on SMB Q4 budget timing (gap). Spill isn't insurance and can be bought any month, but "before your renewal" and "new-year budget" are natural *why now* hooks; ask for the renewal month in every "not now".

**Personas.** At 10–49 employees: founder or CEO plus an office or ops manager (33.8% of SMB-targeting ICPs name the CEO or founder, per Belkins' [buying-committee study](https://belkins.io/blog/b2b-buying-committee-study)). At 50–249: the first HR or People lead or Head of People, sometimes the COO or CFO, with the broker as influencer.

**Objections and responses:** "already have an EAP via our insurer" (low use, slow access, no Slack/Teams entry; position Spill as a complement); "too small" ($195/month flat for the whole team); "people won't use it" (same-day sessions, in-chat access); "privacy" (confidentiality, anonymized reporting); "our health plan covers therapy" (waitlists, deductibles).

**What makes them respond (inferred):** a visible current event, peer proof from a similar-size company, a transparent price, no broker paperwork, and a soft ask.

**Competitors' SMB motion and positioning**

| Player | SMB offer / price | Motion |
|---|---|---|
| Talkspace for SMBs ([link](https://business.talkspace.com/smb)) | Employers under 100 employees; from $500/month for 25+ employees ($20 PEPM) | Self-serve, "no salesperson" |
| Headspace Core for Small Business ([link](https://organizations.headspace.com/small-business)) | $69.99 per employee per year, sliding to $45; positioned as "EAP replacement or supplement"; self-guided content | Self-serve checkout |
| Calm Business Teams ([link](https://health.calm.com/calm-for-organizations/)) | 5–100 people (also "organizations under 300"); about $31–58 per user per year | Self-serve |
| Lyra Health | Enterprise. Since 30 October 2025, employers with 50+ staff reach it through the ParetoHealth captive (3,500 employers) ([PR](https://www.prnewswire.com/news-releases/lyra-health-joins-the-paretohealth-ecosystem-to-bring-mental-health-care-to-small-and-midsize-employers-302599272.html)) | Channel (captive or broker) |
| Spring Health ([link](https://www.springhealth.com/eap)) | Mid-market and enterprise; "EAP+" positioning; claims up to 25% utilization and under 1 day to an appointment (V-claim) | Broker and consultant marketing |
| Modern Health | FlexEAP as an EAP replacement; about $4–12 PEPM ([Vendr](https://www.vendr.com/marketplace/modern-health); V-claim) | Enterprise sales and consultants |
| BetterHelp for Business | Custom quotes; 5+ employees | Sales-led |
| Insurer and ancillary EAPs (ComPsych etc.) | "Free" with bundled lines | Through the broker |

No public evidence was found of competitors running dedicated SMB cold-email programs: SMB competition is self-serve or channel-led. That is whitespace for Spill's outbound, and a sign that brokers and PEOs are the gatekeepers. Spill works out at $7.80 PEPM for 25 people and $1.95 for 100, under Talkspace's $500/month floor.

---

## Top 12 levers by expected impact on positive reply rate

The multipliers are rough, apply per lead unless stated, and overlap, so don't multiply them together. Stacking levers 1–5 credibly reaches **2–3x**.

| # | Lever | Expected multiplier | Key evidence |
|---|---|---|---|
| 1 | **Signal-led targeting and prioritization:** new HR or People hire, first HR role posted, headcount thresholds, funding, layoffs, burnout reviews, renewal month | 1.5–2.5x on the signal cohort (1.2–1.6x blended) | UserGems 3x within 30 days of a new job; Instantly 15–25% (discounted); Hunter segment-size data |
| 2 | **CTA and offer redesign:** interest or offer ask in every email; calendar asks only after interest; a lead-magnet offer (insurer-EAP comparison, industry burnout snapshot) | 1.3–1.8x | Gong 304k (2x meetings); Gong/30MPC offer +28%, meeting ask −44%, pitching −57% |
| 3 | **Deliverability hygiene on email 1:** plain text, no link, no pixel, open tracking off, List-Unsubscribe header | 1.2–1.6x (up to 2x if Microsoft placement is poor today) | Snov.io 2.2x (confounded); Belkins +3% without pixel; Validity Microsoft 75.6%; Instantly's own guide |
| 4 | **Researched "why you, why now"** with human-edited Claude drafts | 1.3–2x | Hunter +56% and +18%; Woodpecker about 2x; Saleshandy hybrid 1.7x human-only and 5x AI-only positives |
| 5 | **Narrow 108 industries to 6–10 segments** with segment-specific proof (also makes learning possible) | 1.2–1.5x | Hunter small campaigns 2.8x; Saleshandy about 2x; Gartner 73% avoid irrelevant outreach |
| 6 | **Multi-thread two contacts per account** in different functions (needs about 2x the mailboxes) | 1.4–2x per account (1.0–1.2x per email) | Hunter 25k campaigns, 3.35% vs 1.61% |
| 7 | **Phone and voicemail on top-signal accounts** | 1.5–2x on the called cohort | Gong 300M calls (email reply 3.44% vs 1.81%; voicemail 5.87% vs 2.73%); Belkins calls 33.6% of meetings |
| 8 | **Every email 50–100 words;** turn email 2's explainer into an "I can send it" offer | 1.1–1.4x | Gong/30MPC length data; pitching −57%; Hunter "too pushy" 65% |
| 9 | **Re-cadence to 5–6 touches,** 2–4 day early gaps, a breakup email | 1.1–1.3x | Saleshandy 44% of positives from follow-ups; Belkins 53% of meetings from step 3+; breakup +89% |
| 10 | **Reply operations:** 1-hour SLA, specific slots after interest, referral, OOO and not-now plays | 1.1–1.3x demos per positive, plus 5–15% more positives recovered | HBR 7x within an hour; Gong specific CTA 37% in active threads |
| 11 | **Light LinkedIn touches** from the sender (profile view, connection, no pitch) | 1.1–1.3x | Expandi HR 30.5% acceptance; 50.5% of decision makers prefer LinkedIn; lift data vendor-only |
| 12 | **Sender persona and timing:** founder-signed email for 10–49-employee accounts; Tuesday–Thursday, 8am–noon recipient time | 1.0–1.2x | Belkins (founders reply most; morning 4x meeting rate); no direct sender-title study (hypothesis) |

**Prerequisite, not a lever:** 8–15 mailboxes on 4–6 secondary domains at 30 sends a day, so that levers 6, 9 and 11 don't push existing inboxes past safe volume.
