"""`us-outbound data show` runs every aggregate query read-only and keeps going past a bad one."""

from us_outbound.ops import data_health


class _Store:
    def __init__(self):
        self.seen = []

    def query(self, sql, params=None):
        self.seen.append(sql)
        if "funding_stage" in sql and "GROUP BY" in sql:
            raise RuntimeError("boom")
        return [{"n": 1}]


class _Ctx:
    def __init__(self):
        self.store = _Store()


def test_every_query_is_a_select_and_one_failure_does_not_hide_the_rest():
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in data_health.QUERIES.values())
    ctx = _Ctx()
    lines = data_health.report(ctx)
    assert len(lines) == len(data_health.QUERIES) == len(ctx.store.seen)
    assert any(line.startswith("funding_stage:") and "RuntimeError" in line for line in lines)
    assert any(line.startswith("tiers:") and '"n": 1' in line for line in lines)
