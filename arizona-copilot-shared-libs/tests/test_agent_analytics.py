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
