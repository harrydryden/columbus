"""The industry crosswalk (Harry, 9 Oct 2026: "translate between tool definitions with a high degree of accuracy").

Apollo describes a company in three vocabularies: its NAICS codes (Apollo infers them), its industry (LinkedIn's
list, which HubSpot and Clay use too) and its keyword tags (free text). The Industries tab translates them into
Spill's labels (apollo_universe.best_label over naics_prefixes, exclude_naics and apollo_keywords). This measures
how well each piece of that translation holds, against the companies whose label is known:
  * an approver's correction (label_corrected), else
  * the label check's verdict when the model was sure (high): a label on the tab, or OUTSIDE when it was sure the
    company is an association or a public body, or that no label fits.
For each Apollo value (an industry, a NAICS code at six digits and at four, a keyword tag), each term on the tab and
each label the rules give, it counts the known companies carrying it and the share whose label, and whose group,
it predicts: its precision. A label inside a group is judged on its label; a group's own (umbrella) label, which
the rules give when the codes alone place a company, is judged on its group. A translation is reliable at PURE or
more over MIN_SUPPORT companies, poor under POOR; a value the tab does not translate is worth adding at PURE.

`us-outbound labels crosswalk` prints the summary and the rows that need a look; with --live it also writes every
row to the Crosswalk tab of the settings sheet (settings_sync does not read it). Read-only otherwise: it asks no
model and spends no credits.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from us_outbound import facts, labels
from us_outbound.context import Context
from us_outbound.industry.material import naics_codes, texts
from us_outbound.logs import log
from us_outbound.settings.conditions import find_terms
from us_outbound.settings.model import Industry, Settings
from us_outbound.sources import apollo_universe as universe

JOB = "labels_crosswalk"
TAB = "Crosswalk"
COLUMNS = ["kind", "value", "tab_says", "companies", "share_right", "verdict", "top_label", "label_share",
           "top_group", "group_share"]
OUTSIDE = "(outside our labels)"  # an association, a public body, or a company no label fits, the model sure
NO_RULES = "(no rules label)"
MIN_SUPPORT = 5  # known companies under which a row is "too few to tell"
PURE, POOR = 0.90, 0.60
CHUNK = 500
RULES, INDUSTRY, NAICS6, NAICS4, TERM, TAG = ("rules label", "apollo industry", "naics", "naics (4 digits)",
                                              "tab term", "apollo tag")
KINDS = (RULES, INDUSTRY, NAICS6, NAICS4, TERM, TAG)


@dataclass(frozen=True)
class Known:
    """A company whose label is known, and what Apollo says of it."""

    account_id: str
    label: str  # a label on the tab, or OUTSIDE
    group: str  # its group, or OUTSIDE
    by: str  # "approver" or "model"
    industry: str = ""  # Apollo's industry, folded
    codes: tuple[str, ...] = ()  # Apollo's NAICS codes
    tags: tuple[str, ...] = ()  # Apollo's keyword tags, folded
    rules: str = NO_RULES  # the label the rules give now


@dataclass
class Row:
    """One translation: an Apollo value, a tab term or a rules label, and what the known companies carrying it are."""

    kind: str
    value: str
    tab_says: str = ""  # the label the tab translates it to now ("" for none)
    says_group: str = ""  # its group
    umbrella: bool = False  # tab_says is a group's own label: judged on the group
    labels: Counter = field(default_factory=Counter)
    groups: Counter = field(default_factory=Counter)
    right: int = 0  # known companies whose label (or group, for an umbrella) is tab_says's

    @property
    def companies(self) -> int:
        return sum(self.labels.values())

    def share_right(self) -> float | None:
        return self.right / self.companies if self.tab_says and self.companies else None

    def top(self, counts: Counter) -> tuple[str, float]:
        if not counts:
            return "", 0.0
        name, k = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        return name, k / self.companies

    def verdict(self) -> str:
        """reliable, poor, mixed or group only for a translation the tab makes; add or exclude for one it does not."""
        n = self.companies
        if n < MIN_SUPPORT:
            return "too few to tell"
        label, l_share = self.top(self.labels)
        group, g_share = self.top(self.groups)
        if not self.tab_says:
            if label == OUTSIDE and l_share >= PURE:
                return "exclude: almost all are outside our labels"
            if l_share >= PURE:
                return f"add to {label}"
            if g_share >= PURE:
                return f"add to the {group} group"
            return ""
        share = self.right / n
        if share >= PURE:
            return "reliable"
        if not self.umbrella and self.groups.get(self.says_group, 0) / n >= PURE:
            return "group only: it places the group, not the label"
        if share < POOR:
            return f"poor: mostly {label} ({l_share:.0%})"
        return "mixed"

    def as_cells(self) -> dict[str, str]:
        label, l_share = self.top(self.labels)
        group, g_share = self.top(self.groups)
        right = self.share_right()
        return {"kind": self.kind, "value": self.value, "tab_says": self.tab_says, "companies": str(self.companies),
                "share_right": f"{right:.0%}" if right is not None else "", "verdict": self.verdict(),
                "top_label": label, "label_share": f"{l_share:.0%}", "top_group": group, "group_share": f"{g_share:.0%}"}


def _known(evs: Sequence[Mapping[str, Any]], settings: Settings) -> tuple[str, str, str] | None:
    """(label, group, by) for a company whose label is known, else None."""
    corrected = labels.latest_correction(evs)
    if corrected and (ind := settings.industry(str(corrected.get("to") or ""))) is not None:
        return ind.industry, ind.industry_group, "approver"
    v = labels.latest_verdict(evs)
    if not v or v.get("confidence") != labels.HIGH:
        return None
    entity, label = str(v.get("entity") or ""), str(v.get("model") or labels.NONE)
    if entity in (labels.ASSOCIATION, labels.PUBLIC_BODY) or label == labels.NONE:
        return OUTSIDE, OUTSIDE, "model"
    ind = settings.industry(label)
    return (ind.industry, ind.industry_group, "model") if ind is not None and entity == labels.COMPANY else None


def _value(evs: Sequence[Mapping[str, Any]], fact: str) -> Any:
    return facts.value(evs, fact, universe.SOURCE)


def load(ctx: Context) -> list[Known]:
    """The companies whose label is known, with their Apollo facts and the rules' label now (four queries)."""
    s = ctx.settings
    checked: dict[str, list[dict]] = {}
    for e in ctx.store.select("signal_events", {"source": labels.JOB, "fact": [labels.VERDICT_FACT,
                                                                               labels.CORRECTED_FACT]}):
        checked.setdefault(str(e["account_id"]), []).append(e)
    ids = sorted(checked)
    apollo: dict[str, list[dict]] = {}
    accounts: dict[str, dict] = {}
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        for e in ctx.store.select("signal_events", {"account_id": chunk, "source": universe.SOURCE,
                                                    "fact": ["naics", "keywords", "apollo_industry"]}):
            apollo.setdefault(str(e["account_id"]), []).append(e)
        accounts.update({str(a["account_id"]): a for a in ctx.store.select("accounts", {"account_id": chunk})})
    out = []
    for aid in ids:
        k = _known(checked[aid], s)
        if k is None:
            continue
        evs = apollo.get(aid, [])
        rules = labels.rules_label(accounts.get(aid, {}), evs, s) if evs else None
        out.append(Known(aid, *k, industry=labels._fold(_value(evs, "apollo_industry") or ""),
                         codes=tuple(naics_codes(_value(evs, "naics"))),
                         tags=tuple(dict.fromkeys(labels._fold(t) for t in texts(_value(evs, "keywords")))),
                         rules=rules.industry if rules else NO_RULES))
    return out


def _translation(settings: Settings, codes: Sequence[str] = (), text: str = "") -> Industry | None:
    return universe.best_label(list(codes), text, settings)


def measure(known: Iterable[Known], settings: Settings) -> list[Row]:
    """Every row: the rules' labels, Apollo's industries, NAICS codes and tags, and the tab's terms."""
    rows: dict[tuple[str, str], Row] = {}
    by_name = {i.industry: i for i in settings.industries}

    def row(kind: str, value: str, says: Industry | None) -> Row:
        key = (kind, value)
        if key not in rows:
            rows[key] = Row(kind, value, says.industry if says else "", says.industry_group if says else "",
                            bool(says and says.is_umbrella))
        return rows[key]

    def count(r: Row, k: Known) -> None:
        r.labels[k.label] += 1
        r.groups[k.group] += 1
        if r.tab_says and (k.group == r.says_group if r.umbrella else k.label == r.tab_says):
            r.right += 1

    terms = [(ind, t) for ind in settings.industries for t in ind.apollo_keywords]
    for k in known:
        count(row(RULES, k.rules, by_name.get(k.rules)), k)
        if k.industry:
            count(row(INDUSTRY, k.industry, _translation(settings, text=k.industry)), k)
        for code in dict.fromkeys(k.codes):
            count(row(NAICS6, code, _translation(settings, codes=[code])), k)
        for code4 in dict.fromkeys(c[:4] for c in k.codes if len(c) >= 4):
            count(row(NAICS4, code4, _translation(settings, codes=[code4])), k)
        text = " ; ".join([*k.tags, k.industry]).strip(" ;")
        matched = {t.casefold() for ind, t in terms if text and find_terms(text, (t,))}
        for ind, t in terms:
            if t.casefold() in matched:
                count(row(TERM, f"{t} ({ind.industry})", ind), k)
        for tag in k.tags:
            if tag not in matched:
                count(row(TAG, tag, None), k)
    return list(rows.values())


def summary(known: Sequence[Known], rows: Sequence[Row]) -> dict[str, Any]:
    """The headline numbers and the rows that need a look."""
    n = len(known)
    by = Counter(k.by for k in known)
    exact = sum(1 for k in known if k.rules == k.label)
    labelled = [k for k in known if k.rules != NO_RULES]
    group_of = {r.value: r.says_group for r in rows if r.kind == RULES}
    same_group = sum(1 for k in labelled if group_of.get(k.rules) == k.group)

    def pick(test, limit=10) -> list[dict[str, str]]:
        found = [r for r in rows if r.companies >= MIN_SUPPORT and test(r)]
        return [r.as_cells() for r in sorted(found, key=lambda r: (-r.companies, r.kind, r.value))[:limit]]

    return {
        "known": n, "by_approver": by.get("approver", 0), "by_model": by.get("model", 0),
        "outside_our_labels": sum(1 for k in known if k.label == OUTSIDE),
        "rules": {"exact_label": exact, "same_group": same_group, "other_group": len(labelled) - same_group,
                  "no_rules_label": n - len(labelled)},
        "poor": pick(lambda r: r.verdict().startswith("poor")),
        "group_only": pick(lambda r: r.verdict().startswith("group only")),
        "add": pick(lambda r: r.verdict().startswith(("add", "exclude"))),
    }


def run(ctx: Context) -> dict[str, Any]:
    """`us-outbound labels crosswalk`: measure, and with --live write the Crosswalk tab."""
    known = load(ctx)
    rows = measure(known, ctx.settings)
    out = summary(known, rows)
    ordered = sorted(rows, key=lambda r: (KINDS.index(r.kind), -r.companies, r.value))
    cells = [r.as_cells() for r in ordered if r.companies]
    sheet_id = ctx.guard.bounds.settings_sheet_id
    written = False
    if ctx.live and sheet_id:
        sheets = ctx.clients.sheets
        if not sheets.read_tabs(sheet_id, [TAB]).get(TAB):
            try:
                sheets.add_tab(sheet_id, TAB)
            except Exception as exc:  # it is there, only empty
                log("crosswalk_tab_exists", error=str(exc)[:120])
        sheets.replace_tab(sheet_id, TAB, COLUMNS, cells)
        written = True
    out.update(rows=len(cells), tab_written=written)
    log("labels_crosswalk", run_id=ctx.run_id, known=out["known"], rows=len(cells), written=written)
    return out
