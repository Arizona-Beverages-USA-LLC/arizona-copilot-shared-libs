"""Agent step telemetry on Google's standard (the ADK BigQuery Agent Analytics plugin) for the Tea Talk agents outside
the SAP KG runtime. Same table, settings and Tea Talk link as sap-kg-semantic's core/telemetry/agent_analytics.py
(decision 91), so one set of views covers all 22 agents.

    from shared_libs.telemetry import agent_analytics as aa
    App(root_agent=..., name=..., plugins=aa.plugins("vip_distributor"))      # where the App is built
    state_delta = aa.state_delta(context.message.metadata)                     # in the executor, per turn
    aa.drop_check("vip_distributor")                                          # after each turn: lost rows -> console
    aa.warm_up()                                                              # once per process, at start

Every model request and response, tool call (arguments, SQL, result), error, token count and step time goes to
`gen-lang-client-0552847374.adk_agent_analytics.agent_events` (US, beside Tea Talk's tt_telemetry_*). Tea Talk sends
its ids as A2A message metadata (tt_turn_id, tt_conversation_id, tt_session_id, tt_user, tt_env); state_delta puts them
in the session state for the turn, which the plugin logs on every event. Owner's decisions (2026-11-27): full content
(up to 1 MB per text block), 270-day partitions, admins and the Investigator read it.

Off unless AGENT_ANALYTICS=1 (an engine environment variable): rollback is the setting.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)

PROJECT = os.environ.get("ADK_ANALYTICS_PROJECT", "gen-lang-client-0552847374")
DATASET = os.environ.get("ADK_ANALYTICS_DATASET", "adk_agent_analytics")
TABLE = os.environ.get("ADK_ANALYTICS_TABLE", "agent_events")
LOCATION = os.environ.get("ADK_ANALYTICS_LOCATION", "US")
MAX_CONTENT = 1024 * 1024      # per text block; 100 KB cut 46% of model requests (2026-11-27)
LINK_KEYS = ("tt_turn_id", "tt_conversation_id", "tt_session_id", "tt_user", "tt_env")
_PLUGINS: list = []
_DROPS_SEEN: dict[str, int] = {}


def enabled() -> bool:
    return os.environ.get("AGENT_ANALYTICS", "").strip().lower() in ("1", "true", "on", "yes")


def plugins(engine: str) -> list:
    """[BigQueryAgentAnalyticsPlugin] when enabled, else []. Never raises: telemetry never stops an agent building."""
    if not enabled():
        return []
    try:
        from google.adk.plugins.bigquery_agent_analytics_plugin import (BigQueryAgentAnalyticsPlugin,
                                                                        BigQueryLoggerConfig)
        kw: dict[str, Any] = dict(table_id=TABLE, max_content_length=MAX_CONTENT, batch_size=1,
                                  batch_flush_interval=0.5, create_views=False, log_session_metadata=True,
                                  custom_tags=_tags(engine))
        fields = getattr(BigQueryLoggerConfig, "__dataclass_fields__", {})
        if "create_eval_views" in fields:          # ADK 2.13+
            kw["create_eval_views"] = False
        if "flush_on_run_end" in fields:           # rows via the background writer, not a flush on the response path
            kw["flush_on_run_end"] = False
        # the views exist (created once centrally); creating them in every engine process added ~40 s to its first turn
        cfg = BigQueryLoggerConfig(**{k: v for k, v in kw.items() if not fields or k in fields})
        plugin = BigQueryAgentAnalyticsPlugin(project_id=PROJECT, dataset_id=DATASET, table_id=TABLE, config=cfg,
                                              location=LOCATION)
        _PLUGINS.append(plugin)
        return [plugin]
    except Exception:  # noqa: BLE001
        log.warning("agent analytics plugin not attached for %s", engine, exc_info=True)
        return []


def link_from_metadata(meta: Optional[dict]) -> dict[str, str]:
    """The Tea Talk ids an A2A message carries (only the known keys, as short strings)."""
    meta = meta if isinstance(meta, dict) else {}
    return {k: str(meta[k])[:200] for k in LINK_KEYS if meta.get(k) not in (None, "")}


def state_delta(meta: Optional[dict]) -> Optional[dict]:
    """{"tt": {...}} for runner.run_async(state_delta=...) when Tea Talk sent its ids, else None."""
    link = link_from_metadata(meta)
    return {"tt": link} if link else None


def warm_up(timeout_s: Optional[float] = None) -> float:
    """Run the plugins' one-time setup now, at process start, instead of inside the process's first turn. Returns the
    seconds it took (-1.0 when it did not finish). Never raises.

    Two one-time costs otherwise land in a worker process's first turn: the setup (BigQuery client, table check, the
    Storage Write client on the plugin's own background loop), and the modules the plugin's before_tool_callback
    imports lazily to classify a tool (_get_tool_origin: google.adk.tools.mcp_tool, which loads the `mcp` package when
    it is installed, and google.adk.agents.remote_a2a_agent; ~1.3 s of CPU on a laptop). Agent Engine throttles CPU
    while no request is open, which a non-blocking A2A turn is (the send returns at once, the turn runs behind the
    polls), so on the Sales copilot the imports held the first tool call of each process 6-9 s (measured 2026-11-28;
    plugin off: 0 s). At start the container has CPU.

    Runs in its own thread and event loop: with flush_on_run_end=False the plugin keeps its writer on a process-wide
    background loop, so nothing it builds is tied to this loop. Joined up to AGENT_ANALYTICS_WARM_TIMEOUT_S (30 s);
    past that, setup goes on in the background and the first turn joins it (the plugin coalesces setup)."""
    if not _PLUGINS:
        return 0.0
    if timeout_s is None:
        try:
            timeout_s = float(os.environ.get("AGENT_ANALYTICS_WARM_TIMEOUT_S", "30"))
        except ValueError:
            timeout_s = 30.0
    out: dict = {}
    t0 = time.monotonic()

    def run() -> None:
        for mod in ("google.adk.tools.mcp_tool.mcp_tool", "google.adk.agents.remote_a2a_agent"):
            try:                                                   # what _get_tool_origin imports on a first tool call
                importlib.import_module(mod)
            except Exception:  # noqa: BLE001 -- not installed: the plugin skips it the same way
                pass
        try:
            import asyncio

            async def go() -> None:
                for p in list(_PLUGINS):
                    start = getattr(p, "_ensure_started", None)       # ADK's own setup entry (before_run_callback)
                    if start is not None:
                        out[id(p)] = await start()
            asyncio.run(go())
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    th = threading.Thread(target=run, name="agent-analytics-warm-up", daemon=True)
    th.start()
    th.join(max(0.0, timeout_s))
    took = time.monotonic() - t0 if not th.is_alive() else -1.0
    try:
        results = [v for k, v in out.items() if k != "error"]
        print(f"[AGENT_ANALYTICS] warm-up pid={os.getpid()} "
              + (f"done in {took:.1f}s" if took >= 0 else f"still running after {timeout_s:.0f}s (goes on)")
              + f" setup={results}" + (f" error={out['error']}" if "error" in out else ""), flush=True)
    except Exception:  # noqa: BLE001
        pass
    return took


def _tags(engine: str) -> dict:
    return {"engine": engine, "runtime": "copilot", "env": os.environ.get("AGENT_ANALYTICS_ENV", "shared")}


def drop_check(engine: str = "") -> dict[str, int]:
    """New dropped-row counts since the last check, across this process's plugins. Any new drop is logged (marker
    AGENT_ANALYTICS_DROPS) and written as one TELEMETRY_DROPS row, which Tea Talk's Telemetry console shows under
    Coverage (decision 98: no email). Never raises."""
    new: dict[str, int] = {}
    try:
        total: dict[str, int] = {}
        for p in _PLUGINS:
            stats = getattr(p, "get_drop_stats", None)
            for reason, n in ((stats() if stats else None) or {}).items():
                total[reason] = total.get(reason, 0) + int(n or 0)
        for reason, n in total.items():
            if n > _DROPS_SEEN.get(reason, 0):
                new[reason] = n - _DROPS_SEEN.get(reason, 0)
                _DROPS_SEEN[reason] = n
        if new:
            log.warning("AGENT_ANALYTICS_DROPS %s", json.dumps(new))
            record_drops(engine, new)
    except Exception:  # noqa: BLE001
        log.debug("drop check failed", exc_info=True)
    return new


def drop_row(engine: str, drops: dict[str, int], now: Optional[dt.datetime] = None) -> dict:
    """The TELEMETRY_DROPS row for agent_events (the plugin's schema). Pure."""
    t = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    return {"timestamp": t, "event_id": uuid.uuid4().hex, "event_type": "TELEMETRY_DROPS", "agent": engine or None,
            "session_id": None, "invocation_id": None, "user_id": None, "trace_id": None, "span_id": None,
            "parent_span_id": None,
            "content": json.dumps({"drops": [{"reason": k, "rows": int(v)} for k, v in sorted(drops.items())]}),
            "attributes": json.dumps({"custom_tags": _tags(engine or "unknown")}), "latency_ms": None,
            "status": "ERROR", "error_message": "telemetry rows dropped: " + ", ".join(f"{k} {v}" for k, v in drops.items()),
            "is_truncated": False}


def record_drops(engine: str, drops: dict[str, int]) -> None:
    """Write the drop counts through the streaming insert API (not the plugin's write stream, so a failure there is still
    recorded). Background thread, never raises."""
    def run() -> None:
        try:
            from google.cloud import bigquery
            bigquery.Client(project=PROJECT).insert_rows_json(f"{PROJECT}.{DATASET}.{TABLE}", [drop_row(engine, drops)])
        except Exception:  # noqa: BLE001
            log.debug("drop row not written", exc_info=True)
    threading.Thread(target=run, name="agent-analytics-drops", daemon=True).start()
