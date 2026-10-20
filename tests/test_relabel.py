"""`us-outbound labels set | show` (ops/relabel.py; Harry, 7 Oct 2026): a company's label corrected by hand, and its
label story. (`relabel` and `labels audit` went in W2-4, 20 Oct 2026, with their tests.)

The world is tests/test_enrol.py's three accounts (acc-1 Advertising agencies, acc-2 Fintech, acc-3 Advertising
agencies), live, every call through the real clients and the guard. (An open send-approval card under a wrong label
was withdrawn too, until send approvals went with W2-1.)"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from tests.test_enrol import default_openers  # noqa: F401  (the autouse fixture)
from tests.test_enrol import make
from tests.test_render import AGENCIES, TECH, make_settings
from us_outbound.clean.domains import is_public_body
from us_outbound.ops import relabel
from us_outbound.settings.model import Industry

INDUSTRIES = (
    Industry(AGENCIES, AGENCIES, True, naics_prefixes=("5418",), apollo_keywords=("marketing agency",), priority=2),
    Industry("Advertising agencies", AGENCIES, True, naics_prefixes=("541810",),
             apollo_keywords=("advertising agency",), priority=2),
    Industry(TECH, TECH, True, naics_prefixes=("5415",), apollo_keywords=("saas",), priority=1),
    Industry("Fintech", TECH, True, naics_prefixes=("5415",), apollo_keywords=("fintech",), priority=1),
    Industry("Games studios", TECH, True, naics_prefixes=("541511",), apollo_keywords=("video games",), priority=1),
)


def fact(ctx, aid: str, name: str, value, at: datetime | None = None) -> None:
    ctx.store.insert("signal_events", [{"event_id": f"{aid}-{name}-{(at or ctx.now).isoformat()}", "account_id": aid,
                                        "source": "apollo_org", "fact": name, "value": value, "quote": "",
                                        "source_url": "", "observed_at": at or ctx.now}])


class FakeOverrides:
    """The settings sheet's Overrides tab behind the transport: read, a cell updated, rows appended."""

    HEAD = ["domain", "field", "value", "note"]

    def __init__(self, t, rows=()):
        self.rows = [dict(r) for r in rows]
        self.appended: list[list[str]] = []
        t.route("GET", "Overrides", fn=lambda r: {"values": [self.HEAD] + [[x.get(h, "") for h in self.HEAD]
                                                                           for x in self.rows]})
        t.route("PUT", "Overrides", fn=self._put)
        t.route("POST", ":append", fn=self._append)

    def _put(self, req):
        row = int(req.json["range"].rsplit("C", 1)[1]) - 2  # the value column, C
        self.rows[row]["value"] = req.json["values"][0][0]
        return {}

    def _append(self, req):
        for values in req.json["values"]:
            self.appended.append(values)
            self.rows.append(dict(zip(self.HEAD, values)))
        return {}


def at(ctx, when: datetime, *, live: bool | None = None, job: str = "relabel") -> None:
    ctx.now = when
    ctx.job = job
    ctx.guard.configure(job=job, **({} if live is None else {"live": live}))


def world():
    s = make_settings(live_sending=True, approver_slack_ids=("U_HARRY",))
    ctx, t = make(live=True, settings=s)
    ctx.settings = dataclasses.replace(ctx.settings, industries=INDUSTRIES)
    fact(ctx, "acc-1", "naics", ["541810"])  # an advertising code, but no keyword: the agencies umbrella
    fact(ctx, "acc-2", "naics", ["541511"])
    fact(ctx, "acc-2", "keywords", ["fintech", "payments"])  # stays Fintech
    at(ctx, ctx.now, job="relabel")
    return ctx, t


@pytest.mark.parametrize("domain, public", [
    ("braintreema.gov", True), ("www.army.mil", True), ("https://ci.boston.ma.us/x", True), ("k12.ny.us", True),
    ("co.kings.ny.us", True), ("joe@town.lexington.ma.us", True),
    ("acme.us", False), ("governance.com", False), ("gov.uk.example.com", False), ("acme.com", False), ("", False),
])
def test_public_bodies(domain, public):
    assert is_public_body(domain) is public





def test_labels_set_corrects_a_company_and_show_tells_the_story(capsys):
    from us_outbound.ops import cli

    ctx, t = world()
    sheet = FakeOverrides(t)
    at(ctx, ctx.now, live=False, job="labels_set")
    dry = relabel.set_label(ctx, "https://www.loopstudio.com/", "fintech")
    assert dry == {"dry_run": True, "domain": "loopstudio.com", "from": "Advertising agencies", "to": "Fintech",
                   "active": True}
    assert ctx.store.get("accounts", account_id="acc-3")["industry"] == "Advertising agencies"
    at(ctx, ctx.now, live=True, job="labels_set")
    out = relabel.set_label(ctx, "loopstudio.com", "fintech")
    assert out["sheet"] == "added" and "cards_withdrawn" not in out
    assert sheet.appended[0][:3] == ["loopstudio.com", "industry", "Fintech"]
    shown = relabel.show(ctx, "loopstudio.com")
    assert (shown["industry"], shown["label_source"], shown["copy_level"]) == ("Fintech", "approver", "label")
    assert shown["history"][0]["corrected"] == "Advertising agencies → Fintech" and shown["history"][0]["via"] == "cli"
    with pytest.raises(LookupError, match="no company with the domain 'nobody.com'"):
        relabel.set_label(ctx, "nobody.com", "fintech")
    with pytest.raises(ValueError, match="no Industries label called 'crypto'"):
        relabel.set_label(ctx, "loopstudio.com", "crypto")
    assert cli.main(["labels", "show", "loopstudio.com"], context_factory=lambda *a, **k: ctx) == 0
    assert '"label_source": "approver"' in capsys.readouterr().out
    assert cli.main(["labels", "set", "loopstudio.com"], context_factory=lambda *a, **k: ctx) == 2
    assert "labels set needs the label" in capsys.readouterr().err
