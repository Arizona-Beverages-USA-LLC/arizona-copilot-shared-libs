"""Agent step telemetry on Google's standard (the ADK BigQuery Agent Analytics plugin) for the Tea Talk agents outside
the SAP KG runtime. Same table, settings and Tea Talk link as sap-kg-semantic's core/telemetry/agent_analytics.py
(decision 91), so one set of views covers all 22 agents.

    from shared_libs.telemetry import agent_analytics as aa
    App(root_agent=..., name=..., plugins=aa.plugins("vip_distributor"))      # where the App is built
    state_delta = aa.state_delta(context.message.metadata)                     # in the executor, per turn

Every model request and response, tool call (arguments, SQL, result), error, token count and step time goes to
`gen-lang-client-0552847374.adk_agent_analytics.agent_events` (US, beside Tea Talk's tt_telemetry_*). Tea Talk sends
its ids as A2A message metadata (tt_turn_id, tt_conversation_id, tt_session_id, tt_user, tt_env); state_delta puts them
in the session state for the turn, which the plugin logs on every event. Owner's decisions (2026-11-27): full content
(100 KB per event), 90-day partitions, admins and the Investigator read it.

Off unless AGENT_ANALYTICS=1 (an engine environment variable): rollback is the setting.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

PROJECT = os.environ.get("ADK_ANALYTICS_PROJECT", "gen-lang-client-0552847374")
DATASET = os.environ.get("ADK_ANALYTICS_DATASET", "adk_agent_analytics")
TABLE = os.environ.get("ADK_ANALYTICS_TABLE", "agent_events")
LOCATION = os.environ.get("ADK_ANALYTICS_LOCATION", "US")
MAX_CONTENT = 100 * 1024
LINK_KEYS = ("tt_turn_id", "tt_conversation_id", "tt_session_id", "tt_user", "tt_env")


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
                                  custom_tags={"engine": engine, "runtime": "copilot",
                                               "env": os.environ.get("AGENT_ANALYTICS_ENV", "shared")})
        fields = getattr(BigQueryLoggerConfig, "__dataclass_fields__", {})
        if "create_eval_views" in fields:          # ADK 2.13+
            kw["create_eval_views"] = False
        if "flush_on_run_end" in fields:           # rows via the background writer, not a flush on the response path
            kw["flush_on_run_end"] = False
        # the views exist (created once centrally); creating them in every engine process added ~40 s to its first turn
        cfg = BigQueryLoggerConfig(**{k: v for k, v in kw.items() if not fields or k in fields})
        return [BigQueryAgentAnalyticsPlugin(project_id=PROJECT, dataset_id=DATASET, table_id=TABLE, config=cfg,
                                             location=LOCATION)]
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
