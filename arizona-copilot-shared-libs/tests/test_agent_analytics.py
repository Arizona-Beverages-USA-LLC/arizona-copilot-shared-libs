"""shared_libs.telemetry.agent_analytics: Google's BigQuery Agent Analytics plugin for the Tea Talk agents outside the
SAP KG runtime, and the Tea Talk link (tt_* A2A metadata into the turn's session state)."""
from shared_libs.telemetry import agent_analytics as AA


def test_off_unless_the_setting_says_so(monkeypatch):
    monkeypatch.delenv("AGENT_ANALYTICS", raising=False)
    assert not AA.enabled() and AA.plugins("x") == []
    monkeypatch.setenv("AGENT_ANALYTICS", "1")
    assert AA.enabled()


def test_only_tea_talks_link_keys_go_into_the_turn_state():
    meta = {"tt_turn_id": "t-1", "tt_user": "someone@example.com", "user_id": "u", "tt_env": "", "x": 1}
    assert AA.link_from_metadata(meta) == {"tt_turn_id": "t-1", "tt_user": "someone@example.com"}
    assert AA.state_delta(meta) == {"tt": {"tt_turn_id": "t-1", "tt_user": "someone@example.com"}}
    assert AA.state_delta(None) is None and AA.state_delta({"user_id": "u"}) is None


def test_lost_rows_are_reported_once_each_as_a_console_row(monkeypatch):
    import json

    class _Plugin:
        stats = {}

        def get_drop_stats(self):
            return dict(self.stats)

    written = []
    monkeypatch.setattr(AA, "_PLUGINS", [_Plugin()])
    monkeypatch.setattr(AA, "_DROPS_SEEN", {})
    monkeypatch.setattr(AA, "record_drops", lambda engine, drops: written.append((engine, drops)))
    assert AA.drop_check("iri_copilot") == {} and written == []          # nothing lost, nothing written
    _Plugin.stats = {"queue_full": 2}
    assert AA.drop_check("iri_copilot") == {"queue_full": 2}
    assert AA.drop_check("iri_copilot") == {}                              # the same 2 are not reported again
    _Plugin.stats = {"queue_full": 5}
    assert AA.drop_check("iri_copilot") == {"queue_full": 3}
    assert written == [("iri_copilot", {"queue_full": 2}), ("iri_copilot", {"queue_full": 3})]
    row = AA.drop_row("iri_copilot", {"queue_full": 3})
    assert row["event_type"] == "TELEMETRY_DROPS" and row["status"] == "ERROR" and row["agent"] == "iri_copilot"
    assert json.loads(row["content"]) == {"drops": [{"reason": "queue_full", "rows": 3}]}


def test_warm_up_runs_setup_at_start_and_never_holds_or_fails_the_boot(monkeypatch):
    import asyncio

    calls = []

    class _Ok:
        async def _ensure_started(self):
            calls.append(asyncio.get_running_loop())
            return "ok"

    class _Boom:
        async def _ensure_started(self):
            raise RuntimeError("no table")

    class _Slow:
        async def _ensure_started(self):
            await asyncio.sleep(5)

    monkeypatch.setattr(AA, "_PLUGINS", [])
    assert AA.warm_up() == 0.0                                             # no plugin, nothing to do
    monkeypatch.setattr(AA, "_PLUGINS", [_Ok()])
    assert AA.warm_up() >= 0 and len(calls) == 1                           # setup ran, in its own loop
    monkeypatch.setattr(AA, "_PLUGINS", [_Boom()])
    assert AA.warm_up() >= 0                                               # a failing setup does not raise
    monkeypatch.setattr(AA, "_PLUGINS", [_Slow()])
    assert AA.warm_up(timeout_s=0.2) == -1.0                               # nor holds the boot past the timeout
    monkeypatch.setattr(AA, "_PLUGINS", [object()])
    assert AA.warm_up() >= 0                                               # no setup hook: skipped
