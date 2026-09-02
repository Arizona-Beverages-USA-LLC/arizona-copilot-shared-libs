"""
Artifact helpers — save file bytes via ADK's native artifact mechanism.

Flow:
    1. Format module renders the file to bytes in memory (e.g. render PNG via vl-convert)
    2. save_as_adk_artifact() wraps bytes in a google.genai.types.Part and calls
       tool_context.save_artifact(filename, part)
    3. ADK automatically includes the artifact in the response event stream
    4. Agent Engine surfaces it as a downloadable file attachment in GE chat

The agent never sees the bytes. The tool returns a small metadata dict so the
model can acknowledge what happened without ever processing the bytes.

Why this replaces the GCS pipeline:
    The ADK artifact mechanism is the first-class, supported way to deliver
    files from a tool to the UI. Agent Engine auto-provisions an
    InMemoryArtifactService when one isn't explicitly configured (see
    vertexai/agent_engines/templates/adk.py set_up()), so no additional
    setup is needed. Gemini Enterprise's chat UI has native rendering for
    these artifacts (the download card with filename + file-type icon).

    Previously we uploaded to GCS and returned a signed URL. That worked
    mechanically but required ~200 lines of signing code, four IAM grants,
    and the URL had to be surfaced via A2UI which didn't have a clickable
    Link component. The ADK artifact path has none of those problems.
"""

from __future__ import annotations

import logging

from google.genai import types

from ._types import ExportResult, MIME_TYPES


logger = logging.getLogger(__name__)


async def save_as_adk_artifact(
    tool_context,
    raw_bytes: bytes,
    filename: str,
    format: str,
) -> ExportResult:
    """Save bytes as an ADK artifact and return an ExportResult.

    Args:
        tool_context: The ADK ToolContext (passed automatically by ADK when
            the tool function declares it as a parameter). Must have
            access to an artifact_service.
        raw_bytes: The file contents as bytes (e.g. PNG bytes).
        filename: Sanitized filename with extension (e.g. "walmart_360.pdf").
            Must already be sanitized via safe_filename() before reaching here.
        format: Short format label ("png", "xlsx", "html", "pdf", "docx", "pptx").
            Used to look up the canonical MIME type.

    Returns:
        ExportResult with artifact metadata. ADK has already attached the
        artifact to the response event stream; this return value is just
        for the model's consumption so it can tell the user what was saved.

    Raises:
        KeyError: if format is not one of the known types.
        ValueError: if filename lacks the correct extension, or if
            tool_context doesn't have an artifact service.
    """
    if format not in MIME_TYPES:
        raise KeyError(
            f"Unknown export format: {format!r}. "
            f"Must be one of: {sorted(MIME_TYPES)}"
        )

    expected_ext = f".{format}"
    if not filename.lower().endswith(expected_ext):
        raise ValueError(
            f"Filename {filename!r} doesn't end with expected extension "
            f"{expected_ext!r} for format {format!r}."
        )

    mime_type = MIME_TYPES[format]
    size_kb = max(1, len(raw_bytes) // 1024)

    # Wrap the bytes in a Part. display_name makes the UI show this filename
    # (some artifact renderers use that instead of the artifact key).
    artifact_part = types.Part(
        inline_data=types.Blob(
            data=raw_bytes,
            mime_type=mime_type,
            display_name=filename,
        )
    )

    logger.info(
        "Saving artifact: format=%s bytes=%d mime=%s filename=%s",
        format, len(raw_bytes), mime_type, filename
    )
    version = await tool_context.save_artifact(
        filename=filename,
        artifact=artifact_part,
    )
    logger.info("Artifact saved: filename=%s version=%d", filename, version)

    return ExportResult(
        filename=filename,
        mime_type=mime_type,
        size_kb=size_kb,
        format=format,
        artifact_version=version,
    )


def safe_filename(suggested: str, fallback_stem: str, format: str) -> str:
    """Produce a filesystem-safe filename with the correct extension.

    Accepts a user-suggested filename (possibly with illegal characters) and
    a fallback stem to use if the suggestion is empty/unusable. Ensures the
    returned filename ends with the correct extension for the given format.
    """
    illegal = set('<>:"/\\|?*\x00')
    cleaned = "".join(c for c in (suggested or "").strip() if c not in illegal)
    cleaned = "_".join(cleaned.split())

    if not cleaned:
        cleaned = fallback_stem

    ext = f".{format}"
    if not cleaned.lower().endswith(ext):
        if "." in cleaned:
            cleaned = cleaned.rsplit(".", 1)[0]
        cleaned = cleaned + ext

    return cleaned
