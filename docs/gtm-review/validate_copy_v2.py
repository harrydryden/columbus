"""Appendix B v3: no unchecked claims, no fake-familiar openers, T1 arm B = soft question with the link kept."""
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all
from us_outbound.settings.model import CopyStep
from us_outbound.copy import copy_markup
from us_outbound.enrol import render

ASK_TECH = "\n\nIs this on the list at {{company}} this year, or already covered?"
ASK_LEGAL = "\n\nIs this on the list for the firm this year, or already covered?"

TECH = {
 "copy_version": "technology-startups-v3", "industry": "Technology & Startups", "role": "", "status": "approved",
 "approved_by": "Harry", "qa": "", "qa_notes": "", "sources": "", "note": "",
 "s1_subject": "Counseling that lives in Slack",
 "s1_body": """Hi {{first_name}},

{{opener}}

Tech teams move fast and burn out quietly. Between layoff whiplash, always-on Slack and building against a runway, the strain usually stays hidden until someone strong gives notice.

{{role_line}}

Spill is on-demand counseling that anyone on the team can book from Slack or Teams, often for the same day. Here's [how Spill works for tech companies and startups]({{industry_url}}).

Best wishes,
{{sender_first_name}}""",
 "s2_subject": "What Spill does for tech teams",
 "s2_body": """Hi {{first_name}},

Following my note last week, here's the short version of Spill for tech companies and startups.

**What is Spill?**
Spill is an on-demand counseling service, [trusted by over 50,000 employees]({{site_url}}). It helps tech teams stay productive, cuts absenteeism and frees up whoever carries HR, by dealing with the issues that most often derail work.

**Who is Spill for?**
Anyone at the company: engineers, designers, sales and support, and the founders themselves. That might be burnout from a sprint that never ends, the strain after a reorg, imposter feelings, anxiety or ADHD, or life events like a new baby or losing someone close.

**What makes Spill unique**
- Support the same day, in a couple of clicks. No waiting lists, phone trees or referrals.
- Remote-native: video sessions across US time zones, early mornings, evenings and weekends.
- Booking lives in Slack and Microsoft Teams, as well as email and any phone.
- Managers get training and tools to support anyone on their team who's struggling.
- {{price_line}} We don't lock you in.

If you'd like to see it, [book a short demo]({{demo_url}}).

Best wishes,
{{sender_first_name}}""",
 "s3_subject": "Does Spill work alongside an EAP?",
 "s3_body": """Hi {{first_name}},

A quick one, since this comes up a lot.

If you already have an EAP through your carrier, Spill works alongside it. If you don't, it works on its own. Either way, the difference is where it lives: someone opens Slack or Teams, picks a time and talks to a counselor, often the same day. Founders are covered on the same plan as everyone else.

Would it help to see how that looks for a team like yours? [Book a quick demo]({{demo_url}}).

Best wishes,
{{sender_first_name}}""",
 "s4_subject": "Who looks after benefits at {{company}}?",
 "s4_body": """Hi {{first_name}},

Last note from me for now.

If someone else looks after benefits or people at {{company}}, could you point me to them? And if the timing's wrong, no problem; this will keep.

When it moves up the list, Spill can be live in Slack or Teams within hours. [See it in a short demo]({{demo_url}}) whenever works.

Best wishes,
{{sender_first_name}}""",
 "people_leader_line": "If you're the one people come to when things get hard, you want support they'll actually use and a rollout that doesn't eat your week.",
 "founder_line": "If you lead the team, keeping the people who carry the product costs far less than replacing them.",
 "operations_line": "If you run operations, it shows up as sick days and scrambled cover, so simple admin and a predictable cost matter.",
}
LEGAL = {
 "copy_version": "legal-teams-v3", "industry": "Legal Teams", "role": "", "status": "approved",
 "approved_by": "Harry", "qa": "", "qa_notes": "", "sources": "", "note": "",
 "s1_subject": "Confidential counseling for {{company}}",
 "s1_body": """Hi {{first_name}},

{{opener}}

In most law firms the billable hour sets the pace, client emergencies own the evenings, and asking for help can still feel like weakness. So people push on until they burn out or leave.

{{role_line}}

Spill is confidential counseling that attorneys and staff book privately from their own phones, often for the same day; the firm sees only anonymized, aggregate numbers. Here's [how Spill works for law firms]({{industry_url}}).

Best wishes,
{{sender_first_name}}""",
 "s2_subject": "What Spill does for law firms",
 "s2_body": """Hi {{first_name}},

Following my note last week, here's the short version of Spill for law firms.

**What is Spill?**
Spill is an on-demand counseling service, [trusted by over 50,000 employees]({{site_url}}). It helps firms keep people productive, reduce absence and free up partners' and HR time by dealing with the issues that most often derail work.

**Who is Spill for?**
Everyone at the firm: attorneys, paralegals and business staff, and the owners too. That could be billable-hour burnout, the strain of adversarial work, perfectionism, anxiety or depression, or life events like having a baby or losing someone close.
There's no minimum headcount, so a boutique gets the same support as a national firm.

**What makes Spill unique**
- Support the same day, in a couple of clicks. No waiting lists, callbacks or referrals.
- Sessions early mornings, evenings and weekends, so they fit around court schedules and client emergencies.
- People book privately, from their own phone or from Slack or Teams, and the firm sees only anonymized, aggregate data.
- Partners and managers get training and tools to support anyone who's struggling.
- {{price_line}} We don't lock you in.

To see how it would work at {{company}}, [book a short demo]({{demo_url}}).

Best wishes,
{{sender_first_name}}""",
 "s3_subject": "Who covers paralegals and staff?",
 "s3_body": """Hi {{first_name}},

The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?

They work the same deadlines and client emergencies as the attorneys they support. Spill covers everyone at the firm, booked privately from a personal device, often for the same day, with the owners covered too.

Is that a gap at {{company}}? If so, [book a quick demo]({{demo_url}}) and I'll walk you through it.

Best wishes,
{{sender_first_name}}""",
 "s4_subject": "Who handles benefits at {{company}}?",
 "s4_body": """Hi {{first_name}},

Last note from me for now.

If someone else handles benefits or staff well-being at {{company}}, could you point me their way? And if the timing's wrong, no problem; this will keep.

When it moves up the list, Spill can be set up in hours for a firm of any size. [See it in a short demo]({{demo_url}}) whenever works.

Best wishes,
{{sender_first_name}}""",
 "people_leader_line": "If you're the person attorneys and staff quietly come to, you want support they'll trust enough to use and a benefit that's simple to roll out.",
 "founder_line": "If you lead the firm, keeping good associates spares you the recruiting, ramp-up and lost billables that follow a departure.",
 "operations_line": "If you run operations, you see it as last-minute gaps around court dates and filings, so you want something simple to run at a predictable cost.",
}
# Arm B for a 20-49 founder (adapted from the red team's rewrite): one observed fact, no inference, link kept, soft question.
LEGAL_S1_FOUNDER_B = """Hi {{first_name}},

{{opener}}

In a firm your size there is usually no HR department behind you, so when someone is struggling it lands on whoever runs the place, and it stays there until a good person hands in their notice.

{{role_line}}

Spill is on-demand counseling anyone at the firm books privately from Slack, Teams or their own phone, often for the same day, with you covered too. Here's [how Spill works for law firms]({{industry_url}}).

Is this on the list at {{company}} this year, or already covered?

Best wishes,
{{sender_first_name}}"""

OPENERS = ["Your careers page lists an EAP through ComPsych.", "You're hiring for a People role.",
           "Congratulations on the recent round.", "Your careers page lists unlimited PTO."]

tabs = default_tabs()
for row in tabs["General"]:
    if row["key"] == "postal_address": row["value"] = "Spill, 1 Example Street, London EC1A 1AA, United Kingdom"
    if row["key"] == "privacy_url": row["value"] = "https://www.spill.chat/us/legals/privacy-notice"
tabs["Copy"] = [TECH, LEGAL]
settings, errors = validate_all(tabs)
assert settings is not None, {t: e for t, e in errors.items() if e}
hannah = next(m for m in settings.mailboxes if m.owner_name == "Hannah Spalding")
hannah = type(hannah)(**{**hannah.__dict__, "status": "Active"})

def body_words(row, step, values):
    r = copy_markup.render(row.step(step).body, values, optional=render.OPTIONAL_VARIABLES)
    lines = [l for l in r.words.split("\n") if l.strip()]
    counted = " ".join(lines[1:-2]) if len(lines) > 3 else " ".join(lines)
    n = copy_markup.word_count(counted)
    for v in render.OPTIONAL_VARIABLES:
        u = values.get(v, "")
        if u and u in counted: n -= copy_markup.word_count(u)
    return n

def check(d, label, s1=None, account=None, role="People leader"):
    row = settings.copy_row(d["copy_version"])
    if s1:
        steps = list(row.steps); steps[0] = CopyStep(row.steps[0].subject, s1)
        row = type(row)(**{**row.__dict__, "steps": tuple(steps)})
    account = account or {"account_id": "a1", "domain": "acme.com", "clean_name": "Acme Labs", "hq_city": "Austin", "hq_state": "TX",
                          "industry": row.industry, "industry_group": row.industry, "size_band": "50-99"}
    opener, _ = render.pick_opener(OPENERS[0], sender_is_harry=False)
    values = render.variables(account, {"first_name": "Dana", "role": role}, hannah, settings, copy_row=row, opener=opener,
                              legal_overlay="The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?")
    out = render.render_sequence(row, values, mailbox=hannah, settings=settings, for_send=False)
    print(f"\n=== {label} ===")
    for r in out:
        print(f"  email {r.step}: subject {len(r.subject):3} chars | words {body_words(row, r.step, values):3} | violations: {list(r.violations) or 'none'}")
    return out, values

smith = {"account_id": "a2", "domain": "smithlaw.com", "clean_name": "Smith & Rowe", "hq_city": "Chicago", "hq_state": "IL",
         "industry": "Legal Teams", "industry_group": "Legal Teams", "size_band": "20-49"}
t, tv = check(TECH, "TECH v3 arm A")
check(TECH, "TECH v3 arm B (soft question, link kept)", s1=TECH["s1_body"].replace("\n\nBest wishes,", ASK_TECH + "\n\nBest wishes,"))
l, lv = check(LEGAL, "LEGAL v3 arm A (founder)", account=smith, role="Founder or executive")
check(LEGAL, "LEGAL v3 arm B (founder, 20-49 rewrite)", s1=LEGAL_S1_FOUNDER_B, account=smith, role="Founder or executive")
print("\n=== openers ===")
for o in OPENERS:
    c, why = render.pick_opener(o, sender_is_harry=False); print(f"  {'KEPT   ' if c else 'DROPPED'} {o!r} {why}")
# Claim scan against facts.md: phrases we must not see
bad = ["never touches firm systems", "Nothing touches firm systems", "most tech companies already have", "other firms run it", "No phone line", "survivor guilt", "licensed", "therap"]
import itertools
texts = " ".join(itertools.chain(*[[d[k] for k in d if k.startswith("s")] for d in (TECH, LEGAL)])) + LEGAL_S1_FOUNDER_B
print("\n=== claim scan ===", [b for b in bad if b.lower() in texts.lower()] or "clean")
print("\n=== rendered LEGAL arm B email 1 (text) ===")
row = settings.copy_row("legal-teams-v3"); steps=list(row.steps); steps[0]=CopyStep(steps[0].subject, LEGAL_S1_FOUNDER_B)
row = type(row)(**{**row.__dict__, "steps": tuple(steps)})
print(render.render_step(row, lv, step=1, mailbox=hannah, settings=settings, for_send=False).text)
