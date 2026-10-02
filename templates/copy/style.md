# How a US Outbound sequence is written

One row of the Copy tab is one four-email sequence for one industry and one role (below).
Claude drafts it from this guide, facts.md and the industry's page; a second model checks it; Harry
approves it. The render-time check (enrol/copy_rules.py, enrol/copy_markup.py) enforces the hard rules
on every email before it is sent.

## Voice

- Warm, plain and direct, like a note from one busy professional to another. First person ("I", "we").
- Specific to the industry: use its own words and pressures (busy season, shift handovers, census,
  caseloads, launch weeks, grant cycles). A reader in that industry should feel it was written for them.
- Specific to the role in every email (below), never by guessing about the reader's company.
- Not salesy: no hype, no exclamation marks, no buzzwords (revolutionize, game-changer, unlock,
  leverage, synergy, cutting-edge, world-class), no fake urgency, no guilt, no fear.
- Short sentences. Short paragraphs: one to three sentences, never a line over 300 characters.
- American English and US grammar: counseling, organization, well-being, program, analyze.
- Sentence-case subjects ("Busy season support for {{company}}"), under 50 characters, no "!",
  no "Re:" or "Fwd:", no emoji.

## Industry and role (Harry, 1 Oct 2026)

The key to a reply is relevance: every email is written for one industry AND one role, so a reader
in that role at that kind of organization feels it was written for them. One row of the Copy tab
is one industry (an Industries label, or a group) and one role:

- **People leader** (Head of People, HR Director, CHRO): the person everyone brings problems to; their
  stake is uptake, manager support, confidentiality, a benefit that is easy to roll out and to justify,
  and keeping people.
- **Founder or executive** (CEO, founder, owner, managing partner, executive director): their stake is
  keeping key people, steady output and culture, often with little or no HR; time is short, so get to
  the point.
- **Operations** (COO, head of operations, general manager): their stake is absence and cover, shifts
  and scheduling, simple admin and a predictable cost.

The role shapes every email: the hook, why it matters to them, which Spill facts lead, and how the
call to action is framed. Never guess about the reader's own company beyond the opener's evidence.
Rows with no role (General, and fallbacks) use the three role lines in email 1 instead.

## The four emails

Every email opens "Hi {{first_name}}," and ends with "Best wishes," and "{{sender_first_name}}" on its own
line (Harry, 1 Oct 2026). The signature, the data-source notice and the unsubscribe link are added by the
system after that; never write them.

1. **Day 0: inform and plant a seed (60 to 110 words; Harry, 1 Oct 2026).** Personal, relevant data and
   a hook, not a sales pitch. "{{opener}}" alone on the line after the greeting: it is filled at enrol
   time (Openers, below) with a line a signal about their company points to, what their site says, or
   the generic line, and disappears for the 30% holdout, so the email must read well without it. Then the hook: a
   sharp, true observation about a pressure in this industry as it lands on this role, from the
   industry's page. Then one or two sentences on how Spill helps with exactly that, for this role.
   Then one soft line that invites a visit to the industry page:
   [see how Spill works for CPA firms]({{industry_url}}) (Spill's US site when the industry has no page).
   That link is the only ask. No demo, no call, no meeting, no "worth a chat?", nothing that asks for
   their time: the soft approach lets the sequence run, and the goal is that they remember Spill when
   the next email arrives.
2. **Day 7: the long form (180 to 280 words).** Harry's explainer, tailored to the industry and the role.
   A bridge line for this role, then three headed sections, then the call to action:
   - **What is Spill?** what it is and what it does for the organization, in this industry's terms.
   - **Who is Spill for?** who on this kind of team it helps and with what, framed for this role.
   - **What makes Spill unique** four bullets: same-day support in a couple of clicks with no waiting
     lists; one or two points that matter to this role in this industry; the tools it works through;
     and "{{price_line}} We don't lock you in."
   Email 2 also links the site in "What is Spill?" ({{site_url}}). The call to action is a demo link:
   [book a short demo]({{demo_url}}) or similar anchor text.
3. **Day 14: a new angle for the role (50 to 90 words).** One different reason, chosen for the role:
   - People leader: confidentiality and uptake, manager training, working alongside an EAP, easy
     anonymized reporting.
   - Founder or executive: keeping key people, a lean team with little or no HR, setup in hours, no lock-in.
   - Operations: absence and cover, out-of-hours sessions around shifts, simple admin, a predictable cost.
   A short question is fine. One demo link as the call to action.
4. **Day 21: the close (40 to 80 words).** A polite last note: no guilt, leave the door open, one sentence
   of value for this role, then one sentence that alludes to the free trial before signing off, for
   example "If it helps, we can also start with a free trial for your team." Never give its length or
   terms. One demo link as the call to action.

Never "with me", "my calendar" or "a time with me" in emails 2 to 4: the demo page books the right
person whoever sends the email.

## Role lines (rows with no role only)

Rows with no role (General and group fallbacks) have three role lines, one sentence each (under 30
words), shown in email 1 by the contact's role. Each speaks to that role's stake in this industry and
must read naturally after the hook.

## Openers (Signals and General tabs; Harry, 2 Oct 2026)

The {{opener}} line comes from the Signals tab, not the Copy tab. Each signal has a line for each
copy role (opener_people, opener_founder, opener_ops), and New People leader also has opener_self,
for when the contact is the new leader. The system fills the line at enrol time with the account's
own stored facts (enrol/openers.py). A line with a token that has no fact is skipped, never guessed:
the next line in the cell is tried, then the signal's plain opener, then the generic line (below),
then none. 30% of accounts get no opener at all, so we can measure whether openers help.

**Signals are context, never the line** (Harry, 2 Oct 2026: "these are just signals"). For the hiring,
People, growth and funding signals (New People leader, First People hire, People role open, Hiring and
growth, and both funding rows), the signal tells us what the team is probably going through, and the
line speaks to the pressure that tends to bring, then to support through it:

- a lot of new people: onboarding, managers stretched, culture under strain;
- a first or open People role: whoever carries people issues carrying too much;
- a new People leader settling in: the first months, setting priorities. Their own line (opener_self)
  is warm without "congratulations on the new role", which would tell them we watched their start date;
- funding: a period of change, more on everyone's plate, priorities and routines shifting.

These lines never name what was observed: no hiring, recruiting, growth, growing, scaling, headcount,
funding or money; no job or posting titles; no counts. They are hedged ("often", "tends to", "usually")
and true whether or not the observation was, so they never read as surveillance. Use {company} at most
once, and only where it does not claim to know their team ("at Brightline, a lot is changing" does).

**Funding is a signal, never a line.** A line about the round reads as money grabbing, so the round only
tells us the team is likely changing fast, and the line speaks to what that brings for people. No opener
line, as written or as filled, may mention funding or money: funding, fund, raise, round, investors,
investment, capital, valuation, Series A, seed round, backed, IPO, money, cash, dollars or a dollar
figure. The check refuses it (copy_rules.money_violations; `copy check` names the line, and at enrol
time the line is passed over for the next). Email bodies are not held to this: nonprofit copy says
"funding cycles". {funding_stage} is retired: a line that still has it never fills.

**The generic line** (General tab: opener_generic_people, opener_generic_founder, opener_generic_ops,
then opener_generic). An account with no signal line gets it: the General angle, Control accounts among
it, and an account whose signal lines all fall through. Its theme is the ever-growing pressure in our
work and personal lives. It reads general first, so the industry hook after it can get specific, and
must not make the hook's point. Tokens: {company}, {city}. The optional "what they do" line comes first
when it is on (below).

The page-reader signals (EAP named, Mental health support listed, Wellbeing app or perk named,
Progressive benefits) follow the same rule (Harry, 2 Oct 2026: "rework the benefits-page lines the same
way"). The page tells us they already look after their people, and the line speaks to the pressure that
still finds people, at work and at home, and to help that's there when it does:

- mental health support listed: they already speak up for well-being; the hard weeks are where it counts;
- an EAP: support is in place; people reach for the help that feels quickest and most personal;
- a well-being app or perk: the everyday is looked after; some weeks need a person to talk to;
- progressive benefits: they care about balance; life at home still doesn't keep office hours.

They never echo the page: no mental health, counseling, EAP, employee assistance, app, perk, benefit,
stipend, time off, leave or sabbatical, and no provider. Never a word against what they offer: no "on
paper", no phone numbers or logins, no "actually use". An EAP is "alongside or instead, never
disparaging" (the Upgrade the EAP angle), and an app or perk complements counseling. {evidence},
{provider} and {page} stay for a line Harry writes on the sheet; the defaults use no token.

Every opener:

- One sentence, under about 20 words, warm and plain. It must read naturally as the first line
  after "Hi Dana," and before the industry hook, without repeating the hook's point or wording.
- No exclamation marks, no statistic, no "therapy", "therapist", "licensed" or "unlimited". No demo,
  call, meeting or booking words: email 1 asks only for a visit to the site.
- Tokens, in single braces: {company}, {city}, {open_roles}, {posting_title}, {people_title},
  {growth}, {evidence}, {provider}, {page} ("on its careers page", "on its benefits page" or "in its
  job postings"). "a {posting_title}" becomes "an" where the title needs it. The plain opener column
  takes {evidence} only. The context signals' default lines use none of them.
- The optional "what they do" line (General opener_focus_line, off until Harry turns on
  opener_focus) uses {focus}. That is a short lower-case phrase the task model takes from Apollo's
  description, like "payroll software for restaurants".

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
