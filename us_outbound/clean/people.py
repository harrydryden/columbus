"""People fields (SPEC 13, Data cleaning; SPEC 5 Roles; SPEC 6 accounts.size_band).

  * Title-case names, with credentials, pronouns and emojis stripped.
  * Map titles to roles by the Roles tab, and skip the titles SPEC 5 says to skip
    (fixed in code): interns, coordinators, recruiters, sales ops and CS ops, and any
    EMEA or APAC title.
  * Rank people for contact (Harry, 1 Oct 2026: "the closer to seniority and decision maker
    the better"): the Roles-tab order for the company's size, then seniority, then how well
    the title matches, then the newest in role. Junior titles come last, and never as a
    People leader. contacts/pick.py and enrol/enrol.py both rank with rank_person().
  * Record states as USPS codes.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from us_outbound.settings.model import SIZE_BANDS, Role, band_bounds

# -- names -------------------------------------------------------------------

# Compared in upper case without dots ("Ph.D." -> "PHD").
CREDENTIALS = frozenset(
    {
        "MBA", "PHD", "EDD", "PSYD", "DBA", "JD", "LLM", "ESQ", "CPA", "CMA", "CFA", "CFP", "CHFC", "CLU",
        "SHRM", "SHRM-CP", "SHRM-SCP", "SPHR", "PHR", "APHR", "GPHR", "SPHRI", "PHRI", "CEBS", "CCP", "CBP",
        "CPLP", "CHRP", "CHRL", "CPTD", "PMP", "CSM", "CSPO", "LCSW", "LMSW", "MSW", "LMFT", "LPC", "LMHC",
        "MSC", "BSC", "MED", "MPH", "MHA", "MPA", "MFA", "MPP", "BSN", "MSN", "DNP", "DDS", "DMD", "DVM",
        "PA-C", "AIA", "CISSP", "CAE", "CFRE", "CPC", "CMP", "ACC", "PCC", "MCC", "RYT", "FACHE",
    }
)
# Also surnames (Ma, Do, Ba ...): stripped only when written in capitals or with dots, and
# never when they are the whole field.
AMBIGUOUS_CREDENTIALS = frozenset({"MA", "MS", "BA", "BS", "DO", "MD", "PA", "NP", "RN", "PE", "JD"})
HONORIFICS = frozenset({"DR", "MR", "MRS", "MS", "MISS", "MX", "PROF", "REV", "HON"})
GENERATIONAL = {"JR": "Jr.", "SR": "Sr.", "II": "II", "III": "III", "IV": "IV"}

_PRONOUN = r"(?:she|her|hers|he|him|his|they|them|their|theirs|xe|xem|xyr|ze|zir|hir|any|all)"
_PRONOUNS = re.compile(
    rf"(?<!\w)(?:pronouns?\s*:?\s*)?{_PRONOUN}(?:\s*/\s*{_PRONOUN})+(?!\w)", re.IGNORECASE
)
_BRACKETED = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\}")
_DOUBLE_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")  # nicknames: Robert "Bob" Smith
_EDGE_PUNCT = " ,;:|/\\-_*~•·"
_DROP_CHARS = {"️", "︎", "⃣"}


def _strip_symbols(text: str) -> str:
    """Remove emojis and other symbols, keeping letters, accents and name punctuation."""
    text = unicodedata.normalize("NFC", text).replace("’", "'").replace("‘", "'")
    return "".join(
        " " if unicodedata.category(ch) in {"So", "Sk", "Sm", "Co", "Cn"} else ch
        for ch in text
        if ch not in _DROP_CHARS and unicodedata.category(ch) != "Cf"
    )


def _cred_key(token: str) -> str:
    return token.replace(".", "").upper()


def _is_credential(token: str, *, sole: bool) -> bool:
    key = _cred_key(token)
    pieces = [p for p in key.split("/") if p]  # "MBA/PhD"
    if not pieces:
        return False
    for p in pieces:
        if p in CREDENTIALS:
            continue
        if p in AMBIGUOUS_CREDENTIALS and not sole and (token.isupper() or "." in token):
            continue
        return False
    return True


def _recase_piece(piece: str) -> str:
    out, cap = [], True
    for ch in piece.lower():
        out.append(ch.upper() if cap else ch)
        cap = ch in "'."
    s = "".join(out)
    if len(s) > 3 and s.startswith("Mc") and s[2].isalpha():
        s = "Mc" + s[2].upper() + s[3:]
    return s


def _recase(word: str) -> str:
    """Title-case a word written all in capitals or all in lower case; keep intentional casing."""
    gen = GENERATIONAL.get(_cred_key(word))
    if gen:
        return gen
    if not (word.isupper() or word.islower()):
        return word  # McDonald, DeShawn, MacArthur, LaToya
    return "-".join(_recase_piece(p) for p in word.split("-"))


def _clean_part(text: str | None, *, first: bool) -> str:
    s = _strip_symbols(text or "")
    prev = None
    while prev != s:
        prev, s = s, _BRACKETED.sub(" ", s)
    s = _DOUBLE_QUOTED.sub(" ", s)
    s = _PRONOUNS.sub(" ", s)
    # After a comma come credentials ("Doe, MBA, SHRM-CP"); keep only a generational suffix.
    head, *after = s.split(",")
    tokens = head.split()
    for seg in after:
        tokens += [t for t in seg.split() if _cred_key(t) in GENERATIONAL]
    tokens = [t.strip(_EDGE_PUNCT) for t in tokens]
    tokens = [t for t in tokens if t]
    if first:
        while len(tokens) > 1 and _cred_key(tokens[0]) in HONORIFICS:
            tokens.pop(0)
    kept: list[str] = []
    for t in tokens:
        if _cred_key(t) in GENERATIONAL or not _is_credential(t, sole=len(tokens) == 1):
            kept.append(t)
    return " ".join(_recase(t) for t in kept)


def clean_person_name(first: str | None, last: str | None) -> tuple[str, str]:
    """(first, last) title-cased, without credentials, pronouns, honorifics or emojis.

    "JANE", "DOE, MBA" -> ("Jane", "Doe"); "o'brien" -> "O'Brien"; mixed case is kept as written.
    """
    return _clean_part(first, first=True), _clean_part(last, first=False)


# -- states ------------------------------------------------------------------

USPS_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "GU": "Guam", "VI": "U.S. Virgin Islands", "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}
# Common abbreviations (AP and GPO styles), keyed without dots or spaces.
_STATE_ABBREVIATIONS = {
    "ala": "AL", "alab": "AL", "ariz": "AZ", "ark": "AR", "calif": "CA", "cal": "CA", "cali": "CA",
    "colo": "CO", "col": "CO", "conn": "CT", "del": "DE", "washingtondc": "DC", "washdc": "DC",
    "fla": "FL", "flor": "FL", "ill": "IL", "ills": "IL", "ind": "IN", "kan": "KS", "kans": "KS",
    "kent": "KY", "mass": "MA", "mich": "MI", "minn": "MN", "miss": "MS", "mont": "MT", "neb": "NE",
    "nebr": "NE", "nev": "NV", "nmex": "NM", "ncar": "NC", "ndak": "ND", "okla": "OK", "ore": "OR",
    "oreg": "OR", "penn": "PA", "penna": "PA", "scar": "SC", "sdak": "SD", "tenn": "TN", "tex": "TX",
    "wash": "WA", "wva": "WV", "wvirginia": "WV", "wis": "WI", "wisc": "WI", "wyo": "WY",
    "virginislands": "VI", "usvirginislands": "VI", "usvi": "VI",
}
_COUNTRY = frozenset({"us", "usa", "unitedstates", "unitedstatesofamerica", "america"})
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def _state_key(text: str) -> str:
    s = _ZIP.sub(" ", text.casefold()).replace(".", "")
    s = re.sub(r"^\s*state\s+of\s+|\s+state\s*$", "", " ".join(s.split()))
    return re.sub(r"[^a-z]", "", s)


_STATE_LOOKUP: dict[str, str] = {
    **{code.lower(): code for code in USPS_STATES},
    **{re.sub(r"[^a-z]", "", name.casefold()): code for code, name in USPS_STATES.items()},
    **_STATE_ABBREVIATIONS,
}


def state_code(value: str | None) -> str | None:
    """The USPS code for a US state, DC or territory, however it is written; None otherwise.

    Takes names ("Illinois"), codes ("il"), abbreviations ("Calif.", "N.Y.", "Penn") and
    locations ("Chicago, IL, USA", "Washington, D.C.", "New York, NY 10001").
    """
    if not value or not value.strip():
        return None
    whole = _state_key(value.replace(",", " "))
    if whole in _STATE_LOOKUP:
        return _STATE_LOOKUP[whole]
    for seg in reversed(value.split(",")):  # the state comes after the city
        key = _state_key(seg)
        if not key or key in _COUNTRY:
            continue
        return _STATE_LOOKUP.get(key)
    return None


# -- titles and roles --------------------------------------------------------

# Long forms folded to one spelling, applied to both the prospect's title and the Roles tab.
_TITLE_PHRASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("chief", "human", "resources", "officer"), ("chro",)),
    (("chief", "human", "resource", "officer"), ("chro",)),
    (("chief", "hr", "officer"), ("chro",)),
    (("chief", "people", "officer"), ("cpo",)),
    (("chief", "executive", "officer"), ("ceo",)),
    (("chief", "operating", "officer"), ("coo",)),
    (("chief", "operations", "officer"), ("coo",)),
    (("chief", "financial", "officer"), ("cfo",)),
    (("chief", "finance", "officer"), ("cfo",)),
    (("chief", "of", "staff"), ("chiefofstaff",)),  # one word: not C-level, and not a "Chief ... Officer"
    (("vice", "president"), ("vp",)),
    (("v", "p"), ("vp",)),
    (("human", "resources"), ("hr",)),
    (("human", "resource"), ("hr",)),
    (("co", "founder"), ("cofounder",)),
    (("business", "partner"), ("businesspartner",)),  # an HR Business Partner is not a partner of the firm
    (("product", "owner"), ("productowner",)),  # a Scrum role, not an owner of the company
)
_TITLE_WORDS: dict[str, tuple[str, ...]] = {
    "dir": ("director",), "mgr": ("manager",), "mngr": ("manager",), "sr": ("senior",), "snr": ("senior",),
    "jr": ("junior",), "asst": ("assistant",), "assoc": ("associate",), "exec": ("executive",),
    "ops": ("operations",), "pres": ("president",), "svp": ("senior", "vp"), "evp": ("executive", "vp"),
    "avp": ("assistant", "vp"), "gm": ("general", "manager"), "admin": ("administrator",),
    "hrbp": ("hr", "business", "partner"),
}
_TITLE_FILLER = frozenset({"of", "the", "and", "for", "a", "an", "at"})

# SPEC 5 "Skip", fixed in code. Phrases in the folded spelling above.
# TODO(Harry): only EMEA and APAC are specified; APJ, LATAM, Europe, UK and Asia are not skipped.
SKIP_TITLE_PHRASES: tuple[str, ...] = (
    "intern", "interns", "internship",
    "coordinator", "coordinators",
    "recruiter", "recruiters", "recruiting", "recruitment", "talent acquisition",
    "sales operations", "salesops",
    "customer success operations", "cs operations", "csops",
    "revenue operations", "revops",  # sales ops under another name
    "emea", "apac", "asia pacific", "europe middle east africa",
)

# Harry, 1 Oct 2026: junior titles are contacted only when nobody else is left, and never for
# the People leader role. A junior title that matches a founder's or an executive's is an
# assistant to one ("Executive Assistant, Office of the CEO"), so it is never contacted as one either.
JUNIOR_WORDS = frozenset(
    {"assistant", "associate", "coordinator", "intern", "internship", "specialist", "junior", "trainee", "apprentice"}
)
JUNIOR_NEVER_ROLES = frozenset({"People leader", "Founder or executive"})
_JUNIOR_NEVER = frozenset(r.casefold() for r in JUNIOR_NEVER_ROLES)

# Seniority, most senior first (Harry, 1 Oct 2026: "seniority matters more than function"). A title
# takes the first level any of whose phrases it contains, so "Managing Director" is C-level, not Director.
SENIORITY = ("C-level, founder or owner", "VP or Head", "Director", "Manager or Lead", "Other")
_SENIORITY_PHRASES: tuple[tuple[str, ...], ...] = (
    ("ceo", "coo", "cfo", "cpo", "chro", "cto", "cmo", "cro", "cio", "ciso", "cso", "cco", "cdo", "cao", "chief",
     "founder", "cofounder", "owner", "president", "managing partner", "founding partner", "managing director",
     "managing principal", "managing member", "executive director", "founding director"),
    ("vp", "head", "general manager", "partner", "principal", "chiefofstaff"),
    ("director",),
    ("manager", "lead", "leader", "businesspartner", "supervisor"),
)
OTHER_SENIORITY = len(_SENIORITY_PHRASES)

# One-word titles that, followed by one of these words, name another job: a "Principal Engineer",
# a "Partner Manager" or an "Owner Relations" lead does not own the firm.
_GUARDED_WORDS = frozenset({"principal", "partner", "owner"})
_NOT_AN_OWNER_BEFORE = frozenset(
    {"engineer", "engineering", "consultant", "designer", "architect", "scientist", "developer", "analyst",
     "researcher", "product", "software", "data", "program", "project", "account", "accounts", "solutions",
     "success", "marketing", "manager", "relations", "development", "experience"}
)


@lru_cache(maxsize=8192)
def _folded(title: str) -> tuple[str, ...]:
    s = _strip_symbols(title).casefold().replace("&", " and ").replace("+", " and ").replace("'", "")
    tokens = re.sub(r"[^\w]+", " ", s).split()
    if "to" in tokens[1:]:  # "Executive Assistant to the CEO" is not the CEO
        tokens = tokens[: tokens.index("to", 1)]
    words: list[str] = []
    for t in tokens:
        words.extend(_TITLE_WORDS.get(t, (t,)))
    i, out = 0, []
    while i < len(words):
        for long_form, short in _TITLE_PHRASES:
            if tuple(words[i : i + len(long_form)]) == long_form:
                out.extend(short)
                i += len(long_form)
                break
        else:
            out.append(words[i])
            i += 1
    return tuple(w for w in out if w not in _TITLE_FILLER)


def _fold_title(title: str | None) -> list[str]:
    return list(_folded(title or ""))


def title_key(title: str | None) -> str:
    """One spelling for a title however it is written ("Head of Human Resources" and "head of HR" match)."""
    return " ".join(_folded(title or ""))


def _contains(haystack: Sequence[str], phrase: Sequence[str]) -> bool:
    return bool(phrase) and f" {' '.join(phrase)} " in f" {' '.join(haystack)} "


def is_skipped_title(title: str | None) -> bool:
    """True for titles SPEC 5 says never to contact (interns, coordinators, recruiters, ...)."""
    folded = _fold_title(title)
    return any(_contains(folded, phrase.split()) for phrase in SKIP_TITLE_PHRASES)


def is_junior_title(title: str | None) -> bool:
    """True for assistant, associate, coordinator, intern, specialist (and junior, trainee, apprentice) titles."""
    return any(w in JUNIOR_WORDS for w in _fold_title(title))


def seniority_rank(title: str | None) -> int:
    """0 C-level, founder or owner; 1 VP or Head; 2 Director; 3 Manager or Lead; 4 other (SENIORITY).

    Abbreviations count as their long forms (SVP is a VP, Sr. Dir. a Director, CHRO and CPO C-level).
    """
    folded = _fold_title(title)
    for rank, phrases in enumerate(_SENIORITY_PHRASES):
        if any(_contains(folded, p.split()) for p in phrases):
            return rank
    return OTHER_SENIORITY


def _spans(words: tuple[str, ...], phrase: tuple[str, ...]) -> list[tuple[int, int]]:
    """Where phrase appears in words, as [start, end) spans; a guarded word before another job's word does not count."""
    n, out = len(phrase), []
    for i in range(len(words) - n + 1):
        if words[i : i + n] != phrase:
            continue
        if n == 1 and phrase[0] in _GUARDED_WORDS and i + 1 < len(words) and words[i + 1] in _NOT_AN_OWNER_BEFORE:
            continue
        out.append((i, i + n))
    return out


def _inside(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Span a lies within the longer span b."""
    return b[0] <= a[0] and a[1] <= b[1] and b[1] - b[0] > a[1] - a[0]


@dataclass(frozen=True)
class TitleMatch:
    role: Role  # the Roles-tab row
    index: int  # its place on the tab: on a tie, the row listed first wins
    longest: tuple[int, int]  # (words, characters) of its longest phrase found in the title
    covered: float  # the share of the title's words its phrases cover


def title_matches(title: str | None, roles: Sequence[Role], industry_group: str = "") -> list[TitleMatch]:
    """Every Roles-tab row with a title that appears in this one as a whole phrase; [] for a skipped title.

    Case, punctuation, "&"/"and", "of", and common long forms (Chief Executive Officer ->
    CEO, Vice President -> VP, Dir. -> Director, Human Resources -> HR) do not matter. A
    phrase inside a longer phrase of another row does not count: a "People Operations
    Manager" is not an "Operations Manager", and a "Founding Partner" is a founder at any firm.
    A row with industry_groups counts only at an account in one of them.
    """
    words = _folded(title or "")
    if not words or is_skipped_title(title):
        return []
    found: list[tuple[int, tuple[int, int], str]] = []  # (row index, span, phrase text)
    for idx, role in enumerate(roles):
        if not role.counts_in(industry_group):
            continue
        for t in role.titles:
            phrase = _folded(t)
            found += [(idx, span, " ".join(phrase)) for span in _spans(words, phrase)] if phrase else []
    kept = [f for f in found if not any(o[0] != f[0] and _inside(f[1], o[1]) for o in found)]
    out = []
    for idx in sorted({f[0] for f in kept}):
        mine = [f for f in kept if f[0] == idx]
        covered = {i for _, (lo, hi), _ in mine for i in range(lo, hi)}
        longest = max((hi - lo, len(text)) for _, (lo, hi), text in mine)
        out.append(TitleMatch(roles[idx], idx, longest, len(covered) / len(words)))
    return out


def map_title_to_role(title: str | None, roles: Sequence[Role], industry_group: str = "") -> str | None:
    """The role this title is contacted as (its row's copy role), whatever the company size; None if none or skipped.

    The longest matching phrase wins; on a tie, the row listed first on the tab. rank_person
    is the size-aware version contacts are picked with.
    """
    matches = title_matches(title, roles, industry_group)
    if not matches:
        return None
    return max(matches, key=lambda m: (m.longest, -m.index)).role.writes_as


@dataclass(frozen=True)
class Ranked:
    """Where one person comes in the order for one company: the lower key, the sooner contacted."""

    role: Role  # the Roles-tab row they are contacted under (role.writes_as picks the copy)
    order: int  # that row's rank at the company's size
    seniority: int  # seniority_rank
    covered: float  # the share of the title the row's titles cover
    junior: bool
    days_in_role: int | None = None

    @property
    def key(self) -> tuple:
        newest = self.days_in_role if self.days_in_role is not None else math.inf
        return (self.junior, self.order, self.seniority, -self.covered, newest)


def rank_person(
    title: str | None, roles: Sequence[Role], employees: int | None, industry_group: str = "",
    days_in_role: int | None = None,
) -> Ranked | None:
    """Where this title comes at a company of this size; None when it is never contacted there.

    The order (Harry, 1 Oct 2026): 1. the Roles-tab rank for the size; 2. seniority; 3. how
    much of the title the row's titles cover; 4. the newest in role, when known. A junior
    title sorts after everyone else, so it is used only when nobody else is left, and it is
    never contacted as a People leader or Founder or executive. A title on two rows
    ("Founder & Head of People") counts as the one ranked higher at this size.
    """
    junior = is_junior_title(title)
    best: TitleMatch | None = None
    best_order = 0
    for m in title_matches(title, roles, industry_group):
        order = m.role.order_at(employees)
        if order is None or (junior and m.role.writes_as.casefold() in _JUNIOR_NEVER):
            continue
        if best is None or (order, -m.covered) < (best_order, -best.covered):
            best, best_order = m, order
    if best is None:
        return None
    return Ranked(best.role, best_order, seniority_rank(title), best.covered, junior, days_in_role)


# -- size --------------------------------------------------------------------

def size_band(employees: int | float | str | None) -> str | None:
    """The SIZE_BANDS band for a headcount ("64" -> "50-99", "7" -> "5-9"); None when unknown or off the ladder.
    Whether the size is one we contact is the General range's question (Settings.size_in_range), not this one."""
    if employees is None or isinstance(employees, bool):
        return None
    try:
        n = int(float(str(employees).replace(",", "").strip()))
    except (ValueError, OverflowError):
        return None
    for band in SIZE_BANDS:
        lo, hi = band_bounds(band)
        if lo <= n <= hi:
            return band
    return None


def company_size(employees: int | float | str | None, band: str | None = None) -> int | None:
    """The headcount the Roles tab's size ranges are read against: the low end of the account's
    size band (as the resolver set it, so "50-99" is 50), else its employee count; None if unknown."""
    if band in SIZE_BANDS:
        return band_bounds(band)[0]
    if employees is None or isinstance(employees, bool):
        return None
    try:
        return int(float(str(employees).replace(",", "").strip()))
    except (ValueError, OverflowError):
        return None
