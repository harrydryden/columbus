"""base/say.py: the shape every Slack message takes (Harry, 12 Oct 2026)."""

from tests.fakes import make_context
from us_outbound.base import say
from us_outbound.ops.cli import JOBS
from us_outbound.ops.schedule import SCHEDULE
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all


def _ctx():
    settings, errors = validate_all(default_tabs())
    assert settings is not None, errors
    return make_context(settings)


def test_every_scheduled_job_has_a_plain_name():
    missing = sorted({j.name for j in SCHEDULE} - set(say.JOB_NAMES))
    assert missing == []
    assert set(say.JOB_NAMES) - set(JOBS) <= {"stop", "unenrol", "settings_bootstrap", "settings_load"}
    assert say.job_name("poll_replies") == "checking for replies"
    assert say.job_name("some_new_job") == "some new job"


def test_compose_puts_the_action_on_its_own_line():
    text = say.compose(say.ACTION, "Something needs a look", ["One fact.", ""], do="Fix it here.", why="it matters.")
    assert text.splitlines() == [":warning: *Something needs a look*", "One fact.", "*What to do:* Fix it here.",
                                 "_Why it matters: it matters._"]
    steps = say.compose(say.INFO, "Steps", do=["First.", "Second."]).splitlines()
    assert steps == [":bar_chart: *Steps*", "*What to do:*", "1. First.", "2. Second."]
    assert say.compose(say.INFO, "Only facts", ["a", "b"]).splitlines() == [":bar_chart: *Only facts*", "a", "b"]


def test_links_open_the_tab_or_fall_back_to_plain_words(monkeypatch):
    ctx = _ctx()
    ctx.clients.sheets.tab_ids = lambda sheet_id: {"General": 42}
    assert say.sheet_link(ctx, "General") == "<https://docs.google.com/spreadsheets/d/sheet-test/edit#gid=42|General tab>"

    def broken(sheet_id):
        raise RuntimeError("no network")

    ctx.clients.sheets.tab_ids = broken
    assert say.sheet_link(ctx, "Copy", "Copy tab") == "<https://docs.google.com/spreadsheets/d/sheet-test/edit|Copy tab>"
    for k in ("PROJECT", "SERVICE", "ENVIRONMENT"):
        monkeypatch.delenv(f"RAILWAY_{k}_ID", raising=False)
    assert say.logs_link() == "the worker's logs in Railway"
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "s")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "e")
    assert say.logs_link() == "<https://railway.com/project/p/service/s?environmentId=e|the worker's logs in Railway>"
    assert say.command("sync") == "`railway ssh -- us-outbound sync`"
