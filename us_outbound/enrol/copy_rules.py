"""The render-time copy check (SPEC 10 "Rules"; SPEC 1.4; SPEC 13 CAN-SPAM).

check() runs on every rendered email before it is handed to Instantly. Any violation
blocks the send: SPEC 1.4 says the copy rules are enforced by a check at render time,
not only by review. It returns every violation, not just the first.

The rules (SPEC 10):
  * "counselor" and "counseling", never "therapy" or "therapist";
  * "registered", never "licensed";
  * no "unlimited";
  * never disparage the prospect's existing EAP or benefits (a small phrase list, for
    Harry to review);
  * "EAP" is fine when naming theirs, but never in Spill's product name;
  * American spelling;
  * the 30% utilization figure is the only statistic allowed;
  * no empty or unrendered {{variable}};
  * no line over 300 characters;
  * every copy row sent has status approved.
Step 1 also carries exactly one link, the privacy and opt-out page (SPEC 10 "Sequence").

A demo is always with Harry (SPEC 9 "Sender continuity"), so a sender who is not the demo
host may not offer "a time with me", and the host may not call himself "my colleague".

content_violations() is the word-level part on its own: render.py uses it to test an
opener before choosing it, and reply drafts (SPEC 11) can use it too.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from us_outbound.settings.model import CopyRow

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
# PHASE0-CONFIRM: both phrase lists are a first cut for Harry to review.

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
    return re.sub(r"is(e|ed|es|ing|ation|ations|er|ers)$", r"iz\1", w)


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
    (_rx(rf"\b(?:re|de|un|over)?(?:{_ISE_STEMS})is(?:e|ed|es|ing|ation|ations|er|ers)\b"), _ise),
    (_rx(r"\b(?:anal|paral|catal)ys(?:e|ed|ing)\b"), _yse),  # "analyses" is also the American plural noun
    (_rx(rf"\b(?:dis|un|mis|re)?(?:{_OUR_STEMS})our(?:s|ed|ing|al|ally|ful|fully|ite|ites|able|ably|er|ers|hood|less)?\b"), _our),
    (_rx(rf"\b(?:{_RE_STEMS})re(?:s|d)?\b"), _re),
)

# -- statistics -----------------------------------------------------------------------------
# PHASE0-CONFIRM: what counts as a statistic beyond percentages is a first cut for Harry.

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


def check(
    rendered_subject: str,
    rendered_body: str,
    *,
    copy_row: CopyRow | None,
    step: int | None,
    sender_is_harry: bool,
    privacy_url: str | None = None,
    demo_host: str = "Harry Dryden",
    exempt: Iterable[str] = (),
) -> list[str]:
    """Every violation of the SPEC 10 rules in one rendered email; the send is blocked if any.

    copy_row: the Copy-tab row it came from (None for a reply draft, which has no row).
    step: 1 to 4, or None for a reply draft. Step 1 must carry exactly one link, the
      privacy and opt-out page (privacy_url, when given).
    exempt: literal strings the word rules skip (the prospect's name, company and place,
      and our postal address); line length and variables still apply to them.
    """
    exempt = tuple(exempt)
    out: list[str] = []
    if copy_row is not None and copy_row.status != "approved":
        out.append(f"copy {copy_row.copy_version} step {copy_row.step} is {copy_row.status or 'blank'}, not approved")
    if step == 1 and not rendered_subject.strip():
        out.append("subject is empty")
    if not rendered_body.strip():
        out.append("body is empty")
    if "\n" in rendered_subject:
        out.append("subject has a line break")
    for part, text in (("subject", rendered_subject), ("body", rendered_body)):
        out.extend(f"{part} {v}" for v in content_violations(
            text, sender_is_harry=sender_is_harry, demo_host=demo_host, exempt=exempt
        ))
        out.extend(f"{part} {v}" for v in structure_violations(text))
    if step == 1:
        found = links(rendered_subject) + links(rendered_body)
        if len(found) != 1:
            out.append(f"step 1 has {len(found)} links; it may have only one, the privacy and opt-out link")
        elif privacy_url and _norm_link(found[0]) != _norm_link(privacy_url):
            out.append(f'step 1\'s one link must be the privacy and opt-out page, not "{found[0]}"')
    return list(dict.fromkeys(out))
