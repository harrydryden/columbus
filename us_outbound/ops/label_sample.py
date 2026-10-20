"""Labelled companies judged by hand, a batch a month (Harry, 10 Oct 2026: "pull the 50-company sample"; 11 Oct:
"the model classification can continuously improve over time"), so the labels' accuracy is measured against a person,
not against the label check itself, and the judged companies become the label check's answer key.

  * The Label sample tab on the settings sheet holds every batch: the company, its site, what it does, the label and
    copy we use and how it was decided, and three columns for Harry (your_verdict: the right label, or one of
    VERDICTS; right_label; note). A batch is never redrawn: judged rows stay, as the answer key.
  * `label_sample_monthly` (the 1st of each month, 09:15) scores last month's batch, adds a new one of MONTHLY
    companies not on the tab yet, and posts both to Slack with a link to the tab (say.sheet_link) and what to fill in.
    The draft definition changes for the misses follow (learn/definition_suggestions.py, the writing model and the
    answer key asked live), then the ICP rule proposals from the hand-check (learn/precision.propose).
  * `us-outbound labels eval --from-sample` scores the model on every judged company (labels.judged_rows).

W3-2 (20 Oct 2026; code-verify-labels B11, redteam-stats §1.5 and §3 L4):
  * an `outside` verdict scores an opinion of no label (the model's none, the rules' no label) as right, where it
    read as a wrong group; and outside rows join the answer key as negatives (labels.judged_rows: the check should
    leave them out);
  * "untrue pitch" reads the copy level enrol stamped on the company's contact (contacts.copy_level) when it was
    emailed: the row's copy column is the level at the draw, which can differ from what was sent;
  * the monthly draw puts REJUDGE earlier judged companies back among the new rows, unmarked (a domain on the tab
    twice is one judged again), so the score counts each company once, by its first verdict, and the post reports how
    often the second verdict matched the first (agreement: the rater's drift, T1r);
  * the label policy (labels.policy; L4, Harry's D20) is computed from the last labels.POLICY_BATCHES batches by how
    each company's label was decided (by_source) and stored as a label_policy fact each month (labels.store_policy),
    with the sample's score, which the Monday readout reads (learn/label_metrics.py). It is applied only while
    General copy_level_policy is yes.

Otherwise the job asks no model and changes no company: it reads the database, writes the Label sample tab (live)
and its facts. settings_sync does not read the tab.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from us_outbound import fmt, labels
from us_outbound.base import say
from us_outbound.clients.guard import GuardViolation
from us_outbound.clients.sheets import read_tab, write_tab
from us_outbound.context import Context
from us_outbound.learn import definition_suggestions, stats
from us_outbound.logs import log
from us_outbound.settings.model import Industry, Settings

JOB = "labels_sample"
MONTHLY_JOB = "label_sample_monthly"
TAB = "Label sample"
SIZE = 50  # the first batch (Harry judged 50 on 11 Oct 2026)
MONTHLY = 25
# Earlier judged companies put back unmarked in each monthly batch (T1r; redteam-stats §1.5: one rater, so a drift
# in his standard reads as a drift in the labels unless some rows are judged twice).
REJUDGE = 2
SEED = "label-sample"  # the same accounts and batch draw the same companies
EMAILED = ("verified", "enrolled", "engaged", "demo_requested", "demo_booked")
RIGHT, RIGHT_GROUP, WRONG_GROUP, OUTSIDE = VERDICTS = ("right", "right group", "wrong group", "outside")
COLUMNS = ["batch", "domain", "company", "website", "they_do", "our_label", "our_group", "copy", "decided_by",
           "rules_said", "model_said", "your_verdict", "right_label", "note"]
# What a verdict means for the pitch: the copy a company gets is wrong when it says more than the verdict allows.
WRONG_PITCH = {labels.LABEL_COPY: {RIGHT_GROUP, WRONG_GROUP, OUTSIDE}, labels.GROUP_COPY: {WRONG_GROUP, OUTSIDE},
               labels.GENERAL_COPY_LEVEL: set()}
NO_LABEL_SAID = ("none", "no label")  # an opinion of no label: right for a company judged outside


def batch_name(ctx: Context) -> str:
    """This month's batch, "2026-11" (UK time)."""
    return ctx.today_uk().strftime("%Y-%m")


def sent_levels(ctx: Context, account_ids: Iterable[str]) -> dict[str, str]:
    """account id -> the copy level enrol stamped on its emailed contact (contacts.copy_level; W1-6), for the
    accounts emailed with a stamp. A company not emailed, or emailed before the stamp, is not in it."""
    ids = list(dict.fromkeys(str(i) for i in account_ids if i))
    if not ids:
        return {}
    out: dict[str, str] = {}
    for c in ctx.store.select("contacts", {"account_id": ids}):
        if c.get("enrolled_at") and str(c.get("copy_level") or "") in labels.LEVELS:
            out.setdefault(str(c["account_id"]), str(c["copy_level"]))
    return out


def _row(a: Mapping[str, Any], sv: labels.StoredVerdict | None, batch: str, level: str) -> dict[str, str]:
    v = sv.verdict if sv else None
    domain = str(a.get("domain") or "")
    conf = str(a.get("label_confidence") or "")
    return {
        "batch": batch, "domain": domain, "company": str(a.get("clean_name") or domain),
        "website": f"https://{domain}", "they_do": (v.what_they_do or v.evidence) if v else "",
        "our_label": str(a.get("industry") or ""), "our_group": str(a.get("industry_group") or ""),
        "copy": level,
        "decided_by": str(a.get("label_source") or "not checked") + (f" ({conf})" if conf else ""),
        "rules_said": (sv.rules or "no label") if sv else "",
        "model_said": f"{v.label} ({v.confidence})" if v else "not asked",
        "your_verdict": "", "right_label": "", "note": "",
    }


def rows_for(ctx: Context, accounts: Sequence[Mapping[str, Any]], batch: str) -> list[dict[str, str]]:
    """The tab's rows for these accounts (empty verdict columns), by domain: what we use for each and how it was
    decided, the copy level the one sent (sent_levels) or, not emailed yet, the one its label earns now."""
    ids = [str(a["account_id"]) for a in accounts]
    checks: dict[str, list[dict]] = {}
    if ids:
        for e in ctx.store.select("signal_events", {"account_id": ids, "source": labels.JOB,
                                                    "fact": labels.VERDICT_FACT}):
            checks.setdefault(str(e["account_id"]), []).append(e)
    sent = sent_levels(ctx, ids)
    return [_row(a, labels.latest_stored(checks.get(str(a["account_id"]), [])), batch,
                 sent.get(str(a["account_id"])) or labels.copy_level(a))
            for a in sorted(accounts, key=lambda a: str(a.get("domain") or ""))]


def draw(ctx: Context, size: int = SIZE, seed: str = SEED, *, batch: str = "",
         exclude: frozenset[str] = frozenset()) -> list[dict[str, str]]:
    """`size` companies at random from those emailed or to be emailed and not in `exclude` (domains), as the tab's
    rows (empty verdict columns)."""
    accounts = [a for a in ctx.store.select("accounts", {"status": list(EMAILED)})
                if str(a.get("industry") or "") and str(a.get("domain") or "") not in exclude]
    accounts.sort(key=lambda a: str(a["account_id"]))
    chosen = random.Random(f"{seed}:{batch}" if batch else seed).sample(accounts, min(size, len(accounts)))
    return rows_for(ctx, chosen, batch)


FIRST_BATCH = "2026-10"  # rows written before the batch column (Harry's first 50)


def split_repeats(rows: Sequence[Mapping[str, str]]) -> tuple[list[Mapping[str, str]], list[Mapping[str, str]]]:
    """(each domain's first row, the rows that repeat a domain already on the tab): a repeat is a company judged
    again (REJUDGE), so it is scored once, by its first verdict, and read for agreement. The tab is in the order it
    was written, oldest first."""
    seen: set[str] = set()
    first, again = [], []
    for r in rows:
        d = str(r.get("domain") or "").strip().lower()
        (again if d in seen else first).append(r)
        seen.add(d)
    return first, again


def rejudge_rows(ctx: Context, kept: Sequence[Mapping[str, str]], batch: str, n: int) -> list[dict[str, str]]:
    """n companies judged in an earlier batch and not judged again yet, drawn at random (the same each run of a
    batch), as new rows of this batch: the company as it stands now, with nothing to say it was judged before."""
    first, again = split_repeats(kept)
    twice = {str(r.get("domain") or "").strip().lower() for r in again}
    candidates = sorted({str(r["domain"]).strip().lower() for r in first
                         if str(r.get("your_verdict") or "").strip() and r.get("batch") != batch
                         and str(r["domain"]).strip().lower() not in twice})
    if not candidates or n <= 0:
        return []
    found = {str(a.get("domain") or "").lower(): a for a in ctx.store.select("accounts", {"domain": candidates})}
    pool = [d for d in candidates if d in found]
    chosen = random.Random(f"{SEED}:rejudge:{batch}").sample(pool, min(n, len(pool)))
    return rows_for(ctx, [found[d] for d in chosen], batch)


def add_batch(ctx: Context, size: int, batch: str, rejudge: int = 0) -> dict[str, Any]:
    """Draw `size` companies not on the tab yet, with `rejudge` judged before put back among them (rejudge_rows),
    and, with --live, write them under the rows already there, which are kept as they are (a judged row is the
    answer key). Returns what was drawn and the rows now on the tab."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    rows = read_tab(ctx.clients.sheets, sheet_id, TAB) if sheet_id else None
    kept = [{c: str(r.get(c) or (FIRST_BATCH if c == "batch" else "")) for c in COLUMNS} for r in rows or []
            if str(r.get("domain") or "").strip()]
    cells = draw(ctx, size, batch=batch, exclude=frozenset(r["domain"] for r in kept))
    again = rejudge_rows(ctx, kept, batch, rejudge)
    new = sorted([*cells, *again], key=lambda r: r["domain"])  # unmarked: in domain order like the rest
    written = False
    if ctx.live and sheet_id and new:
        write_tab(ctx.clients.sheets, sheet_id, TAB, COLUMNS, [*kept, *new], rows)
        written = True
    log(JOB, run_id=ctx.run_id, batch=batch, drawn=len(cells), rejudged=len(again), written=written, kept=len(kept))
    return {"batch": batch, "drawn": len(cells), "rejudged": len(again), "rows": new, "tab_written": written,
            "verdicts_kept": sum(1 for r in kept if r["your_verdict"].strip()), "on_tab": [*kept, *new]}


def run(ctx: Context, size: int = SIZE) -> dict[str, Any]:
    """A batch of `size` added now (this month's), live only: the monthly job's first half, alone."""
    out = add_batch(ctx, size, batch_name(ctx))
    out.pop("on_tab")
    return out


interval = stats.wilson  # the Wilson 95% interval for k of n (learn/stats.py, W2-3: one copy)


def _named(text: Any) -> str:
    """A label as the tab writes it, without the confidence after it ("Edtech (medium)" is Edtech)."""
    return str(text or "").split(" (")[0].strip()


def _against(truth: Industry | str, said: Any, settings: Settings) -> str | None:
    """How a label stands against the right one: RIGHT, RIGHT_GROUP, WRONG_GROUP, or None when no opinion was given.
    For a company judged outside, an opinion of no label (the model's none, the rules' no label) is RIGHT (W3-2;
    code-verify-labels B11: it read as a wrong group)."""
    name = _named(said)
    if truth == OUTSIDE:
        if name.lower() in NO_LABEL_SAID:
            return RIGHT
        return None if not name or name.lower() == "not asked" else WRONG_GROUP
    ind = settings.label(name)
    if ind is None:
        return None if not name or name in ("no label", "not asked") else WRONG_GROUP
    assert isinstance(truth, Industry)
    if ind.industry == truth.industry:
        return RIGHT
    return RIGHT_GROUP if ind.industry_group == truth.industry_group else WRONG_GROUP


def read(r: Mapping[str, Any], settings: Settings) -> tuple[str, Industry | str | None] | str | None:
    """One row's verdict as (verdict, the right label: an Industry, OUTSIDE, or None when unknown); None when not
    judged yet; the domain (a str) when the verdict cannot be read. A verdict is one of VERDICTS, or the right label
    itself (Harry, 11 Oct 2026, wrote labels in the column), in any case."""
    said = " ".join(str(r.get("your_verdict") or "").split())
    word = said.lower()
    truth: Industry | str | None = None
    if word in VERDICTS:
        if word == RIGHT:
            truth = settings.label(_named(r.get("our_label")))
        elif word == OUTSIDE:
            truth = OUTSIDE
        else:
            truth = settings.label(r.get("right_label"), fuzzy=True)
        return word, truth
    if said and (truth := settings.label(said, fuzzy=True)) is not None:
        return _against(truth, r.get("our_label"), settings) or WRONG_GROUP, truth
    if said:
        return str(r.get("domain") or "")
    return None


def score(rows: Sequence[Mapping[str, str]], settings: Settings,
          sent: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Harry's verdicts counted: label right, group right, and emails whose copy says more than the verdict allows.

    sent: domain -> the copy level enrol stamped (sent_levels by domain): the pitch is judged on what was sent, and
    on the row's copy column only for a company not emailed (W3-2; B11). Where the right label is known (written,
    "right", "outside", or right_label beside a verdict), the rules' and the model's labels are scored against it
    too: which of the three opinions the sheet should trust. label_copy: the rows sent a label's own copy, apart
    (RT §3 L4: in October 4 of 30 such emails pitched the wrong label). by_source: (exactly right, judged) by how
    the label was decided (decided_by), what the label policy reads."""
    judged, unreadable = [], []
    for r in rows:
        got = read(r, settings)
        if got is None:
            continue
        if isinstance(got, str):
            unreadable.append(got)
            continue
        judged.append((r, *got))
    sent = sent or {}

    def level(r: Mapping[str, str]) -> str:
        return sent.get(str(r.get("domain") or "").strip().lower()) or str(r.get("copy") or "")

    n = len(judged)
    verdicts = Counter(v for _, v, _ in judged)
    wrong_pitch = [r for r, v, _ in judged if v in WRONG_PITCH.get(level(r), set())]
    label_right, group_right = verdicts[RIGHT], verdicts[RIGHT] + verdicts[RIGHT_GROUP]
    opinions: dict[str, dict[str, int]] = {}
    for name, column in (("ours", "our_label"), ("rules", "rules_said"), ("model", "model_said")):
        c: Counter[str] = Counter()
        for r, _, truth in judged:
            if truth is None:
                continue
            got = _against(truth, r.get(column), settings)
            c["judged"] += 1
            c["gave_none"] += got is None
            c["label_right"] += got == RIGHT
            c["group_right"] += got in (RIGHT, RIGHT_GROUP)
        opinions[name] = {k: c[k] for k in ("judged", "gave_none", "label_right", "group_right")}
    lc = [v for r, v, _ in judged if level(r) == labels.LABEL_COPY]
    by_source: dict[str, list[int]] = {}
    for r, v, _ in judged:
        cell = by_source.setdefault(_named(r.get("decided_by")) or "not checked", [0, 0])
        cell[0] += v == RIGHT
        cell[1] += 1
    return {
        "rows": len(rows), "judged": n, "unreadable": unreadable, "verdicts": dict(verdicts),
        "label_right": label_right, "group_right": group_right, "wrong_pitch": len(wrong_pitch),
        "intervals": {"label_right": interval(label_right, n), "group_right": interval(group_right, n),
                      "wrong_pitch": interval(len(wrong_pitch), n)},
        "label_copy": {"judged": len(lc), "label_right": sum(v == RIGHT for v in lc),
                       "group_right": sum(v in (RIGHT, RIGHT_GROUP) for v in lc),
                       "wrong_pitch": sum(v in WRONG_PITCH[labels.LABEL_COPY] for v in lc)},
        "by_source": by_source,
        "opinions": opinions,
        "truths": [(str(r.get("domain") or ""), truth) for r, _, truth in judged
                   if isinstance(truth, Industry) or truth == OUTSIDE],
        "wrong": [{"domain": str(r.get("domain") or ""), "ours": str(r.get("our_label") or ""),
                   "copy": level(r), "verdict": v,
                   "right_label": (truth.industry if isinstance(truth, Industry) else str(r.get("right_label") or ""))}
                  for r, v, truth in judged if v != RIGHT],
    }


def judged(rows: Sequence[Mapping[str, str]], settings: Settings) -> list[tuple[str, Industry | str]]:
    """The judged companies whose right answer is known: (domain, its label, or OUTSIDE), each company once by its
    first verdict, for `labels eval --from-sample` and the answer key."""
    return list(score(split_repeats(rows)[0], settings)["truths"])


def sample_rows(ctx: Context) -> list[tuple[str, Industry | str]]:
    """The Label sample tab's judged companies with their right answer (none when the tab is missing)."""
    sheet_id = ctx.guard.bounds.settings_sheet_id
    rows = read_tab(ctx.clients.sheets, sheet_id, TAB) if sheet_id else None
    return judged(rows or [], ctx.settings)


def _key(got: tuple[str, Industry | str | None]) -> str:
    verdict, truth = got
    return truth.industry if isinstance(truth, Industry) else str(truth or verdict)


def agreement(rows: Sequence[Mapping[str, str]], settings: Settings) -> tuple[int, int]:
    """(second verdicts matching the first, companies judged twice with both verdicts read): the rater's agreement
    with himself (T1r). Two verdicts match when they give the same right label (or both outside); a verdict with no
    right label (a wrong group with right_label blank) is compared by its word."""
    first, again = split_repeats(rows)
    by_domain = {str(r.get("domain") or "").strip().lower(): r for r in first}
    agreed = n = 0
    for r in again:
        before = by_domain.get(str(r.get("domain") or "").strip().lower())
        a, b = (read(before, settings) if before else None), read(r, settings)
        if not isinstance(a, tuple) or not isinstance(b, tuple):
            continue
        n += 1
        agreed += _key(a) == _key(b)
    return agreed, n


# -- the monthly batch (Harry, 11 Oct 2026) -----------------------------------------------------------------------


def previous_batch(batch: str) -> str:
    """The month before "2026-11": "2026-10"."""
    year, month = (int(x) for x in batch.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def _share(k: int, n: int) -> str:
    return f"{k / n:.0%}" if n else "-"


def summary_line(name: str, out: Mapping[str, Any]) -> str:
    """"October: 44 of 50 judged · label right 77% · group right 95% · untrue pitch 5%"."""
    n = out["judged"]
    if not n:
        return f"{name}: none judged yet"
    return (f"{name}: {n} of {out['rows']} judged · label right {_share(out['label_right'], n)} · group right "
            f"{_share(out['group_right'], n)} · untrue pitch {_share(out['wrong_pitch'], n)}")


def summary(scored: Mapping[str, Any], agreed: tuple[int, int] = (0, 0)) -> dict[str, Any]:
    """What the label_policy fact keeps of a score (labels.store_policy), for the Monday readout."""
    return {"judged": scored["judged"], "label_right": scored["label_right"], "group_right": scored["group_right"],
            "wrong_pitch": scored["wrong_pitch"], "label_copy": dict(scored["label_copy"]),
            "rejudged": agreed[1], "rejudge_agreed": agreed[0]}


def policy_line(value: Mapping[str, Any]) -> str:
    """"rules+model 20 of 21 exactly right → label copy · model 6 of 9 → group copy · ...": each source the policy
    routes, with the rows it rests on (sources with none judged are left out)."""
    parts = []
    for s in labels.POLICY_SOURCES:
        k, n = (value.get("counts") or {}).get(s) or (0, 0)
        if n:
            level = (value.get("policy") or {}).get(s, labels.GROUP_COPY)
            parts.append(f"{s} {k} of {n} exactly right → {level} copy")
    return " · ".join(parts) or "no judged rows yet"


def monthly(ctx: Context) -> dict[str, Any]:
    """`label_sample_monthly`: score last month's batch and every batch, add this month's (with REJUDGE companies
    judged before), store the label policy, and post to Slack with a link to the tab. In dry-run the batch is not
    written and the post goes to the dev channel."""
    from us_outbound.base import notify
    from us_outbound.learn import precision

    batch = batch_name(ctx)
    last = previous_batch(batch)
    added = add_batch(ctx, MONTHLY, batch, rejudge=REJUDGE)
    rows = added.pop("on_tab")
    earlier = [r for r in rows if r["batch"] != batch]
    first, _ = split_repeats(earlier)
    ids = {str(a.get("domain") or "").lower(): str(a["account_id"]) for a in ctx.store.select(
        "accounts", {"domain": sorted({str(r["domain"]).lower() for r in first})})} if first else {}
    by_id = sent_levels(ctx, ids.values())
    sent = {d: by_id[i] for d, i in ids.items() if i in by_id}
    last_rows = [r for r in first if r["batch"] == last]
    scored_last = score(last_rows, ctx.settings, sent)
    scored_all = score(first, ctx.settings, sent)
    agreed = agreement(earlier, ctx.settings)
    batches = sorted({r["batch"] for r in first})[-labels.POLICY_BATCHES:]
    pooled = score([r for r in first if r["batch"] in batches], ctx.settings, sent)
    counts = {s: (int(k), int(n)) for s, (k, n) in pooled["by_source"].items()}
    policy = labels.store_policy(ctx, counts, batches, summary(pooled, agreed))
    link = say.sheet_link(ctx, TAB)
    new = added["drawn"] + added["rejudged"]
    head = f"{fmt.plural(new, 'company', 'companies')} to check: is our industry label right?"
    facts = [f"Each month a person checks the industry we gave some of the companies we email. This month's "
             f"{new} new companies to judge ({batch}) are on the {link}."]
    do = [f"On the {link}, fill `your_verdict` for each new row: the right label, or right, right group, wrong group "
          "or outside."]
    if last_rows:
        facts.append(f"• {summary_line(f'Last month ({last})', scored_last)}")
    if scored_all["judged"]:
        facts.append(f"• {summary_line('Every batch', scored_all)}")
        lc = scored_all["label_copy"]
        if lc["judged"]:
            facts.append(f"• Emails sent a label's own copy: {lc['wrong_pitch']} of {lc['judged']} pitched the wrong "
                         "label.")
        unjudged = sum(1 for r in last_rows if not str(r.get("your_verdict") or "").strip())
        if unjudged:
            facts.append(f"• {unjudged} of last month's companies are not judged yet.")
    if agreed[1]:
        facts.append(f"• Judged again: {agreed[1]} {'company' if agreed[1] == 1 else 'companies'} you judged before "
                     f"came back unmarked; your verdict matched the first for {agreed[0]} of {agreed[1]}.")
    if pooled["judged"]:
        applied = ("applied (General `copy_level_policy` is yes)" if ctx.settings.general.copy_level_policy
                   else "not applied: General `copy_level_policy` is no, so nothing changes")
        facts.append(f"• Label policy ({', '.join(batches)}): {policy_line(policy)}; {applied}.")
    if last_rows or scored_all["judged"]:
        facts.append("_Label right: the exact industry. Group right: at least its wider group. Untrue pitch: the "
                     "email said more about the company than the verdict allows._")
    answer_key = labels.gold_rows() + labels.judged_rows(ctx, judged(first, ctx.settings))
    suggested = definition_suggestions.suggest(ctx, first, answer_key, batch)
    if suggested["suggestions"]:
        better = "no worse" if suggested.get("better") else "worse, so check them before approving"
        changes = fmt.plural(len(suggested["suggestions"]), "definition change")
        suggestions = say.sheet_link(ctx, definition_suggestions.TAB)
        facts.append(f"• {changes} drafted for the labels the check confused ({', '.join(suggested['labels'])}):"
                     f" {suggestions}. Tried on the companies whose right label we know: now {suggested['now']}; "
                     f"with the changes {suggested['later']} ({better}).")
        do.append(f"On the {suggestions}, set `decision` to approve or reject. Approved ones go onto the Industries "
                  "tab at the next settings sync, and every company is checked again under them.")
    elif suggested["error"]:
        facts.append(f"• No definition changes drafted: {suggested['error']}.")
    try:  # the proposals never cost the month's post: a failure is said in it
        proposed = precision.propose(ctx, answer_key)
    except GuardViolation:
        raise
    except Exception as exc:
        log("icp_proposals_failed", run_id=ctx.run_id, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        proposed = {"rows": [], "written": False, "error": f"the proposals could not be made ({type(exc).__name__})"}
    if proposed["rows"]:
        tab = say.sheet_link(ctx, precision.SUGGESTIONS_TAB)
        open_ = [r for r in proposed["rows"] if r["status"] == precision.PROPOSED]
        facts.append(f"• {fmt.plural(len(proposed['rows']), 'ICP rule')} drafted from the hand-check's leave-out "
                     f"reasons ({', '.join(r['kind'] + ' ' + r['target'] for r in proposed['rows'])}), "
                     f"{len(open_)} for you to decide: {tab}.")
        if open_:
            do.append(f"On the {tab}, set `status` to approved or rejected. Only approved rows are applied, at the "
                      "next settings sync.")
    elif proposed.get("error"):
        facts.append(f"• No ICP rules drafted: {proposed['error']}.")
    lines = [say.compose(say.DECIDE, head, facts, do=do)]
    posted = notify.alert(ctx, "\n".join(lines))
    out = {**added, "last_batch": last, "last": scored_last, "all": scored_all, "link": link, "agreement": agreed,
           "policy": policy, "suggestions": suggested, "proposals": proposed, "posted": posted.get("posted")}
    log(MONTHLY_JOB, run_id=ctx.run_id, batch=batch, drawn=added["drawn"], rejudged=added["rejudged"],
        last_judged=scored_last["judged"], all_judged=scored_all["judged"], posted=posted.get("posted"))
    return out
