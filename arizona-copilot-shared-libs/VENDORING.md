# Vendoring `shared_libs/` into an agent repo

This repo is the single source of truth. Each agent repo carries a **vendored
copy** of the whole tree at `<agent_repo>/shared_libs/`. Keep them identical —
change here, then re-sync.

Vendor the **whole `shared_libs/` tree** (all three packages). `a2ui` imports
`shared_libs.data_export`, so a2ui without data_export is broken. (An agent that
only wants the export tools, no charts, may vendor just `shared_libs/data_export/`;
an agent that only wants instrumentation may vendor just `shared_libs/telemetry/`
— but never a2ui alone.)

## First-time install into an agent

1. Copy the whole `shared_libs/` tree into the agent repo root:

   ```
   <agent_repo>/shared_libs/
   ├── __init__.py
   ├── a2ui/
   ├── data_export/
   └── telemetry/
   ```

2. Add the runtime deps (see `requirements.txt`): put the pip deps in the agent's
   `requirements.txt` **and** the inline `REQUIREMENTS` in `deploy.py` /
   `deploy_a2a.py`, and ensure `"./shared_libs"` is in `extra_packages`.

3. Wire the two a2ui callbacks on the agent (see README "Wiring an agent"). If the
   agent offers file exports, wire `create_export_toolset()` from `data_export`.
   For telemetry, call `configure_model_tiers(TRIAGE_MODEL, FAST_MODEL, DEEP_MODEL)`
   once at import, register `configure_bq_ledger(...)` if the agent queries
   BigQuery, and chain the four telemetry callbacks (see README "Wiring an agent
   (telemetry)").

4. Add `A2UI_GUIDELINES_CORE.md` to the agent's skill/prompt; swap in the agent's
   own domain chart examples (keep VIP's proven, catalog-agnostic template).

## Keeping copies in sync (choose one, use the SAME one everywhere)

**Option A — git subtree (recommended; keeps history, one command to pull):**

```bash
# one-time, from the agent repo root:
git subtree add  --prefix shared_libs  <this-repo-url> main --squash
# later, to pull updates:
git subtree pull --prefix shared_libs  <this-repo-url> main --squash
```

**Option B — plain copy (simplest):**

```bash
# from the agent repo root, with this repo checked out next to it:
rm -rf shared_libs
cp -r ../arizona-copilot-shared-libs/shared_libs shared_libs
```

A short CI check that diffs each agent's vendored `shared_libs/` against this
repo's `shared_libs/` catches drift early. Run it on every agent.

## Do NOT

- Edit `shared_libs/**` inside an agent repo. Change it here and re-sync.
- Vendor `a2ui` without `data_export` — a2ui's download/render path imports
  `shared_libs.data_export.{png,gcs_upload,_artifact}` and will fail at runtime.
- Change the state-key names (`arizona_current_chart_id`, `arizona.chart_cache`)
  in one place only — they must match between `chart_id_injector` and
  `a2ui_bridge`, which is guaranteed as long as the whole `a2ui/` package is
  vendored together.
- Rename the catalog (`arizona_copilot_combined`) in one agent only — the catalog
  is shared; rename here and re-sync all agents, or agents diverge.
