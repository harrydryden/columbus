"""BigQueryStore's SQL: parameters BigQuery can assign to any column type."""

from us_outbound.clients.bq import BigQueryStore
from us_outbound.clients.guard import Guard


class CapturingClient:
    def __init__(self):
        self.queries: list[tuple[str, list]] = []

    def query(self, sql, job_config=None, **_):
        self.queries.append((sql, list(job_config.query_parameters) if job_config else []))
        return self

    def result(self):
        return []


def store() -> tuple[BigQueryStore, CapturingClient]:
    client = CapturingClient()
    return BigQueryStore(Guard(), "test-project", "EU", client=client), client


def test_setting_a_column_to_null_sends_an_untyped_null():
    s, client = store()
    s.update("contacts", {"contact_id": ["c1"]}, {"suppressed": None, "suppressed_reason": "unsubscribe"})
    [(sql, params)] = client.queries
    assert "SET suppressed = NULL, suppressed_reason = @s1 WHERE contact_id IN UNNEST(@w0)" in sql
    assert sorted(p.name for p in params) == ["s1", "w0"]


def test_an_empty_in_list_matches_nothing():
    s, client = store()
    assert s.select("events", {"step": []}) == []
    [(sql, params)] = client.queries
    assert sql.endswith("WHERE FALSE") and params == []
