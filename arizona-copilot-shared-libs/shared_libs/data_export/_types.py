"""
Shared types for the data_export package.

Keep this file dependency-free (stdlib only) so any format module can import
from it without circular dependencies.

Version 3 (2026-04-19): ExportResult now carries `artifact_version` (int).
The file bytes are delivered to the UI via ADK's native artifact mechanism
(tool_context.save_artifact), NOT via a URL. ADK automatically attaches
the artifact to the response event stream; Gemini Enterprise renders it
as a native download card. No URLs, no GCS, no A2UI Link component.

History:
    v1: ExportResult.base64_bytes — broke Vertex generate_content with
        INVALID_ARGUMENT on large payloads (~500KB base64 blobs).
    v2: ExportResult.download_url — GCS signed URL path. Worked technically
        but delivered URLs have no native render component in A2UI v0.8,
        and GE regular chat has a richer native download attachment anyway.
    v3 (this): ExportResult.artifact_version — ADK artifact path. Bytes
        never go through function_response; GE renders natively.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ----------------------------------------------------------------------------
# Export result — what every export_* tool returns
# ----------------------------------------------------------------------------

@dataclass
class ExportResult:
    """Result of an export tool call.

    Fields:
        filename: Final sanitized filename with extension (e.g. "walmart_360.pdf").
            This is the name under which the artifact was saved via ADK.
        mime_type: MIME type of the content (e.g. "application/pdf").
        size_kb: File size in KB, useful for logging and so the model can
            mention the file size to the user.
        format: Short label ("png", "xlsx", "html", "pdf", "docx", "pptx").
        artifact_version: Version number assigned by ADK's artifact service
            when save_artifact() was called. ADK handles versioning across
            same-name artifacts in a session. The agent can mention this to
            disambiguate re-exports of the same filename.

    The bytes themselves are NOT in this object. They've already been handed
    to ADK via tool_context.save_artifact(), and ADK is responsible for
    surfacing them in the chat UI.
    """

    filename: str
    mime_type: str
    size_kb: int
    format: str
    artifact_version: int

    def to_dict(self) -> dict:
        """Serialize for ADK tool return (JSON-safe, and tiny — no bytes, no URLs)."""
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_kb": self.size_kb,
            "format": self.format,
            "artifact_version": self.artifact_version,
            # status field helps the model recognize success unambiguously
            "status": "success",
        }


# ----------------------------------------------------------------------------
# MIME types — wire types for delivery to Gemini Enterprise
#
# GE's chat attachment renderer treats certain MIME types as "unsafe" for
# inline preview and displays "Unsupported attachment" instead of a normal
# download card. Confirmed empirically 2026-04-19:
#   - image/svg+xml  -> "Unsupported attachment" (browser sandboxed away)
#   - text/html      -> "Unsupported attachment" (browser sandboxed away)
#
# Both contain executable payloads (scripts, XSS vectors) that GE refuses
# to hand to the chat iframe. The workaround: send them as
# application/octet-stream (generic binary). GE then offers a normal
# download button; when the user saves the file, the filename extension
# (.svg / .html) tells their OS how to open it locally — browsers and
# image viewers handle the file correctly from disk.
#
# PNG, PDF, CSV, and the Office formats display correctly with their
# canonical MIME types because GE has native inline previews for them.
# ----------------------------------------------------------------------------

MIME_TYPES = {
    "png": "image/png",
    # SVG — sent as octet-stream to avoid GE's "Unsupported attachment"
    # when MIME is image/svg+xml. Real type preserved in filename extension.
    "svg": "application/octet-stream",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # HTML — same story as SVG. Browsers open .html files from disk fine.
    "html": "application/octet-stream",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # ZIP — the HTML download path wraps interactive Vega HTML in a zip
    # since GE refuses to render .html attachments directly. The zip
    # contains a single chart.html member the user extracts and opens.
    "zip": "application/zip",
}


# ----------------------------------------------------------------------------
# Session descriptor — common fields for all in-progress documents
# ----------------------------------------------------------------------------

@dataclass
class BaseSession:
    """Base class for per-format session objects.

    Each format's session extends this with its own builder state (e.g. the
    workbook for XLSX, the Document for DOCX). The registry in _sessions.py
    keys all sessions by session_id regardless of format.
    """

    session_id: str
    format: str
    created_at: float  # time.time() when created
    metadata: dict = field(default_factory=dict)  # user-supplied title, filename hint, etc.
