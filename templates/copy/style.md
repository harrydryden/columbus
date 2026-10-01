# How a US Outbound sequence is written

One row of the Copy tab is one four-email sequence for one industry (and, optionally, one role).
Claude drafts it from this guide, facts.md and the industry's page; a second model checks it; Harry
approves it. The render-time check (enrol/copy_rules.py, enrol/copy_markup.py) enforces the hard rules
on every email before it is sent.

## Voice

- Warm, plain and direct, like a note from one busy professional to another. First person ("I", "we").
- Specific to the industry: use its own words and pressures (busy season, shift handovers, census,
  caseloads, launch weeks, grant cycles). A reader in that industry should feel it was written for them.
- Specific to the role through the role line (below), never by guessing about the reader's company.
- Not salesy: no hype, no exclamation marks, no buzzwords (revolutionize, game-changer, unlock,
  leverage, synergy, cutting-edge, world-class), no fake urgency, no guilt, no fear.
- Short sentences. Short paragraphs: one to three sentences, never a line over 300 characters.
- American English and US grammar: counseling, organization, well-being, program, analyze.
- Sentence-case subjects ("Busy season support for {{company}}"), under 50 characters, no "!",
  no "Re:" or "Fwd:", no emoji.

## The four emails

Every email opens "Hi {{first_name}}," and ends with "Best wishes," and "{{sender_first_name}}" on its own
line (Harry, 1 Oct 2026). Email 1 asks for nothing: it is about the reader's industry and links its
page, [see how Spill works for CPA firms]({{industry_url}}) (Spill's US site when the industry has no
page yet); a demo ask that early is too presumptive (Harry, 1 Oct 2026). Emails 2 to 4 each have exactly
one call to action, a link to book a demo: [book a short demo]({{demo_url}}) or similar anchor text.
Email 2 also links the site in "What is Spill?" ({{site_url}}). Never "with me", "my calendar" or "a time with me": the demo page books
the right person whoever sends the email. The footer, opt-out and privacy notice are added by the
system; never write them.

1. **Day 0, the hook (60 to 110 words).** "{{opener}}" alone on the line after the greeting (it is
   filled with evidence about the account when there is some, and disappears otherwise, so the email
   must read well without it). Then one or two sentences on a real pressure in this industry, from its
   page. Then "{{role_line}}" alone on its own line. Then one sentence on how Spill helps with that
   pressure, and a sentence linking the industry page. No demo ask.
2. **Day 7, the long form (180 to 280 words).** Harry's explainer, tailored to the industry. A short
   bridge line, then three headed sections, then the call to action:
   - **What is Spill?** what it is and what it does for the organization, in this industry's terms.
   - **Who is Spill for?** who on this kind of team it helps and with what, from the page and facts.md.
   - **What makes Spill unique** four bullets: same-day support in a couple of clicks with no waiting
     lists; one or two industry-specific points (out-of-hours sessions for shift workers, sessions
     around client work, confidentiality from partners); the tools it works through; and
     "{{price_line}} We don't lock you in."
3. **Day 14, a new angle (50 to 90 words).** One different reason, often from the page's FAQs:
   confidentiality, manager support, out-of-hours access, setup in hours, working alongside an EAP.
   A short question is fine. Call to action.
4. **Day 21, the close (40 to 80 words).** A polite last note: no guilt, leave the door open, one
   sentence of value, and the call to action one last time.

## Role lines

Each row has three role lines, one sentence each (under 30 words), shown in email 1 by the contact's
role. Each speaks to that role's stake in this industry and must read naturally after the hook:

- **People leader** (Head of People, HR Director): uptake and experience, being the person everyone
  comes to, retention, a benefit that is easy to roll out and to justify.
- **Founder or executive** (CEO, founder, managing partner, executive director): keeping key people,
  steady output, culture, a lean team with little or no HR.
- **Operations** (COO, office manager, firm administrator): absence and cover, shifts and scheduling,
  simple admin, a predictable cost.

## Markup

The Copy tab is written in a small markup the system turns into an email:

- A blank line starts a new paragraph; a single line break stays a line break.
- A line starting "- " is a bullet; a list needs at least two.
- **Bold** for the three headings of email 2, nothing else.
- [anchor text](link) for links. Links go only to {{demo_url}}, {{industry_url}}, {{site_url}} or a page
  on https://www.spill.chat. No bare URLs, no "click here".
- Variables in double braces: {{first_name}}, {{company}}, {{place}}, {{opener}}, {{role_line}},
  {{price_line}}, {{demo_url}}, {{industry_url}}, {{site_url}}, {{sender_first_name}}, {{legal_overlay}}.
  No other braces, no HTML.

## Harry's long-form email (the model for email 2)

Hi {{first_name}},

In case it's useful, here's a short overview of Spill.

**What is Spill?**
Spill is an on-demand counseling service, [trusted by over 50,000 employees]({{site_url}}). We help organizations increase staff productivity, reduce absenteeism and free up HR time by addressing the issues that most often derail performance at work.
With Spill, employees get fast, easy access to professional counseling, and managers get the tools they need to support anyone on their team who's struggling.

**Who is Spill for?**
Anyone on your team who's struggling with personal or professional issues that affect their well-being and job performance.
That could be work-related challenges (like stress or burnout), mental health conditions (like anxiety, depression or ADHD) or life events (like having a baby or losing someone close).

**What makes Spill unique**
- Employees can get support the same day, in just a couple of clicks. No waiting lists or callbacks.
- Sessions run early mornings, evenings and weekends, so support fits around work.
- We integrate with the tools you already use, like email, Slack and Microsoft Teams.
- {{price_line}} We don't lock you in.

To hear more and get a quote for your team, [book a short demo]({{demo_url}}).

Best wishes,
{{sender_first_name}}
