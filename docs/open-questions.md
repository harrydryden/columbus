# Open questions for Harry

SPEC says: "If something is missing or ambiguous, stop and ask Harry rather than guess." Every item below either blocks progress or has a safe default already in the code, so the build is not stalled on any of them. To change a default, edit the sheet where the item lives there, or say which item and the code changes.

## A. Needed to finish Phase 0 (blocking)

1. **Privacy and opt-out page.** `/us/legals/privacy-notice` is still a Webflow draft, and there is no US opt-out page. Every email links to it (SPEC 10), and render blocks any send while `privacy_url` is blank. What is the live URL?
   *Deferred (Harry, 30 Sep): on the website; not needed yet. Render still blocks sends while `privacy_url` is blank, so this is needed before phase 2 goes live.*
2. **Postal address.** Please give Spill's UK registered address as it should appear in the footer (`postal_address`).
   *Deferred (Harry, 30 Sep): not needed yet; needed before phase 2 goes live.*
3. **Footer and Article 14 text.** Please approve or edit `templates/copy/footer.txt` and `templates/copy/article14.txt` (SPEC 14: "For Harry to approve"). The notice names Apollo and Clay as sources for every contact, and gives the lawful basis as legitimate interests.
   *Deferred (Harry, 30 Sep): not needed yet; needed before phase 2 goes live.*
4. **Instantly.** We have no connector or key in this session. Please add the API key to the sealed Railway variable `US_OUTBOUND_INSTANTLY_API_KEY` (docs/railway-setup.md, step c). The plan facts in SPEC 14 are still unconfirmed: plan tier, email and contact caps and current use, and whether the emails, reply, forward and accounts endpoints exist.
5. **Where the system runs.** Which project and region should hold Cloud Run, Scheduler, Secret Manager and Artifact Registry?
   *Answered (Harry, 30 Sep): Railway, on Spill's existing paid plan, in EU West (Amsterdam).*
   - One always-on worker runs the scheduler, which replaces Cloud Run Jobs and Cloud Scheduler.
   - Railway Postgres replaces BigQuery.
   - Sealed variables replace Secret Manager.
   - The Google project Columbus (`columbus-510209`) keeps only a Sheets service account.

   This departs from SPEC 1.7, 3 and 6; see docs/railway-setup.md.
6. **Google Cloud costs.** *No longer apply (30 Sep):* there is no Cloud Scheduler, Secret Manager or Artifact Registry, and the Sheets API is free with no billing account. Railway usage is about $3–5 a month on Spill's existing plan (docs/railway-setup.md, step g). New questions from the move:
   - **6a. Postgres backups.** Does Spill's Railway plan include volume backups, and do you want them? [Daily and Weekly on, if available; billed as volume storage]
   - **6b. Railway workspace.** Which Railway workspace should hold the project "Columbus", and who besides you should be a member? Members can open the non-sealed variables and the logs. [Spill's main workspace; you as Admin]
   - **6c. Service-account keys.** If the spill.chat organization blocks key creation (`iam.disableServiceAccountKeyCreation`), may an organization admin lift it for `columbus-510209` only, while the key is made? [yes, then restore it] If not, the jobs need another way into the sheet, and the code has to change.
   - **6d. Railway as a processor.** Prospect names and emails are stored in Railway's EU region. Should Railway go through Spill's DPA review before real data lands there (phase 1)? [yes, before phase 1]
   - **6e. Config in code.** Railway's `railway.json` and `railway.toml` are deprecated, so the worker's settings are checked by hand from a list in railway-setup.md. Do you want Railway's replacement, `.railway/railway.ts`? It needs Node and the Railway CLI to apply. [no; the dashboard and the list]
   - **6f. If the worker stops.** heartbeat_check runs inside the worker, so it cannot report that the worker is down. Railway's project webhooks can post deployment crashes to a Slack incoming webhook for #us-outbound-dev. This needs an incoming webhook in Slack (a separate app, or one added to ours). Shall I set it up? [yes, in phase 0]
7. **Clay.**
   - What are the plan tier and the credit pool? *Answered in part (30 Sep):* the jobs may use 2,000 Clay credits a month (`clay_monthly_credits`), paced by the day.
   - Is the Routines (function) API enabled for the workspace, with "API & CLI" ticked on the two functions? If not, we use the CSV fallback in SPEC 8.
   - The "US Outbound" folder and its two functions are built in Clay's UI.
8. **Settings sheet.** A sheet the service account creates is visible only to that account. Shall I create "US Outbound – Settings" in your Drive from the defaults, for you to share with the service account? Or would you rather create it yourself?
9. **Slack.** Please create the app from `deploy/slack-app-manifest.yaml`, then create #us-outbound and #us-outbound-dev and invite the bot. Neither channel exists yet.
10. **Apollo tracker.** In Apollo:
    - fix "us/pricing" → "/us/pricing"
    - add "/us/book-demo" as high intent
    - confirm data starts arriving (`data_received` is false)
    - confirm the script is not on employee-facing pages
11. **HubSpot service key.** Create it with the SPEC 13 scopes (phase0-runbook.md §5). HubSpot is retiring private apps, so a service key replaces the SPEC's private-app token.
12. **Clay in dry-run.** Should Clay functions run while `live_sending = no`? Phase 1 verifies the queue before any send, so they need to. The default is yes: Clay runs within the monthly budget in dry-run too, because it sends nothing to prospects.

## B. Decisions with a default in place

The default is in brackets. Items marked PHASE0-CONFIRM are checked against the real system during phase 0 setup.

### Settings sheet and signals

13. First People hire uses `people_leader_count`, which comes from apollo_people. The row lists "apollo_jobs, apollo_people". [both]
14. Terms match whole words, so "mental health day" does not match "mental health days". Should plurals be added to Progressive benefits? [SPEC terms as written]
15. Should the Nonprofits rows exclude NAICS 813110 (religious organizations), since churches are off? [not excluded]
16. Each of the 9 "Off" industry groups is one inactive row named after the group, until you add its website labels. [as described]
17. Please check the Apollo keyword chosen for each Tech label (e.g. software, agtech, video games). [as drafted]
18. Industries `landing_page_url` and `proof_point` are blank ("Harry to fill"). A blank proof point blocks step 2 for that group. [blank]
19. No default signal has an opener, so every account gets its angle's default opener. Should "EAP named" ship with "Saw your benefits page mentions {evidence}."? [blank]
20. Validation adds checks beyond SPEC. Are they OK? [all on]
    - daily_cap is at most 30
    - claude_monthly_cap_usd is at most 10
    - live needs approver ids
    - an empty tab is rejected, except Overrides and Tests
    - CA or WA set active is rejected
    - a running test needs approved copy
    - dev_channel must differ from alert_channel
21. Dates are typed as YYYY-MM-DD and shares as 0.15, not as formatted percentages. [yes]
22. Both copy versions share subjects, so the first A/B test isolates the opener. [yes]
23. What goes in the Copy tab's "sources" column? [blank]
24. `claude_model` is `claude-haiku-4-5` (SPEC: "a current fast Claude model"). Classification, drafts and readouts cost well under the $10 cap even on a larger model. [Haiku 4.5]

### Scoring and tiers

25. A negative total score is not floored at 0. [not floored]
26. While the latest Clay careers read is blocked or error, that source gives no points, even from an earlier good read that is still fresh. Hold, Exclude and Suppress signals still count. [as described]
27. When several signals suggest the chosen angle, the one with the largest weight sets the opener. [yes]
28. An unknown or inactive HQ state makes the account Excluded rather than Held. [Excluded]
29. The partner keyword "payroll" and NAICS 524 over-exclude a little: CPA firms that list payroll, and insurtechs. [keep, the safe direction]
30. Founded less than 2 years ago uses 1 January when only the year is known. [yes]
31. The tier-mix alert (5–40%) posts once per rescore, with its bounds in code. Should it post once a day, with the bounds on the General tab? [per rescore, in code]
32. While FL is off, the FL staff share is added to the CA/WA share before the 20% test. This is the stricter reading of SPEC 9. [added]
33. Signal matches and tiers are recomputed on every rescore, including for enrolled accounts. Should they freeze at enrollment, so the Monday signal table measures what was true at send time? [recomputed]

### Contacts and cleaning

34. Only EMEA and APAC titles are skipped, not APJ, LATAM, Europe, UK or Asia. [only EMEA and APAC]
35. RevOps counts as sales ops and is skipped. A title is skipped if a skip word appears anywhere in it. [yes]
36. The Roles tab has no CHRO, VP HR, Head of HR, Director of People, Owner or Principal, so those titles are never contacted. Should they be added to the sheet? [not added]
37. Company-name casing is kept as given, so "acme creative" stays lower case. [kept]

### Copy and rendering

38. The asks by role are gone: every email's call to action is the demo page (Harry, 30 Sep 2026). The role now shows in email 1's role line. [demo link]
39. Email 4 no longer offers the one-pager; it ends on the demo link too. Should one of the emails offer the one-pager instead? [no]
40. Must every email mention same-day counseling? [not enforced]
41. "therapy" and "therapist" are blocked even inside a quote from the prospect's page, and the opener then falls back to the default. [blocked]
42. Please review the phrase lists for disparaging an EAP and for EAP in Spill's name, in `enrol/copy_rules.py`. [short lists]
43. Statistics: any percentage except the 30% utilization claim is blocked, as are "3x", "2 in 3" and "500+ companies". Prices and durations are allowed. [yes]
44. "wellbeing" is blocked as British; use "well-being". [blocked]
45. The signature ends "spill.chat/us", which mail clients turn into a link, so it can't go in step 1. The footer already names the sender. [yes]

### Enrollment and sending

46. The running test takes version_a's angle accounts. Control fills any shortfall in Priority and Standard, and the other way round. [yes]
47. Weekly hand-check: enrollment waits until this week's hand-check item is marked handled, and pulled accounts are skipped. How do you want to approve it: a Slack thread reply, or the sheet? [a handled item in the database; the Slack approval flow comes with phase 2]
48. Your two addresses share one campaign. Leads use your first Active mailbox's signature. [yes]
49. While `hubspot_owner_id` is blank, the HubSpot re-check excludes any account that has an owner. [fails closed]
50. `mailbox add` joins the campaign only once the mailbox is Active. SPEC's campaign table sends from Active addresses only. [Active only]
51. A mailbox is warm at a warmup or health score of 90 or more, or after 21 days of warmup. [90, in code] (PHASE0-CONFIRM)
52. `unenrol --month` removes that month's Instantly leads but leaves their status in the database unchanged. [unchanged]
53. `stop` and `start` are recorded as heartbeat rows. `start` refuses while any campaign has drifted. [yes]
54. Operator commands that never reach a prospect (stop, mailbox, erase, setup) are live with `--live` alone. Jobs and `start` also need `live_sending = yes`. [as described]
55. Lead imports skip anyone already in any Instantly campaign, the EU ones included, and spend no Instantly verification credits. [yes]
56. The Instantly blocklist gets email addresses only, because a domain entry would also block EU campaigns. Domain suppression stays in the database. [emails only]

### Instantly, Apollo and HubSpot details (PHASE0-CONFIRM)

57. Instantly has no America/New_York in its time-zone list, so campaigns use America/Detroit (same rules). Check that a created campaign shows Mon–Fri and steps on days 0, 3, 8 and 15.
58. Instantly: confirm the custom-variable length limit (worst case: s2_body about 2,350 characters of HTML, s1_body about 1,950 with the longest opener), that HTML in a custom variable renders in a campaign with text_only off (email_format = html; text is the fallback), that the forward endpoint and is_evergreen behave as expected, and that step 2–4 sends stay on the step-1 address.
59. Apollo: `apollo_floor` and the monthly budget (`apollo_monthly_credits`, 2,000) count lead credits. Visitor discovery uses organization search with website-visitor filters (1 credit to confirm). bulk_match never runs the email waterfall; misses go to Clay.
60. HubSpot: confirm the deal→company and contact→company association ids and the v4 unsubscribe-all endpoint. Suppression loads hard bounces only. Company match is on the primary domain and its www. form.

### Data and operations

61. `contacts.enrolment_month` is STRING "YYYY-MM". v_signal_value flags a signal after 200 accounts with step 1 delivered. Site-visit events are logged for enrolled accounts only, as SPEC says. [yes]
62. Raw tables (including raw_clay_contacts, which holds names and emails) have no retention rule, but erase covers them. Should they follow the 12-month contacts rule? [kept]
63. `erase` GDPR-deletes the HubSpot contact whoever created it, lists a manual Clay step, and deletes database rows even in dry-run. [yes]
64. heartbeat_check alerts once when a job is newly missed and repeats at 09:00 UK. `test read` before the read date is labelled an early look. [yes]

### Copy by industry (Harry, 30 Sep 2026)

65. **The website and SPEC 10 disagree.** The industry pages say "licensed therapists" and quote statistics. SPEC 10 bans "licensed", "therapy" and "therapist", and allows no statistic but the 30% figure. The drafts follow SPEC 10: they say "professional counselors" and use no statistics. Keep SPEC 10's rules? [kept]
66. **"Unlimited".** Your long-form email says "Counseling sessions are unlimited", which SPEC 10 bans. That bullet became "Sessions run early mornings, evenings and weekends". Should the ban go? [kept]
67. **The price.** Three sources disagree:
    - the website: "Packages from $250 a month", "priced per employee per month";
    - your email: "Plans start from $195 per month";
    - SPEC 4's size table: $195 up to 10 staff, $250 to 25, $350 to 50, $495 to 100, $995 to 200, then $5 an employee.

    The emails use `{{price_line}}` from the table: "For a team your size it's $350 a month, on a rolling 30-day contract." When the size is unknown it reads "Plans start from $195 a month…". Which is current? [SPEC 4's table]
68. **"Trusted by tens of thousands of employees"** had a HubSpot-tracked link in your email, going somewhere unknown. Emails link only to the demo page, the industry page and spill.chat, with tracking off, so it has no link. Should it link a spill.chat page (reviews, customers)? [no link]
69. **Links in email 1.** SPEC 10 had step 1 carry one link only, the privacy page, for deliverability. Every email now carries the demo link, and email 2 carries the industry page. Watch spam placement in phase 2. [as you asked]
70. **The long form is email 2** (day 7), after a short hook on day 0, not email 1. [email 2]
71. **Sign-off.** Each email ends "Best," and the sender's first name; the footer then gives the full name. Would you prefer "Thanks," or no sign-off? [Best,]
72. **Page-only claims.** QA allows a claim that is on the industry's own page. The drafts use some to check are true for the US:
    - HIPAA compliant;
    - nothing reported to bar associations, boards or regulators;
    - booking by text;
    - sessions in several languages;
    - post-incident sessions;
    - manager training for team leads.

    [allowed; confirm]
73. **Pages with another page's copy on spill.chat.** These pages carry another page's intro and challenges:
    - the churches page on Animal welfare, Arts & culture, Environmental nonprofits, Human rights, and International aid & relief;
    - Social welfare on Emergency & rescue;
    - Automotive & vehicles on Packaging;
    - Private duty & live-in care on Supported living.

    Many sub-industry pages also share their group's wording. The emails for these industries describe their own pressures, and QA checked them. [worth fixing on the site]
74. **The first test** (t1, the EAP opener against the General opener) names copy versions that no longer exist. With copy by industry, it could test two versions of one industry's row, or the opener on and off. [planned; decide before phase 3]
75. **Role-specific copy** is a line per role in email 1. A row with `role` set (for example CPA firms for Operations) overrides it for that role, if you want to go further. [role lines]
76. **Industries not contacted** (Insurance, HR consulting, Substance use treatment: partners) are on the Industries tab, off, with no copy. [no copy]
