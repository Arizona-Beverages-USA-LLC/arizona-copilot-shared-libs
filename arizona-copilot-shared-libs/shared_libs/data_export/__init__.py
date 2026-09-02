"""
data_export — Export tools for Sales & Merchandising Copilot (and future agents).

Provides a set of ADK tools the agent composes to build downloadable files in
six formats: PNG, XLSX, HTML, PDF, DOCX, PPTX. Each tool renders the file in
memory and hands the bytes to ADK via tool_context.save_artifact(). ADK then
surfaces the file as a native download attachment in Gemini Enterprise — no
URL, no A2UI component, no HTML needed on the agent side.

Public surface:
    from shared_libs.data_export import create_export_toolset
    tools = create_export_toolset()   # list of async functions (ADK auto-wraps)
    root_agent = Agent(tools=[*tableau_tools, *tools])

Architecture overview:
    _types.py       — shared dataclasses (ExportResult, MIME_TYPES)
    _sessions.py    — in-memory session registry for multi-step builders
    _artifact.py    — save_as_adk_artifact() helper + filename sanitization
    _gcs.py         — (deprecated, kept for history) GCS signed-URL path
    png/            — one-shot chart rendering via vl-convert-python
    xlsx/           — openpyxl-based builder (future)
    html/           — string-assembly builder (future)
    pdf/            — reportlab-based builder (future)
    docx/           — python-docx-based builder (future)
    pptx/           — python-pptx-based builder (future)
    tools.py        — ADK tool functions + create_export_toolset()

Design principles:
    1. Native ADK artifacts for file delivery. Bytes never pass through the
       model's function_response — they go directly to the UI via ADK's
       artifact mechanism, which Gemini Enterprise renders as a download card.
    2. Builder pattern (future phases). Each document format has new_* /
       add_* / finalize_* primitives. The agent composes layouts via multiple
       tool calls, then the finalize_* call is what creates the artifact.
    3. Sessions are in-memory, per-agent-process. No persistence.
    4. Charts in PDF/DOCX/PPTX are PNG images underneath. One rendering
       pipeline (vl-convert-python), six formats consume it.
    5. Every export tool's FIRST parameter is `tool_context: ToolContext`.
       ADK injects this automatically and hides it from the model's view of
       the tool signature.
"""

from .tools import create_export_toolset

__all__ = ["create_export_toolset"]
