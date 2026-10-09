"""notify.once_key (9 Oct 2026): every "posted once a day, week or month" key built one way, its period last."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.fakes import make_context
from tests.test_labels import DEFAULT
from us_outbound.ops import notify


def test_the_period_is_the_uk_day_week_or_month_and_always_last():
    ctx = make_context(DEFAULT, now=datetime(2026, 10, 26, 23, 30, tzinfo=UTC))  # Mon 26 Oct, 23:30 UK (GMT)
    assert notify.once_key(ctx, "apollo_floor") == "apollo_floor:2026-10-26"
    assert notify.once_key(ctx, "mailboxes", per="week") == "mailboxes:2026-W44"
    assert notify.once_key(ctx, "claude_cap_80", per="month", on=date(2026, 11, 2)) == "claude_cap_80:2026-11"
    key = notify.once_key(ctx, "job_error", "enrol", "errors", on=date(2026, 10, 25))
    assert key == "job_error:enrol:errors:2026-10-25" and notify.kind_of(key) == "job_error:enrol:errors"
    with pytest.raises(ValueError):
        notify.once_key(ctx, "x", per="year")
