"""Probe: title->role mapping with real defaults; scoring arithmetic for a carrier-EAP account; Hiring and growth source wiring."""
from datetime import date
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all
from us_outbound.clean.people import map_title_to_role
from us_outbound.scoring.score import score_account

tabs = default_tabs()
res = validate_all(tabs)
settings = res.settings if hasattr(res, 'settings') else res[0]
roles = settings.roles
titles = ["Head of HR","VP Human Resources","Chief Human Resources Officer","Director of People","Director of People Operations",
          "People Operations Manager","HR Generalist","HR Business Partner","Owner","Principal","Partner","Founding Partner",
          "General Manager","Head of People","VP of People","HR Manager","Senior HR Manager","Office Manager","COO","Co-Founder and CTO",
          "Founder & CEO","Managing Partner","Chief People Officer","Head of Talent","People & Culture Lead","Benefits Manager"]
print("TITLE -> ROLE")
for t in titles:
    print(f"  {t!r:40} -> {map_title_to_role(t, roles)}")

def ev(fact, value, src, quote="", days_ago=10):
    from datetime import datetime, timedelta, UTC
    return {"event_id": fact+src, "account_id": "a1", "source": src, "fact": fact, "value": value,
            "quote": quote, "source_url": "", "observed_at": datetime.now(UTC) - timedelta(days=days_ago)}

acct = {"account_id": "a1", "domain": "x.com", "hq_state": "NY", "industry": "Technology & Startups", "industry_group": "Technology & Startups", "size_band": "50-99", "employees": 60}
today = date(2026, 10, 15)
# Carrier EAP alone, via Clay mental_health_provision type carrier_eap
events = [ev("mental_health_provision", {"type": "carrier_eap", "provider": "ComPsych", "quote": "Employee assistance program through ComPsych"}, "clay_careers", quote="Employee assistance program through ComPsych")]
r = score_account(acct, events, settings, today)
print("\nCarrier EAP only, Oct:", r.score, r.tier, "|", r.angle, "| matches:", [(m.signal.signal, m.weight_applied) for m in r.matches])
events2 = events + [ev("values_page", True, "clay_careers")]
r = score_account(acct, events2, settings, today)
print("Carrier EAP + values page, Oct:", r.score, r.tier)
# Hiring and growth from apollo_jobs
r = score_account(acct, [ev("open_roles", 5, "apollo_jobs")], settings, today)
print("open_roles=5 from apollo_jobs:", r.score, [(m.signal.signal, m.weight_applied) for m in r.matches])
r = score_account(acct, [ev("open_roles", 5, "apollo_org")], settings, today)
print("open_roles=5 from apollo_org:", r.score, [(m.signal.signal, m.weight_applied) for m in r.matches])
# Site visit + pricing
r = score_account(acct, [ev("us_visits_30d", 2, "site_visits"), ev("pricing_or_demo_visits_30d", 1, "site_visits")], settings, today)
print("site visit + pricing, Oct:", r.score, r.tier, "| angle:", r.angle, "| opener:", repr(r.opener))
# Funding 500 days ago
r = score_account(acct, [ev("days_since_funding", 500, "apollo_org")], settings, today)
print("funding 500d ago:", r.score, [(m.signal.signal, m.weight_applied) for m in r.matches])
# Nothing at all in Oct
r = score_account(acct, [], settings, today)
print("no facts, Oct:", r.score, r.tier)
r = score_account(acct, [], settings, date(2027,1,15))
print("no facts, Jan:", r.score, r.tier)
