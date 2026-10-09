"""Apollo client: read only, endpoint shapes, paging limits, bulk match chunks."""

import inspect

import pytest

from tests.fakes import FakeTransport
from us_outbound.clients.apollo import (
    BULK_MATCH_FLAGS,
    READ_ENDPOINTS,
    Apollo,
    credits_left,
    enriched_in,
    normalize_filters,
    org_id,
    organizations_in,
    people_in,
    while_counted,
    while_full,
    while_full_page,
)
from us_outbound.clients.guard import APOLLO_READ_ACTIONS, Boundaries, Guard, GuardViolation, Op

BASE = "https://api.apollo.io/api/v1"
READ_METHODS = {
    "credit_usage",
    "search_organizations",
    "search_lookalike_organizations",  # sources/lookalike_leads.py (Harry, 5 Oct 2026)
    "enrich_organization",
    "bulk_enrich_organizations",
    "job_postings",
    "search_people",
    "bulk_match",
    "website_visitor_aggregates",
    "search_website_visitors",
}


def make(live=True):
    t = FakeTransport()
    guard = Guard(live=live, bounds=Boundaries())
    return Apollo(guard, t, "apollo-key"), t, guard


def exercise(apollo):
    apollo.credit_usage()
    apollo.search_organizations({"organization_locations[]": ["new york"]})
    apollo.search_lookalike_organizations(["org1"], {"organization_locations[]": ["United States"]})
    apollo.enrich_organization("acme.example")
    apollo.bulk_enrich_organizations(["acme.example", "beta.example"])
    apollo.job_postings("org1")
    apollo.search_people({"person_titles": ["Head of People"]})
    apollo.bulk_match([{"id": "p1"}])
    apollo.website_visitor_aggregates("spill.chat", ["org1"])
    apollo.search_website_visitors(["spill.chat"], days=30, pages=["/us"])


def test_there_are_no_write_methods():
    public = {n for n, _ in inspect.getmembers(Apollo, inspect.isfunction) if not n.startswith("_")}
    inherited = {"headers", "request", "iter_pages"}  # iter_pages pages a caller's own read (9 Oct 2026)
    assert public - inherited == READ_METHODS
    assert set(READ_ENDPOINTS) <= APOLLO_READ_ACTIONS
    for _, path in READ_ENDPOINTS.values():
        assert not any(w in path for w in ("/contacts", "/accounts", "/labels", "/emailer", "/sequences", "/tasks"))


@pytest.mark.parametrize("live", [True, False])
def test_every_call_is_a_read_through_the_guard(live):
    apollo, t, guard = make(live)
    exercise(apollo)
    assert guard.calls and all(c.system == "apollo" and not c.write and c.sent for c in guard.calls)
    assert {c.action for c in guard.calls} <= APOLLO_READ_ACTIONS
    allowed = {(m, BASE + p.format(organization_id="org1")) for m, p in READ_ENDPOINTS.values()}
    assert {(r.method, r.url) for r in t.requests} <= allowed
    assert all(r.headers["X-Api-Key"] == "apollo-key" and "Authorization" not in r.headers for r in t.requests)


def test_guard_refuses_any_apollo_write():
    with pytest.raises(GuardViolation):
        Guard(live=True).authorize("apollo", Op("labels.create", write=True))


def test_search_organizations_body_and_paging():
    apollo, t, guard = make()
    t.route("POST", "/mixed_companies/search", body={
        "organizations": [{"id": "o1", "name": "Acme", "primary_domain": "acme.example"}],
        "accounts": [{"id": "a1", "organization_id": "o2", "domain": "beta.example"}],
        "pagination": {"page": 2, "per_page": 100, "total_entries": 150, "total_pages": 2},
    })
    page = apollo.search_organizations(
        {
            "organization_locations[]": ["New York, US"],
            "organization_num_employees_ranges[]": ["10,19", "20,49"],
            "q_organization_keyword_tags": ["fintech"],
            "organization_naics_codes": ["5415"],
            "organization_num_jobs_range[min]": 3,
            "organization_num_jobs_range[max]": 50,
            "page": 9,
        },
        page=2,
    )
    [req] = t.requests
    assert (req.method, req.url) == ("POST", f"{BASE}/mixed_companies/search")
    assert req.json == {
        "organization_locations": ["New York, US"],
        "organization_num_employees_ranges": ["10,19", "20,49"],
        "q_organization_keyword_tags": ["fintech"],
        "organization_naics_codes": ["5415"],
        "organization_num_jobs_range": {"min": 3, "max": 50},
        "page": 2,
        "per_page": 100,
    }
    assert [(o["organization_id"], o["domain"]) for o in organizations_in(page)] == [("o1", "acme.example"), ("o2", "beta.example")]
    assert guard.calls[0].action == "organizations.search"
    for bad in ({"page": 501}, {"per_page": 101}, {"page": 0}):
        with pytest.raises(ValueError):
            apollo.search_organizations({}, **bad)
    assert len(t.requests) == 1


def test_lookalike_search_body_seeds_and_limits():
    """sources/lookalike_leads.py: the seeds as lookalike_organization_ids beside the usual filters, at most 5."""
    apollo, t, guard = make()
    apollo.search_lookalike_organizations(["o1", "o2", "o1"], {
        "organization_locations[]": ["United States"], "organization_not_locations": ["California, US"],
        "organization_num_employees_ranges": ["10,19"], "page": 7})
    [req] = t.requests
    assert (req.method, req.url) == ("POST", f"{BASE}/mixed_companies/search")
    assert req.json == {"organization_locations": ["United States"], "organization_not_locations": ["California, US"],
                        "organization_num_employees_ranges": ["10,19"], "lookalike_organization_ids": ["o1", "o2"],
                        "page": 1, "per_page": 100}
    assert guard.calls[0].action == "organizations.search" and guard.calls[0].detail["seeds"] == 2
    for bad in ([], ["a", "b", "c", "d", "e", "f"], ["not/an id"]):
        with pytest.raises(ValueError):
            apollo.search_lookalike_organizations(bad, {})
    assert len(t.requests) == 1


def test_search_people_and_enrich():
    apollo, t, _ = make()
    apollo.search_people(
        {"person_titles[]": ["Head of People"], "person_locations": ["Illinois, US"], "organization_ids": ["o1"],
         "q_organization_domains_list": ["acme.example"]},
        per_page=25,
    )
    apollo.enrich_organization("WWW.Acme.example")
    people, enrich = t.requests
    assert people.url == f"{BASE}/mixed_people/api_search"
    assert people.json["person_titles"] == ["Head of People"] and people.json["per_page"] == 25
    assert people.json["organization_ids"] == ["o1"] and people.json["q_organization_domains_list"] == ["acme.example"]
    assert (enrich.method, enrich.url, enrich.params) == ("GET", f"{BASE}/organizations/enrich", {"domain": "acme.example"})


def test_bulk_enrich_is_one_paid_call_of_up_to_ten_root_domains():
    apollo, t, guard = make()
    t.route("POST", "/organizations/bulk_enrich", body={
        "organizations": [{"id": "o1", "primary_domain": "acme.example", "estimated_num_employees": 64}, None],
        "unique_enriched_records": 1, "missing_records": 1})
    body = apollo.bulk_enrich_organizations(["WWW.Acme.example", "beta.example", "acme.example"])
    [req] = t.requests
    assert (req.method, req.url) == ("POST", f"{BASE}/organizations/bulk_enrich")
    assert req.json == {"domains": ["acme.example", "beta.example"]} and req.idempotent is False  # never resent
    assert guard.calls[-1].action == "organizations.bulk_enrich" and guard.calls[-1].detail == {"count": 2}
    assert [(o["organization_id"], o["domain"]) for o in enriched_in(body)] == [("o1", "acme.example")]
    for bad in ([], [f"c{i}.example" for i in range(11)]):
        with pytest.raises(ValueError):
            apollo.bulk_enrich_organizations(bad)
    assert len(t.requests) == 1


def test_a_saved_account_s_own_id_is_never_taken_for_its_organization_id():
    """Defect 8 (9 Oct 2026): an `accounts` row with no organization_id carried its account id as `id`, which
    callers took as the organization id (apollo_org_id, then paid job-postings lookups)."""
    page = {"organizations": [{"id": "o1", "primary_domain": "acme.example"}],
            "accounts": [{"id": "acct-1", "organization_id": None, "domain": "beta.example"},
                         {"id": "acct-2", "organization_id": "o3", "domain": "gamma.example"}]}
    rows = organizations_in(page)
    assert [(o["organization_id"], o["id"], o.get("apollo_account_id")) for o in rows] == [
        ("o1", "o1", None), (None, None, "acct-1"), ("o3", "o3", "acct-2")]
    assert [org_id(o) for o in rows] == ["o1", "", "o3"]
    assert org_id(None) == ""


def test_iter_pages_keeps_each_caller_s_stop_rule():
    """Apollo.iter_pages (9 Oct 2026): one page loop. Where Apollo gives no total_entries, the visitor searches go on
    while pages are full (while_full) and the lookalike growth searches stop (while_counted), as they always did."""
    def reader(pages):
        asked = []

        def read(n):
            asked.append(n)
            return pages[n - 1] if n <= len(pages) else None
        return read, asked

    full = {"organizations": [{"id": f"o{i}"} for i in range(2)]}  # a full page at per_page=2
    counted = {**full, "pagination": {"total_entries": 3}}
    for more, want in ((while_full, [1, 2, 3]), (while_counted, [1])):
        read, asked = reader([full, full, {"organizations": []}])
        got = list(Apollo.iter_pages(read, more=more, per_page=2))
        assert asked == want and got[-1].last
    read, asked = reader([counted, counted, counted])
    assert [p.number for p in Apollo.iter_pages(read, more=while_counted, per_page=2)] == [1, 2]  # 3 of 3 by page 2
    read, asked = reader([full, full, full])
    pages = list(Apollo.iter_pages(read, per_page=2, max_pages=2))
    assert asked == [1, 2] and not pages[-1].last  # max_pages, not the end of the search
    read, asked = reader([full])
    assert len(list(Apollo.iter_pages(read, per_page=2))) == 1 and asked == [1, 2]  # read() gave None: it ends
    people = {"people": [{"id": "p1"}], "total_entries": 1}
    read, asked = reader([people, people])
    assert [p.rows for p in Apollo.iter_pages(read, more=while_full_page, rows=people_in, per_page=1, max_pages=2)] == [
        [{"id": "p1"}], [{"id": "p1"}]]  # People API Search: only a short page ends it


def test_enriched_in_reads_a_single_answer_and_an_empty_one():
    assert [o["organization_id"] for o in enriched_in({"organization": {"id": "o9", "primary_domain": "x.example"}})] == ["o9"]
    assert enriched_in({}) == enriched_in({"organization": {}}) == enriched_in({"organization": None}) == []


def test_bulk_match_chunks_of_ten_and_no_personal_emails():
    apollo, t, guard = make()
    t.route("POST", "/people/bulk_match", fn=lambda req: {
        "matches": [{"id": d["id"], "email": f"{d['id']}@acme.example", "email_status": "verified"} for d in req.json["details"]],
        "total_requested_enrichments": len(req.json["details"]),
        "unique_enriched_records": len(req.json["details"]),
        "missing_records": 0,
        "credits_consumed": 0.5 * len(req.json["details"]),
    })
    out = apollo.bulk_match([{"id": f"p{i}", "domain": "acme.example", "linkedin_url": None} for i in range(23)])
    assert [len(r.json["details"]) for r in t.requests] == [10, 10, 3]
    assert all(r.params == BULK_MATCH_FLAGS for r in t.requests)
    assert BULK_MATCH_FLAGS["reveal_personal_emails"] == "false" and BULK_MATCH_FLAGS["run_waterfall_email"] == "false"
    assert "linkedin_url" not in t.requests[0].json["details"][0]
    assert len(out["matches"]) == 23 and out["credits_consumed"] == 11.5 and out["total_requested_enrichments"] == 23
    assert [c.detail["count"] for c in guard.calls] == [10, 10, 3]
    with pytest.raises(ValueError):
        apollo.bulk_match([{"id": "p1"}], reveal_personal_emails=True)
    assert len(t.requests) == 3


def test_website_visitor_aggregates_per_company():
    apollo, t, _ = make()
    t.route("GET", "/website_visitors/domain_aggregates", fn=lambda req: (
        {"visits": 4, "top_paths": ["/us", "/us/pricing"]} if req.params["organization_id"] == "o1" else None
    ))
    out = apollo.website_visitor_aggregates("spill.chat", ["o1", "o1", "o2"], since="2026-09-01", until="2026-09-30")
    assert out == {"o1": {"visits": 4, "top_paths": ["/us", "/us/pricing"]}, "o2": {}}
    assert [r.params for r in t.requests] == [
        {"domain": "spill.chat", "organization_id": "o1", "from": "20260901", "to": "20260930"},
        {"domain": "spill.chat", "organization_id": "o2", "from": "20260901", "to": "20260930"},
    ]

    apollo, t, _ = make()
    t.route("GET", "/website_visitors/domain_aggregates", status=404, body={"error": "stats not found"})
    assert apollo.website_visitor_aggregates("spill.chat", ["o3"]) == {"o3": {}}


def test_website_visitor_search_body_is_read_only_and_never_resent():
    """Harry, 5 Oct 2026: the companies that visited spill.chat, from organization search's visitor filters."""
    apollo, t, guard = make()
    t.route("POST", "/mixed_companies/search", body={"organizations": [{"id": "o1", "primary_domain": "acme.example"}]})
    page = apollo.search_website_visitors([" Spill.chat "], days=30, pages=["/us/pricing", " ", "/us/demo"], page=2)
    [req] = t.requests
    assert (req.method, req.url) == ("POST", f"{BASE}/mixed_companies/search")
    assert req.json == {"website_visitors_from_domains": ["spill.chat"], "website_visitors_from_past": 30,
                        "website_visitors_domain_pages": ["/us/pricing", "/us/demo"], "page": 2, "per_page": 100}
    assert req.idempotent is False  # a paid read: never resent after a timeout
    assert guard.calls[-1].action == "website_visitors.search" and not guard.calls[-1].write
    assert [o["organization_id"] for o in organizations_in(page)] == ["o1"]
    apollo.search_website_visitors(["spill.chat"], days=1)
    assert "website_visitors_domain_pages" not in t.requests[-1].json  # no page filter: the whole site
    for bad in ({"days": 2}, {"days": 30, "page": 0}):
        with pytest.raises(ValueError):
            apollo.search_website_visitors(["spill.chat"], **bad)
    with pytest.raises(ValueError):
        apollo.search_website_visitors([""], days=30)
    assert len(t.requests) == 2


def test_credit_usage():
    apollo, t, _ = make()
    t.route("POST", "/usage_stats/credit_usage_stats", body={
        "credit_usage_stats": {"lead_credit": {"limit": 40000, "consumed": 9922, "left_over": 30078}},
        "current_credit_cycle": {"start_date": "2026-09-01T00:00:00Z", "end_date": "2026-10-01T00:00:00Z"},
    })
    usage = apollo.credit_usage()
    assert credits_left(usage) == 30078.0
    assert credits_left(usage, "direct_dial_credit") is None
    assert (t.requests[0].method, t.requests[0].url) == ("POST", f"{BASE}/usage_stats/credit_usage_stats")


def test_normalize_filters_drops_paging_keys():
    assert normalize_filters({"page": 3, "per_page": 10, "revenue_range[min]": 1}) == {"revenue_range": {"min": 1}}
