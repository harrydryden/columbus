"""The copy rules (SPEC 10 "Rules"; SPEC 1.4; SPEC 13 CAN-SPAM; Harry, 30 Sep 2026).

Every email is checked twice: on the sheet, when copy is written (enrol/copy_check.py renders
every row with sample values), and at render time, for the lead it is going to. Any violation
blocks the send: SPEC 1.4 says the copy rules are enforced by a check at render time, not only
by review. Each check returns every violation, not just the first.

The word rules (content_violations; SPEC 10):
  * "counselor" and "counseling", never "therapy" or "therapist";
  * never "licensed" (SPEC 10 says "registered"; facts.md says "professional counselors");
  * no "unlimited";
  * never disparage the prospect's existing EAP or benefits (a small phrase list, for
    Harry to review); "EAP" is fine when naming theirs, but never in Spill's product name;
  * American spelling;
  * the 30% utilization figure is the only statistic allowed;
  * a sender who is not the demo host may not offer "a time with me" (SPEC 9).
The rules on the copy as written (source_violations): it opens "Hi {{first_name}}," and ends
"Best wishes," and {{sender_first_name}}; no exclamation marks; no dollar figures (the price comes
from {{price_line}}, General price_from); no "Re:" or emoji in a subject; {{opener}} and
{{legal_overlay}} alone on their lines.
The rules on each email as sent (email_violations): a subject; no empty or unrendered
{{variable}}; no line over 300 characters; the word count for its step; no spam phrases; no
bare addresses; email 1 links the industry page and not the demo page (Harry, 1 Oct 2026: a demo
ask first is too presumptive); emails 2 to 4 have exactly one link to the demo page, the call to
action; links only to the demo page, the industry page or spill.chat; one link in the body, as the
signature adds the email's second (Harry, 6 Oct 2026: two links an email at most, body and signature
together; Instantly's unsubscribe link aside). The rule is checked, never applied: copy with a second body
link fails the sheet check and is never sent, so what Harry approves is what goes out. Harry, 1 Oct 2026: email 1
informs and plants a seed, so its copy asks for nothing but a visit to the site (no demo, call or
meeting words), and email 4 alludes to the free trial before it signs off ("free trial" is allowed
there and nowhere else).
SPEC 10 had step 1 carry one link only, the privacy page; Harry asked for the demo link in
every email and the industry page in the sequence (docs/pipeline.md, "Copy").

content_violations() is the word-level part on its own: render.py uses it to test an
opener before choosing it, and reply drafts (SPEC 11) can use it too. opener_violations() adds
the email-1 rules that read every word of the body (spam phrases, a demo, call or meeting ask, bare
addresses), so an opener filled with a posting like "Call Center Agent" is dropped rather than
blocking the email, and the rule that is the opener's alone: it never mentions funding or money
(money_violations; Harry, 2 Oct 2026: funding is a signal, never a line). Email bodies are not
held to that one: nonprofit copy says "funding cycles" and fintech copy "a long fundraise".
subject_violations() is the subject rules on a subject alone, for the General tab's personal subject for email 1
(email1_subject; Harry, 5 Oct 2026), which no Copy row carries. line_violations() is the rules that read a piece of
body on its own, for a copy test's line (the Tests tab's variant kind; enrol/variants.py, Harry, 7 Oct 2026).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable


MAX_LINE = 300  # SPEC 10: no line over 300 characters
ALLOWED_STATISTIC = "30"  # SPEC 10: the 30% utilization figure is the only statistic allowed

_I = re.IGNORECASE


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, _I)


# -- banned words (SPEC 10) ---------------------------------------------------------

BANNED: tuple[tuple[re.Pattern[str], str], ...] = (
    (_rx(r"\b(?:psycho)?therap(?:y|ies|ist|ists)\b"), "use counselor or counseling"),
    (_rx(r"\b(?:un)?licensed\b"), "use registered"),
    (_rx(r"\bunlimited\b"), "the copy rules never allow it"),
)

# -- EAPs: never disparage theirs, never in our name ------------------------------------
# Both phrase lists are a first cut for Harry to review (a rule of ours, not an API detail: COPY-PHRASES).

_EAP = r"(?:EAPs?|employee assistance programs?)"
DISPARAGING: tuple[re.Pattern[str], ...] = tuple(
    _rx(p)
    for p in (
        rf"\b{_EAP}\s+(?:don't|doesn't|don’t|doesn’t|do not|does not|never)\s+work\b",
        rf"\b(?:useless|bad|outdated|old-fashioned|broken|clunky|terrible|pointless|failing)\s+(?:old\s+)?{_EAP}",
        rf"\b(?:nobody|no one|no-one)\s+(?:actually\s+|really\s+|ever\s+)?uses?\s+(?:their|your|the|an|its)\s+"
        rf"(?:current\s+|existing\s+)?{_EAP}",
        rf"\b{_EAP}\s+(?:is|are)\s+(?:a\s+|such\s+a\s+)?(?:waste|useless|broken|outdated|pointless)\b",
        r"\b(?:your|their)\s+(?:current\s+|existing\s+)?(?:benefits?|perks)\s+"
        r"(?:don't|doesn't|don’t|doesn’t|do not|does not|aren't|aren’t|are not|isn't|isn’t|is not)\s+"
        r"(?:work|working|enough|good enough|used)\b",
    )
)
SPILL_EAP: tuple[re.Pattern[str], ...] = tuple(
    _rx(p)
    for p in (
        r"\bSpill(?:'s|’s)?\s+EAPs?\b",
        r"\bour\s+(?:own\s+)?EAPs?\b",
        r"\bSpill\s+(?:is|as)\s+(?:an?|the|your)\s+(?:\w+\s+)?EAP\b(?!\s+(?:alternative|replacement))",
    )
)

# -- American spelling --------------------------------------------------------------------
# Whole words, case-insensitive. Families are matched by stem; the rest are listed.


def _our(w: str) -> str:
    return w.replace("our", "or", 1)


def _ise(w: str) -> str:
    return re.sub(r"is(e|ed|es|ing|ation|ations|ational|ationally|er|ers)$", r"iz\1", w)


def _yse(w: str) -> str:
    return re.sub(r"ys(e|ed|ing)$", r"yz\1", w)


def _re(w: str) -> str:
    return re.sub(r"red$", "ered", re.sub(r"re(s?)$", r"er\1", w))


_ISE_STEMS = (
    "organ|recogn|priorit|util|apolog|emphas|summar|special|real|minim|maxim|optim|personal|custom|final|"
    "standard|familiar|modern|visual|categor|critic|author|capital|central|character|civil|econom|energ|"
    "formal|global|hospital|item|legal|local|memor|mobil|normal|rational|sanit|stabil|subsid|symbol|sympath|"
    "synchron|theor|human|harmon|monopol|patron|public|scrutin|sensit|trivial|stigmat|destigmat|digit|"
    "incentiv|monet|social|strateg|empath|ideal|operational|fantas"
)
_OUR_STEMS = (
    "behavi|col|fav|lab|hon|hum|neighb|rum|endeav|harb|flav|sav|vig|rig|val|arm|od|parl|splend|tum|vap|"
    "cand|clam|ard"
)
_RE_STEMS = "cent|theat|met|fib|calib|lit|spect|somb|lust|meag|sab|manoeuv"

# British word -> American word, for words no family covers.
BRITISH_WORDS: dict[str, str] = {
    "counselling": "counseling", "counsellor": "counselor", "counsellors": "counselors",
    "counselled": "counseled", "travelling": "traveling", "travelled": "traveled", "traveller": "traveler",
    "travellers": "travelers", "cancelled": "canceled", "cancelling": "canceling", "labelled": "labeled",
    "labelling": "labeling", "modelling": "modeling", "modelled": "modeled", "levelled": "leveled",
    "levelling": "leveling", "fuelled": "fueled", "fuelling": "fueling", "signalled": "signaled",
    "signalling": "signaling", "totalled": "totaled", "totalling": "totaling", "channelled": "channeled",
    "channelling": "channeling", "marvellous": "marvelous", "jewellery": "jewelry",
    "enrol": "enroll", "enrols": "enrolls", "enrolment": "enrollment", "enrolments": "enrollments",
    "fulfil": "fulfill", "fulfils": "fulfills", "fulfilment": "fulfillment", "skilful": "skillful",
    "wilful": "willful", "instalment": "installment", "instalments": "installments",
    "focussed": "focused", "focussing": "focusing",
    "programme": "program", "programmes": "programs", "licence": "license", "licences": "licenses",
    "practise": "practice", "practised": "practiced", "practises": "practices", "practising": "practicing",
    "defence": "defense", "offence": "offense", "pretence": "pretense",
    "catalogue": "catalog", "catalogues": "catalogs", "analogue": "analog",
    "cheque": "check", "cheques": "checks", "grey": "gray", "whilst": "while", "learnt": "learned",
    "ageing": "aging", "judgement": "judgment", "judgements": "judgments",
    "acknowledgement": "acknowledgment", "acknowledgements": "acknowledgments",
    "enquiry": "inquiry", "enquiries": "inquiries", "enquire": "inquire",
    "sceptical": "skeptical", "sceptic": "skeptic", "mould": "mold", "aluminium": "aluminum",
    "tyre": "tire", "tyres": "tires", "cosy": "cozy", "storey": "story", "pyjamas": "pajamas",
    "paediatric": "pediatric", "anaesthetic": "anesthetic", "aeroplane": "airplane", "maths": "math",
    "wellbeing": "well-being", "per cent": "percent",
}

BRITISH: tuple[tuple[re.Pattern[str], Callable[[str], str]], ...] = (
    (_rx(r"\b(?:" + "|".join(re.escape(w) for w in BRITISH_WORDS).replace(r"\ ", r"\s+") + r")\b"),
     lambda w: BRITISH_WORDS.get(re.sub(r"\s+", " ", w), w)),
    (_rx(rf"\b(?:re|de|un|over)?(?:{_ISE_STEMS})is(?:e|ed|es|ing|ation|ations|ational|ationally|er|ers)\b"), _ise),
    (_rx(r"\b(?:anal|paral|catal)ys(?:e|ed|ing)\b"), _yse),  # "analyses" is also the American plural noun
    (_rx(rf"\b(?:dis|un|mis|re)?(?:{_OUR_STEMS})our(?:s|ed|ing|al|ally|ful|fully|ite|ites|able|ably|er|ers|hood|less)?\b"), _our),
    (_rx(rf"\b(?:{_RE_STEMS})re(?:s|d)?\b"), _re),
)

# -- statistics -----------------------------------------------------------------------------
# What counts as a statistic beyond percentages is a first cut for Harry (not an API detail: COPY-PHRASES).

_PERCENT = _rx(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(?:%|percent\b|per\s+cent\b)")
# The one allowed statistic, as a claim: "30% of employees use Spill", "30% utilization".
_UTILIZATION = _rx(
    r"\b30\s?(?:%|percent\b|per\s+cent\b)\s*(?:of\s+(?:\w+\s+){0,3}?(?:employees|staff|people|team\s+members|teams?|workers)\b"
    r"|utilization\b|usage\b)"
    r"|\b(?:utilization|usage)\b[^.!?\n]{0,40}\b30\s?(?:%|percent\b)"
)
STATISTICS: tuple[re.Pattern[str], ...] = tuple(
    _rx(p)
    for p in (
        r"(?<![\w.])\d+(?:\.\d+)?[x×](?!\w)",  # 3x, 2×
        r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:in|out\s+of)\s+(?:every\s+)?"
        r"(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\b",  # 2 in 3, one out of five
        r"\b(?:\d+|two|three|four|five|ten)\s+times\s+(?:more|less|as|higher|lower|faster|cheaper|better)\b",
        r"\b\d[\d,]*\+\s*(?:companies|teams|customers|employees|people|organizations|businesses|users|members|clients)\b",
        r"\b\d(?:\.\d)?\s?(?:/|out\s+of)\s?(?:5|10)\b(?!\d)",  # 4.8/5
        r"\b\d(?:\.\d)?\s?stars?\b",
    )
)

# -- demos are always with the demo host (SPEC 9) ----------------------------------------------

_DEMO_WITH_ME = _rx(r"\b(?:(?:grab|book|find|pick|schedule)\s+(?:a\s+)?time\s+with\s+me|my\s+calendar|demo\s+with\s+me)\b")

# -- structure ---------------------------------------------------------------------------------

_VARIABLE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")
_TLDS = "com|org|net|io|ai|co|chat|us|uk|app|me|ly|so|dev|biz|info|health|care|link|page|site"
_LINK = re.compile(
    r"https?://[^\s<>()\"']+"
    r"|\bwww\.[^\s<>()\"']+"
    rf"|(?<![@\w.-])(?:[a-z0-9-]+\.)+(?:{_TLDS})(?:/[^\s<>()\"']*)?(?![\w@-])",
    _I,
)
_TRAILING = ".,;:!?)]}'\""


def links(text: str) -> list[str]:
    """Every link in the text, in order: URLs, www. hosts and bare domains a mail client would link."""
    return [m.group(0).rstrip(_TRAILING) for m in _LINK.finditer(text)]


def _norm_link(url: str) -> str:
    u = re.sub(r"^https?://", "", url.strip().lower())
    return u.removeprefix("www.").rstrip("/")


def _mask(text: str, exempt: Iterable[str]) -> str:
    """Replace exempt strings (a prospect's own name, company, place; our postal address) as whole words.

    They are proper nouns, not our wording: "The Colour Agency" is not a spelling error.
    """
    for s in sorted({e.strip() for e in exempt if e and len(e.strip()) >= 2}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(s)}(?!\w)", "Name", text)
    return text


def _quoted(m: re.Match[str]) -> str:
    return re.sub(r"\s+", " ", m.group(0).strip())


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence around text[start:end]: back to the last . ! ? or line break, on to the next."""
    before = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, end) for c in ".!?\n") if i >= 0]
    return text[before : min(ends) if ends else len(text)]


def _statistics(text: str) -> list[str]:
    out: list[str] = []
    for m in _PERCENT.finditer(text):
        figure = _quoted(m)
        if m.group(1) == ALLOWED_STATISTIC and _UTILIZATION.search(_sentence(text, m.start(), m.end())):
            continue
        if m.group(1) == ALLOWED_STATISTIC:
            out.append(f'uses the statistic "{figure}" outside the utilization claim; 30% is allowed only as the share of employees who use Spill')
        else:
            out.append(f'has the statistic "{figure}"; the 30% utilization figure is the only statistic allowed')
    for rx in STATISTICS:
        for m in rx.finditer(text):
            out.append(f'has the statistic "{_quoted(m)}"; the 30% utilization figure is the only statistic allowed')
    return out


def content_violations(
    text: str, *, sender_is_harry: bool = True, demo_host: str = "Harry Dryden", exempt: Iterable[str] = ()
) -> list[str]:
    """The word-level rules on one piece of text: banned words, EAPs, spelling, statistics, demo host."""
    t = _mask(text, exempt)
    out: list[str] = []
    for rx, advice in BANNED:
        for m in rx.finditer(t):
            out.append(f'says "{_quoted(m)}": {advice}')
    for rx in DISPARAGING:
        for m in rx.finditer(t):
            out.append(f'disparages the prospect\'s EAP or benefits: "{_quoted(m)}"')
    for rx in SPILL_EAP:
        for m in rx.finditer(t):
            out.append(f'puts "EAP" in Spill\'s product name: "{_quoted(m)}"')
    for rx, american in BRITISH:
        for m in rx.finditer(t):
            word = _quoted(m)
            out.append(f'has the British spelling "{word}" (American: "{american(word.lower())}")')
    out.extend(_statistics(t))
    if not sender_is_harry:
        for m in _DEMO_WITH_ME.finditer(t):
            out.append(f'offers a demo with the sender ("{_quoted(m)}"); demos are always with {demo_host}')
    else:
        host = demo_host.split()[0] if demo_host.strip() else ""
        if host and re.search(rf"\bmy\s+colleague\s+{re.escape(host)}\b", t, _I):
            out.append(f'calls {demo_host} "my colleague" in an email {demo_host} sends')
    return out


def structure_violations(text: str) -> list[str]:
    """Unrendered {{variables}} and {placeholders}, and lines over MAX_LINE characters."""
    out: list[str] = []
    for m in _VARIABLE.finditer(text):
        out.append(f'has the unrendered variable "{{{{{m.group(1)}}}}}"')
    stray = text.count("{{") + text.count("}}") - 2 * len(_VARIABLE.findall(text))
    if stray > 0:
        out.append('has unmatched "{{" or "}}"')
    for m in _PLACEHOLDER.finditer(text):
        out.append(f'has the unfilled placeholder "{{{m.group(1)}}}"')
    for i, line in enumerate(text.splitlines(), start=1):
        if len(line) > MAX_LINE:
            out.append(f"line {i} is {len(line)} characters (the limit is {MAX_LINE})")
    return out


# -- one email, as copy (the sheet) and as sent (Harry, 30 Sep 2026) ----------------------------

GREETING = re.compile(r"^Hi \{\{\s*first_name\s*\}\},$")
SIGN_OFF_NAME = re.compile(r"^\{\{\s*sender_first_name\s*\}\}$")
SIGN_OFFS = ("Best wishes,",)  # Harry, 1 Oct 2026
# Words each email may have, not counting the greeting and the sign-off (style.md gives the
# targets; these are the limits past which the email is blocked).
STEP_WORDS: dict[int, tuple[int, int]] = {1: (40, 120), 2: (150, 300), 3: (30, 100), 4: (25, 90)}
SUBJECT_MAX = 60  # characters, with a typical company name (the sheet check); style.md asks for under 50
SUBJECT_MAX_SENT = 150  # characters as sent, whatever the company name
SPAM_PHRASES: tuple[re.Pattern[str], ...] = tuple(
    _rx(rf"\b{p}\b")
    for p in (
        r"click here", r"act now", r"limited time", r"risk[- ]free", r"guaranteed?", r"no obligation",
        r"free trial", r"buy now", r"special offer", r"exclusive (?:deal|offer)", r"once in a lifetime",
    )
)
# Harry, 1 Oct 2026: the last email alludes to a free trial before it signs off, so "free trial"
# is allowed there (and required), and nowhere else.
FREE_TRIAL = _rx(r"\bfree trial\b")
TRIAL_STEP = 4
# Harry, 1 Oct 2026: email 1 informs and plants a seed. It asks only for a visit to the site,
# never for a demo, a call or a meeting (the signature's own booking line is not the copy).
_STEP1_ASK = _rx(
    r"\b(?:demo|demos|book(?:ing)?|(?<!on-)calls?(?!-)|meetings?|calendar|schedule|hop on|jump on|walkthrough"
    r"|(?:15|20|30)[- ]minutes?)\b"
)  # "on-call" and "call-outs" are industry words, not an ask
POOR_ANCHORS = frozenset({"here", "click here", "this link", "link", "this", "click"})
EMAIL_LINKS = 2  # Harry, 6 Oct 2026: links an email carries at most, body and signature together
SIGNATURE_LINKS = 1  # the signature's one line, one link (Harry, 5 Oct 2026; render.signature)
BODY_LINKS = EMAIL_LINKS - SIGNATURE_LINKS
_DOLLARS = re.compile(r"\$\s?\d|\b\d[\d,]*\s?(?:dollars|USD)\b", _I)
_EMOJI = re.compile("[\u2600-\u27bf\U0001f000-\U0001faff]")
_REPLY_PREFIX = _rx(r"^\s*(?:re|fwd?)\s*:")
SPILL_PAGES = ("https://www.spill.chat/", "https://spill.chat/")


def source_violations(subject: str, body: str, *, step: int) -> list[str]:
    """Rules on the copy as written in the sheet, before any value goes in."""
    out: list[str] = []
    lines = [line.strip() for line in body.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines or not GREETING.match(lines[0]):
        out.append('body must start with the line "Hi {{first_name}},"')
    if len(lines) < 2 or not SIGN_OFF_NAME.match(lines[-1]) or lines[-2] not in SIGN_OFFS:
        out.append('body must end with the line "Best wishes," and then "{{sender_first_name}}"')
    for part, text in (("subject", subject), ("body", body)):
        if "!" in text:
            out.append(f"{part} has an exclamation mark; the style is calm, not salesy")
        for m in _DOLLARS.finditer(text):
            out.append(f'{part} has the price "{_quoted(m)}"; the price comes only from {{{{price_line}}}} (General price_from)')
    if _REPLY_PREFIX.match(subject):
        out.append('subject starts with "Re:" or "Fwd:" for an email that is neither')
    if _EMOJI.search(subject):
        out.append("subject has an emoji")
    for name in ("opener", "legal_overlay"):
        for line in body.split("\n"):
            if re.search(rf"\{{\{{\s*{name}\s*\}}\}}", line) and not re.fullmatch(rf"\s*\{{\{{\s*{name}\s*\}}\}}\s*", line):
                out.append(f"{{{{{name}}}}} must be alone on its line, so the line disappears when it is empty")
    return out


def email_violations(
    subject: str,
    words: str,
    links_found: Iterable[tuple[str, str]],
    *,
    step: int,
    demo_url: str,
    industry_url: str = "",
    sender_is_harry: bool,
    demo_host: str = "Harry Dryden",
    exempt: Iterable[str] = (),
    uncounted: Iterable[str] = (),
    site_url: str = "",
) -> list[str]:
    """Rules on one email as it will be sent: its subject, its words and its links (signature and notice excluded).

    uncounted: filled-in lines that are not the copy's own words (the evidence opener, the legal
    overlay), left out of the word count so a long opener never blocks an email.
    """
    exempt = tuple(exempt)
    out: list[str] = []
    if not subject.strip():
        out.append("subject is empty")
    if "\n" in subject:
        out.append("subject has a line break")
    if len(subject) > SUBJECT_MAX_SENT:
        out.append(f"subject is {len(subject)} characters (the limit is {SUBJECT_MAX_SENT})")
    if not words.strip():
        out.append("body is empty")
    for part, text in (("subject", subject), ("body", words)):
        out.extend(f"{part} {v}" for v in content_violations(
            text, sender_is_harry=sender_is_harry, demo_host=demo_host, exempt=exempt
        ))
        out.extend(f"{part} {v}" for v in structure_violations(text))
        masked = _mask(text, exempt)
        for rx in SPAM_PHRASES:
            for m in rx.finditer(masked):
                if step == TRIAL_STEP and part == "body" and rx.pattern == FREE_TRIAL.pattern:
                    continue  # the free trial belongs in the last email (Harry, 1 Oct 2026)
                out.append(f'{part} says "{_quoted(m)}", which reads as spam')
    if step == TRIAL_STEP and not FREE_TRIAL.search(words):
        out.append("email 4 does not mention the free trial; the last email alludes to it before signing off")
    if step == 1:
        for m in _STEP1_ASK.finditer(_mask(words, exempt)):
            out.append(f'email 1 says "{_quoted(m)}"; the first email asks only for a visit to the site, '
                       "never a demo, call or meeting")
    for bare in links(_mask(words, exempt)):
        out.append(f'body has the bare address "{bare}"; write it as [anchor text](link)')
    lines = [line for line in words.split("\n") if line.strip()]
    counted = " ".join(lines[1:-2]) if len(lines) > 3 else " ".join(lines)
    from us_outbound.enrol.copy_markup import word_count

    n = word_count(counted) - sum(word_count(u) for u in uncounted if u and u in counted)
    lo, hi = STEP_WORDS.get(step, (0, 10**6))
    if not lo <= n <= hi:
        out.append(f"email {step} has {n} words; it should have {lo} to {hi}")
    found = list(links_found)
    if len(found) > BODY_LINKS:
        out.append(f"has {len(found)} links in the body; with the signature's link an email has {EMAIL_LINKS} at most, "
                   "so the body has one, its call to action: write any other as plain words")
    demo = [u for _, u in found if _norm_link(u) == _norm_link(demo_url)] if demo_url else []
    page = [u for _, u in found if industry_url and _norm_link(u) == _norm_link(industry_url)]
    if step == 1:
        # Harry, 1 Oct 2026: a demo ask in the first email is too presumptive; it links the industry page.
        if demo:
            out.append("email 1 links the demo page; the first email links the industry page instead")
        if industry_url and len(page) != 1:
            out.append(f"email 1 has {len(page)} links to the industry page; it has exactly one")
    elif len(demo) != 1:
        out.append(f"has {len(demo)} links to the demo page; emails 2 to 4 have exactly one, the call to action")
    # With no industry page, {{industry_url}} is the site, which the "trusted by" line also links.
    if industry_url and _norm_link(industry_url) != _norm_link(site_url) \
            and sum(1 for _, u in found if _norm_link(u) == _norm_link(industry_url)) > 1:
        out.append("links the industry page more than once")
    for anchor, url in found:
        if anchor.strip().casefold().rstrip(".") in POOR_ANCHORS:
            out.append(f'has the anchor text "{anchor}"; say where the link goes')
        known = {_norm_link(u) for u in (demo_url, industry_url) if u}
        if _norm_link(url) not in known and not url.startswith(SPILL_PAGES):
            out.append(f'links to "{url}"; emails link only to the demo page, the industry page or spill.chat')
    return list(dict.fromkeys(out))


def subject_violations(subject: str, *, exempt: Iterable[str] = (), step: int = 1) -> list[str]:
    """The rules on email 1's subject alone, as filled: General email1_subject, filled for a sample prospect, is
    checked with these when the General tab is read (settings/validate.py; Harry, 5 Oct 2026).

    The subject rules render_step applies to a Copy row's subject, as written (no exclamation mark, price, "Re:"
    or emoji) and as sent (one line, the word rules, no unrendered variable, no spam phrase), and the rule email 1
    keeps for its words: no demo, call or meeting ask. step: the email the subject is for (a variant test's
    subject may be for emails 2 to 4, which may name a demo; enrol/variants.py, Harry, 7 Oct 2026).
    """
    masked = _mask(subject, exempt)
    out: list[str] = []
    if not subject.strip():
        out.append("is empty")
    if "\n" in subject:
        out.append("has a line break")
    if len(subject) > SUBJECT_MAX:
        out.append(f"is {len(subject)} characters with a typical company name (the limit is {SUBJECT_MAX})")
    if "!" in subject:
        out.append("has an exclamation mark; the style is calm, not salesy")
    out += [f'has the price "{_quoted(m)}"; the price comes only from {{{{price_line}}}} (General price_from)'
            for m in _DOLLARS.finditer(subject)]
    if _REPLY_PREFIX.match(subject):
        out.append('starts with "Re:" or "Fwd:" for an email that is neither')
    if _EMOJI.search(subject):
        out.append("has an emoji")
    out += content_violations(subject, exempt=exempt) + structure_violations(subject)
    out += [f'says "{_quoted(m)}", which reads as spam' for rx in SPAM_PHRASES for m in rx.finditer(masked)]
    if step == 1:
        out += [f'says "{_quoted(m)}"; email 1 asks only for a visit to the site, never a demo, call or meeting'
                for m in _STEP1_ASK.finditer(masked)]
    return list(dict.fromkeys(out))


def line_violations(source: str, words: str, *, step: int, exempt: Iterable[str] = ()) -> list[str]:
    """The rules on a piece of one email's body, alone: a copy test's line (enrol/variants.py; Harry, 7 Oct 2026),
    checked when the Tests tab is read, before it reaches any email.

    source is the text as written (no exclamation mark, and no price but {{price_line}}); words is the text as
    filled with sample values, anchor text only: the word rules for either kind of sender (content_violations),
    no unrendered variable or line over MAX_LINE characters, no spam phrase (email 4 may allude to the free trial),
    no bare address, and in email 1 no demo, call or meeting ask. The rules that read a whole email (its word count,
    links, greeting and sign-off) are checked when each email is rendered for its lead (render.render_step).
    """
    masked = _mask(words, exempt)
    out: list[str] = []
    if "!" in source:
        out.append("has an exclamation mark; the style is calm, not salesy")
    out += [f'has the price "{_quoted(m)}"; the price comes only from {{{{price_line}}}} (General price_from)'
            for m in _DOLLARS.finditer(source)]
    for harry in (True, False):  # demos are with the demo host whoever sends: both rules apply to a shared line
        out += content_violations(words, sender_is_harry=harry, exempt=exempt)
    out += structure_violations(words)
    for rx in SPAM_PHRASES:
        if step == TRIAL_STEP and rx.pattern == FREE_TRIAL.pattern:
            continue  # the free trial belongs in the last email (Harry, 1 Oct 2026)
        out += [f'says "{_quoted(m)}", which reads as spam' for m in rx.finditer(masked)]
    if step == 1:
        out += [f'says "{_quoted(m)}"; email 1 asks only for a visit to the site, never a demo, call or meeting'
                for m in _STEP1_ASK.finditer(masked)]
    out += [f'has the bare address "{bare}"; write it as [anchor text](link)' for bare in links(masked)]
    return list(dict.fromkeys(out))


def opener_violations(text: str, *, exempt: Iterable[str] = ()) -> list[str]:
    """The email-1 rules that read every word of the body, on an opener alone: spam phrases, an ask for a
    demo, call or meeting, and bare addresses (render.pick_opener; Harry, 2 Oct 2026); and the opener's
    own rule, no funding or money (money_violations).

    A filled opener that breaks one of the email-1 rules would block email 1 at render time, so it is
    dropped first; one that mentions funding or money is dropped the same way.
    """
    masked = _mask(text, exempt)
    out = [f'says "{_quoted(m)}", which reads as spam' for rx in SPAM_PHRASES for m in rx.finditer(masked)]
    out += [f'says "{_quoted(m)}"; email 1 asks only for a visit to the site, never a demo, call or meeting'
            for m in _STEP1_ASK.finditer(masked)]
    out += [f'has the bare address "{bare}"' for bare in links(masked)]
    out += money_violations(masked)
    return out


# -- openers never mention funding or money (Harry, 2 Oct 2026) -----------------------------------
# Funding is a signal, never a line. A round tells us a team is going through a period of change, and
# the opener speaks to what that change tends to bring for people, never to the money: a line about
# the round reads as money grabbing (style.md, "Openers"). Openers only, as written on the sheet
# (copy_desk.check_openers) and as filled (render.pick_opener); never email bodies.
MONEY_ADVICE = "an opener never mentions funding or money: funding is a signal, never a line (style.md)"
MONEY: tuple[re.Pattern[str], ...] = (
    _rx(r"\{\s*funding\w*\s*\}"),  # a funding token as written on the sheet: {funding_stage}
    _rx(
        r"\b(?:fund(?:s|ed|ing|er|ers)?|fundrais(?:e|es|ed|er|ers|ing)|rais(?:e|es|ed|ing)"
        r"|(?:pre-)?seed(?:\s+rounds?|[- ]stage)|pre-seed|rounds?|investors?|investments?|capital|valuations?"
        r"|backed|backing|backers?|ipos?|vcs?|financ(?:ed|ing)|money|cash|dollars?)(?![a-z_])"
    ),  # "seed" alone is a crop in agritech, and "finance" a team: neither is a round
    re.compile(r"\b[Ss]eries\s+[A-Z](?![A-Za-z])"),  # Series A; "a series of changes" is not a round
    re.compile(r"\$\s?\d[\d,.]*(?:\s?(?:[KMB]|million|billion)(?![A-Za-z]))?", re.IGNORECASE),
)


def money_violations(text: str, *, exempt: Iterable[str] = ()) -> list[str]:
    """Every mention of funding or money in an opener line ("raised", "Series A", "seed round", "investors",
    "{funding_stage}"), as written or as filled. exempt: proper nouns that are not our wording, like a
    company called Summit Capital."""
    masked = _mask(text, exempt)
    found: list[re.Match[str]] = []
    for rx in MONEY:  # one mention once: "{funding_stage}" is not also "funding"
        found += [m for m in rx.finditer(masked) if not any(m.start() < f.end() and f.start() < m.end() for f in found)]
    return [f'says "{_quoted(m)}"; {MONEY_ADVICE}' for m in sorted(found, key=lambda m: m.start())]
