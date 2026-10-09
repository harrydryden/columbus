"""industry/material.py: what the Industries rules read about a company, parsed one way, and the entity check
before any label is tried (9 Oct 2026)."""

from __future__ import annotations

import dataclasses

import pytest

from us_outbound.industry.material import ASSOCIATION, PUBLIC_BODY, RulesInput, entity, naics_codes, texts
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

SETTINGS, _ = validate_all(default_tabs())


def test_one_reading_of_tags_and_codes_whatever_their_shape():
    assert texts(["saas", " fintech ", "", 3]) == ["saas", "fintech"]
    assert texts("saas, fintech") == ["saas", "fintech"]  # one copy read this as nothing, another as one tag
    assert texts(None) == [] and texts("  ") == []
    assert naics_codes(["541613", "5418"]) == ["541613", "5418"]
    assert naics_codes("541613, 5418") == ["541613", "5418"] and naics_codes(541613.0) == ["541613"]


def test_a_search_row_and_the_stored_facts_give_the_same_input():
    org = {"naics_codes": ["541613"], "keywords": ["marketing  automation", "saas"], "industry": "Computer Software"}
    row = RulesInput.from_org(org)
    assert row == RulesInput.from_facts(["541613"], "marketing automation, saas", " Computer Software ")
    assert row.keyword_text == "marketing automation ; saas ; Computer Software"
    assert RulesInput.from_org({"naics_code": "541511"}).codes == ("541511",)  # enrichment's single code
    assert not RulesInput.from_org({}) and RulesInput.from_org({"industry": "Banking"})


@pytest.mark.parametrize("codes, tags, industry, kind", [
    (("813910",), (), "", ASSOCIATION),  # business associations
    (("813920",), ("networking",), "", ASSOCIATION),  # professional organisations
    ((), ("Trade Association", "advocacy"), "", ASSOCIATION),
    ((), ("chamber of commerce",), "", ASSOCIATION),
    (("921110",), (), "", PUBLIC_BODY),  # executive offices
    ((), (), "Government Administration", PUBLIC_BODY),
    ((), ("association management software", "saas"), "Computer Software", None),  # sells to them
    (("561920",), ("trade shows",), "Events Services", None),  # trade show organisers are companies
    (("541613",), ("marketing automation",), "", None),
])
def test_what_is_never_an_employer_we_prospect(codes, tags, industry, kind):
    assert entity(RulesInput(codes, tags, industry), SETTINGS) == kind


def test_a_code_an_active_label_claims_is_the_sheets():
    """Emergency & rescue (922160) is switched off, so a fire department's code is a public body's; turned on, the
    label reaches it."""
    inp = RulesInput(("922160",))
    assert entity(inp, SETTINGS) == PUBLIC_BODY
    on = tuple(dataclasses.replace(i, active=True) if i.industry == "Emergency & rescue" else i for i in SETTINGS.industries)
    assert entity(inp, dataclasses.replace(SETTINGS, industries=on)) is None
