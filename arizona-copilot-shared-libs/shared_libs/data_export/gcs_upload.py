"""
Google Cloud Storage upload + signed URL generation for interactive HTML charts.

PURPOSE
=======
Gemini Enterprise refuses to display .html downloads as attachments (security
policy — HTML can contain scripts). But GE DOES respect markdown links in
A2UI Text components. So instead of delivering HTML as a file attachment,
we upload the HTML to GCS and render a "[Open interactive chart](url)"
link in the chart Card. User clicks, browser fetches the signed URL, GCS
serves the HTML, browser renders the interactive Vega chart with full
hover tooltips.

This module encapsulates the upload + signing machinery. Consumers pass
HTML bytes + a chart_id, get back a signed URL ready for the template.

STORAGE LAYOUT
==============
    Bucket: ARIZONA_CHART_BUCKET (env var, defaults to "arizonaai-agent-staging")
    Prefix: "charts/"
    Object name: "charts/{chart_id}.html"

ONE-TIME INFRASTRUCTURE REQUIREMENTS
====================================
The Agent Engine's service account needs the following (one-time gcloud setup):

    # 1. Grant the service account permission to sign URLs.
    #    Replace PROJECT_NUMBER with the numeric project id (not the string id).
    gcloud iam service-accounts add-iam-policy-binding \
        service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com \
        --member=serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com \
        --role=roles/iam.serviceAccountTokenCreator

    # 2. Grant the service account storage.objectAdmin on the bucket.
    gcloud storage buckets add-iam-policy-binding gs://arizonaai-agent-staging \
        --member=serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com \
        --role=roles/storage.objectAdmin

    # 3. Set a lifecycle policy to auto-delete charts/ objects after 7 days.
    # This prevents unbounded growth. Create a lifecycle.json and apply:
    cat > lifecycle.json <<EOF
    {
      "rule": [
        {
          "action": {"type": "Delete"},
          "condition": {"age": 7, "matchesPrefix": ["charts/"]}
        }
      ]
    }
    EOF
    gcloud storage buckets update gs://arizonaai-agent-staging \
        --lifecycle-file=lifecycle.json

Without step 1 (serviceAccountTokenCreator), signed URL generation fails with
"you need a private key". Step 2 may already be satisfied since the bucket is
already used for staging agent packages.

SIGNING MECHANISM
=================
We use the "IAM-based signing" path (not a downloaded JSON key). The service
account creates URLs signed by its own IAM identity. This is the Google-
recommended approach for workloads running on GCP compute — no key files
checked into code or mounted from secrets. Requires step 1 above.

FALLBACK
========
If the upload or signing fails, the caller gets a None return. The agent's
A2UI template still includes the HTML link Text component, but the
placeholder text will remain instead of a real URL — user sees something
like "Open interactive chart: [Open in new tab](HTML_URL_HERE)" which
will click but hit a 404. Not pretty; logs will have captured the
underlying failure. Subsequent chart requests in the same session may
succeed if the transient issue resolves.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Optional


logger = logging.getLogger(__name__)


# Environment config. Overridable at deploy time if we want a separate
# chart bucket later.
_BUCKET_NAME = os.environ.get("ARIZONA_CHART_BUCKET", "arizonaai-agent-staging")
_OBJECT_PREFIX = "charts/"

# URL-signing identity. Agent Engine runs code under a tenant-project
# service account (service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re...)
# whose iamcredentials.googleapis.com API we can't enable because the
# tenant project is Google-managed. Instead we impersonate a service
# account in OUR project, where the API is enabled.
#
# The Compute Engine default SA is the simplest choice — it already
# exists in every GCP project, has reasonable defaults, and requires
# no extra provisioning. The RE agent needs roles/iam.serviceAccount
# TokenCreator on this SA to impersonate it. See deploy.md or the
# one-time setup commands referenced in this module's docstring.
_SIGNING_SA_EMAIL = os.environ.get(
    "ARIZONA_CHART_SIGNING_SA",
    f"{os.environ.get('GOOGLE_CLOUD_PROJECT_NUMBER', '524970374556')}"
    f"-compute@developer.gserviceaccount.com",
)

# Signed URL expiration. 24 hours balances security (URL can't leak and
# remain usable forever) with UX (user can revisit the link later same day).
_SIGNED_URL_EXPIRATION = timedelta(hours=24)

# Object content-type. HTML is the actual content; GE isn't the one fetching
# it (the user's browser is), so the "unsafe content-type" issue doesn't
# apply here.
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"


def upload_html_and_sign(
    html_bytes: bytes,
    chart_id: str,
) -> Optional[str]:
    """Upload HTML bytes to GCS under charts/{chart_id}.html and return a
    signed URL valid for 24 hours.

    Returns None on any failure. Callers should treat None as "the link
    won't work for this turn" and gracefully degrade (e.g. leave the
    placeholder token or omit the link Text component).

    Idempotent: re-uploading the same chart_id overwrites the previous
    object (signed URLs are per-call; old URLs still work against the
    new content). That's fine because chart_ids are per-turn.
    """
    print(
        f"[GCS_UPLOAD] starting: chart_id={chart_id!r} bytes={len(html_bytes)} "
        f"bucket={_BUCKET_NAME!r}",
        flush=True,
    )

    if not chart_id:
        print("[GCS_UPLOAD] empty chart_id — aborting", flush=True)
        return None

    try:
        # Lazy import: keeps this module importable in environments
        # without google-cloud-storage (e.g. unit tests, minimal venvs).
        from google.cloud import storage
        from google.auth import default as default_credentials
        from google.auth.transport import requests as auth_requests
    except ImportError as exc:
        print(
            f"[GCS_UPLOAD] ImportError on google-cloud-storage / google-auth: "
            f"{exc} — package missing from runtime",
            flush=True,
        )
        return None

    try:
        print("[GCS_UPLOAD] constructing storage.Client()", flush=True)
        client = storage.Client()
        bucket = client.bucket(_BUCKET_NAME)
        blob_name = f"{_OBJECT_PREFIX}{chart_id}.html"
        blob = bucket.blob(blob_name)

        # Upload. We explicitly set content-type so when the browser
        # eventually fetches the signed URL, it renders as HTML (not as
        # "octet-stream download").
        print(f"[GCS_UPLOAD] uploading to gs://{_BUCKET_NAME}/{blob_name}", flush=True)
        blob.upload_from_string(
            html_bytes,
            content_type=_HTML_CONTENT_TYPE,
        )
        print(
            f"[GCS_UPLOAD] upload OK: gs://{_BUCKET_NAME}/{blob_name} "
            f"({len(html_bytes)} bytes)",
            flush=True,
        )

        # Sign URL using IAM-based signing via impersonation.
        #
        # The RE agent (whose credentials we have) impersonates a SA in
        # our project (the Compute Engine default SA) that DOES have
        # iamcredentials.googleapis.com enabled. We generate an access
        # token for that target SA, then ask generate_signed_url to sign
        # using it.
        #
        # Why not use credentials.service_account_email directly: the RE
        # agent lives in Google's tenant project 440967795959 where
        # iamcredentials API cannot be enabled by customers, so direct
        # signing fails with PERMISSION_DENIED / SERVICE_DISABLED.
        from google.auth import impersonated_credentials

        print("[GCS_UPLOAD] resolving source credentials for impersonation", flush=True)
        source_credentials, project = default_credentials()
        print(
            f"[GCS_UPLOAD] source credentials type={type(source_credentials).__name__} "
            f"project={project!r}",
            flush=True,
        )

        print(
            f"[GCS_UPLOAD] building impersonated credentials for "
            f"target={_SIGNING_SA_EMAIL!r}",
            flush=True,
        )
        try:
            target_credentials = impersonated_credentials.Credentials(
                source_credentials=source_credentials,
                target_principal=_SIGNING_SA_EMAIL,
                target_scopes=[
                    "https://www.googleapis.com/auth/cloud-platform",
                ],
                lifetime=3600,
            )
        except Exception as imp_exc:  # noqa: BLE001
            print(
                f"[GCS_UPLOAD] impersonated_credentials.Credentials() failed: "
                f"{type(imp_exc).__name__}: {imp_exc}",
                flush=True,
            )
            return None

        print("[GCS_UPLOAD] refreshing impersonated credentials", flush=True)
        try:
            target_credentials.refresh(auth_requests.Request())
        except Exception as refresh_exc:  # noqa: BLE001
            print(
                f"[GCS_UPLOAD] impersonated credentials.refresh() failed: "
                f"{type(refresh_exc).__name__}: {refresh_exc}",
                flush=True,
            )
            return None

        has_token = bool(getattr(target_credentials, "token", None))
        print(
            f"[GCS_UPLOAD] impersonated access_token present={has_token}",
            flush=True,
        )

        print(
            f"[GCS_UPLOAD] calling generate_signed_url v4 via impersonation "
            f"sa_email={_SIGNING_SA_EMAIL!r} exp={_SIGNED_URL_EXPIRATION}",
            flush=True,
        )
        try:
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=_SIGNED_URL_EXPIRATION,
                method="GET",
                credentials=target_credentials,
            )
        except Exception as sign_exc:  # noqa: BLE001
            print(
                f"[GCS_UPLOAD] generate_signed_url failed: "
                f"{type(sign_exc).__name__}: {sign_exc}",
                flush=True,
            )
            return None

        print(
            f"[GCS_UPLOAD] SUCCESS: signed URL generated "
            f"(length={len(signed_url)}, starts={signed_url[:60]!r})",
            flush=True,
        )
        return signed_url

    except Exception as exc:  # noqa: BLE001
        # Absolute last resort: any failure in upload or signing returns
        # None. Caller handles gracefully.
        print(
            f"[GCS_UPLOAD] outer exception for chart_id={chart_id!r}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        logger.exception(
            "Failed to upload+sign chart HTML for chart_id=%s", chart_id,
        )
        return None


def upload_bytes_to_staging(
    raw_bytes: bytes,
    object_name: str,
    content_type: str = "application/octet-stream",
) -> Optional[str]:
    """Upload raw bytes to gs://<staging bucket>/<object_name>.

    Reuses the same staging bucket (_BUCKET_NAME) and default runtime
    credentials as the chart uploader. No URL signing is done here: the
    consumer (the scheduled emailer) reads the object with its OWN service
    account, so the agent only needs write access (which the RE SA already
    has on this bucket). Returns the gs:// URI on success, or None on any
    failure -- best-effort, never raises, so a publish problem can never break
    the report itself.
    """
    if not object_name:
        print("[GCS_UPLOAD] empty object_name - aborting", flush=True)
        return None
    try:
        from google.cloud import storage
    except ImportError as exc:
        print(f"[GCS_UPLOAD] storage import failed: {exc}", flush=True)
        return None
    try:
        client = storage.Client()
        blob = client.bucket(_BUCKET_NAME).blob(object_name)
        blob.upload_from_string(raw_bytes, content_type=content_type)
        uri = f"gs://{_BUCKET_NAME}/{object_name}"
        print(f"[GCS_UPLOAD] uploaded {len(raw_bytes)} bytes -> {uri}", flush=True)
        return uri
    except Exception as exc:  # noqa: BLE001
        print(
            f"[GCS_UPLOAD] upload_bytes_to_staging failed for {object_name!r}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return None


__all__ = ["upload_html_and_sign", "upload_bytes_to_staging"]
