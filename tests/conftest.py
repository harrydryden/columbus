from __future__ import annotations

import dataclasses

import pytest

from tests.fakes import FakeTransport, make_context

# Ids that phase 0 looks up in the real systems; fixed values for tests.
TEST_IDS = {
    "hubspot_pipeline_id": "pipe-spill3",
    "hubspot_deal_stage_id": "stage-first",
    "hubspot_owner_id": "owner-harry",
    "clay_accounts_function_id": "fn-us-accounts",
    "clay_contacts_function_id": "fn-us-contacts",
    "approver_slack_ids": ("U_HARRY",),
    "clay_monthly_credits": 2000.0,
    "clay_credits_per_account": 5.0,
}


@pytest.fixture
def default_settings():
    """The SPEC 5 default sheet, validated, with phase-0 ids filled in and mailboxes Active."""
    from us_outbound.settings.defaults import default_tabs
    from us_outbound.settings.validate import validate_all

    settings, errors = validate_all(default_tabs())
    assert not any(errors.values()), errors
    general = dataclasses.replace(settings.general, **TEST_IDS)
    mailboxes = tuple(
        dataclasses.replace(m, status="Active", instantly_account_id=m.address) for m in settings.mailboxes
    )
    return dataclasses.replace(settings, general=general, mailboxes=mailboxes)


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def ctx(default_settings, transport):
    return make_context(default_settings, transport=transport)


@pytest.fixture(autouse=True)
def no_claude_network(monkeypatch):
    """No test reaches the Claude API (Harry, 7 Oct 2026: verify_accounts asks the label check in every mode). A
    client built without a fake SDK (make_context(..., claude_sdk=sdk) gives one) gets an anthropic.Anthropic that
    cannot connect, so the code under test takes its "Claude did not answer" path at once."""
    import anthropic

    class Offline:
        def __init__(self, *args, **kwargs):
            self.messages = self

        def with_options(self, **kwargs):
            return self

        def create(self, **kwargs):
            exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
            Exception.__init__(exc, "no network in tests")
            raise exc

    monkeypatch.setattr(anthropic, "Anthropic", Offline)
