"""People fields (SPEC 13, Data cleaning; SPEC 5 Roles; SPEC 6 accounts.size_band).

  * Title-case names, with credentials, pronouns and emojis stripped.
  * Map titles to roles by the Roles tab, and skip the titles SPEC 5 says to skip
    (fixed in code): interns, coordinators, recruiters, sales ops and CS ops, and any
    EMEA or APAC title.
  * Record states as USPS codes.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from us_outbound.settings.model import SIZE_BANDS, Role

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
    (("chief", "executive", "officer"), ("ceo",)),
    (("chief", "operating", "officer"), ("coo",)),
    (("chief", "operations", "officer"), ("coo",)),
    (("chief", "financial", "officer"), ("cfo",)),
    (("chief", "finance", "officer"), ("cfo",)),
    (("vice", "president"), ("vp",)),
    (("human", "resources"), ("hr",)),
    (("human", "resource"), ("hr",)),
    (("co", "founder"), ("cofounder",)),
)
_TITLE_WORDS: dict[str, tuple[str, ...]] = {
    "dir": ("director",), "mgr": ("manager",), "mngr": ("manager",), "sr": ("senior",), "snr": ("senior",),
    "jr": ("junior",), "asst": ("assistant",), "assoc": ("associate",), "exec": ("executive",),
    "ops": ("operations",), "pres": ("president",), "svp": ("senior", "vp"), "evp": ("executive", "vp"),
    "avp": ("assistant", "vp"), "gm": ("general", "manager"),
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


def _fold_title(title: str | None) -> list[str]:
    s = _strip_symbols(title or "").casefold().replace("&", " and ").replace("+", " and ").replace("'", "")
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
    return [w for w in out if w not in _TITLE_FILLER]


def _contains(haystack: list[str], phrase: list[str]) -> bool:
    return bool(phrase) and f" {' '.join(phrase)} " in f" {' '.join(haystack)} "


def is_skipped_title(title: str | None) -> bool:
    """True for titles SPEC 5 says never to contact (interns, coordinators, recruiters, ...)."""
    folded = _fold_title(title)
    return any(_contains(folded, phrase.split()) for phrase in SKIP_TITLE_PHRASES)


def map_title_to_role(title: str | None, roles: Sequence[Role]) -> str | None:
    """The Roles-tab role whose title appears in this title as a whole phrase; None if none or skipped.

    Case, punctuation, "&"/"and", "of", and common long forms (Chief Executive Officer ->
    CEO, Vice President -> VP, Dir. -> Director, Human Resources -> HR) do not matter. The
    longest matching phrase wins; on a tie, the role listed first on the tab.
    """
    folded = _fold_title(title)
    if not folded or is_skipped_title(title):
        return None
    best: tuple[int, int] = (0, 0)
    best_role: str | None = None
    for role in roles:
        for t in role.titles:
            phrase = _fold_title(t)
            if _contains(folded, phrase):
                score = (len(phrase), len(" ".join(phrase)))
                if score > best:
                    best, best_role = score, role.role
    return best_role


# -- size --------------------------------------------------------------------

def _band_bounds(band: str) -> tuple[int, int]:
    lo, _, hi = band.partition("-")
    return int(lo), int(hi)


def size_band(employees: int | float | str | None) -> str | None:
    """The SIZE_BANDS band for a headcount ("64" -> "50-99"); None outside 10 to 249 or unknown."""
    if employees is None or isinstance(employees, bool):
        return None
    try:
        n = int(float(str(employees).replace(",", "").strip()))
    except (ValueError, OverflowError):
        return None
    for band in SIZE_BANDS:
        lo, hi = _band_bounds(band)
        if lo <= n <= hi:
            return band
    return None
