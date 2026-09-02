"""
GCS upload + V4 signed URL generation (keyless, path-style).

Final working recipe (derived by diffing against gcloud's sign-url output):

    Region: use the actual bucket region (us-east4), NOT "auto". The V4
    signing scope is bound to whatever we put here, and GCS only
    validates signatures whose scope matches the bucket's real region.

    Query parameter casing: lowercase (x-goog-algorithm, etc.), not
    TitleCase. V4 canonical sort is case-sensitive; gcloud signs
    lowercase, and we want our URLs to match gcloud's verified format.

    URL format: path-style (storage.googleapis.com/{bucket}/{object}),
    host header is plain storage.googleapis.com (not a virtual host).

    Signed headers: just "host". Nothing else.

Prior art:
    https://cloud.google.com/storage/docs/access-control/signing-urls-manually
    https://cloud.google.com/storage/docs/authentication/canonical-requests
    Verified against gcloud storage sign-url output (2026-04-19).

Required IAM:
    * Runtime identity has roles/iam.serviceAccountTokenCreator on the
      target SA (for signBlob).
    * Target SA has roles/storage.objectUser on the bucket (for upload
      + read, which signed URLs need).

Object path structure:
    exports/{YYYY}/{MM}/{DD}/{random}/{filename}

Lifecycle:
    14-day bucket auto-delete; 7-day URL expiry.
"""

from __future__ import annotations

import binascii
import collections
import datetime as _dt
import hashlib
import logging
import os
import secrets
from urllib.parse import quote

import google.auth
import google.auth.transport.requests
from google.cloud import iam_credentials_v1
from google.cloud import storage


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_BUCKET_NAME = "arizonaai-agent-exports"
DEFAULT_URL_EXPIRY_DAYS = 7
DEFAULT_BUCKET_REGION = "us-east4"

SIGNING_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def get_bucket_name() -> str:
    return os.environ.get("EXPORTS_BUCKET_NAME", DEFAULT_BUCKET_NAME)


def get_bucket_region() -> str:
    return os.environ.get("EXPORTS_BUCKET_REGION", DEFAULT_BUCKET_REGION)


def get_url_expiry_days() -> int:
    try:
        return int(os.environ.get("EXPORTS_URL_EXPIRY_DAYS", DEFAULT_URL_EXPIRY_DAYS))
    except ValueError:
        return DEFAULT_URL_EXPIRY_DAYS


def _resolve_signing_sa_email(credentials) -> str:
    """Find the service account email to sign as.

    Resolution order:
        1. EXPORTS_SIGNING_SA_EMAIL env var — explicit override, always wins
           when set. This is how production on Agent Engine sets the
           signing identity to `wms-ai-agent` rather than the Google-
           managed runtime service agent (which cannot sign on behalf
           of itself because we can't grant IAM on Google-managed SAs).
        2. credentials.service_account_email — for native SA credentials
           where the identity is the SA itself.
        3. credentials._target_principal — for impersonated_credentials
           on older google-auth versions.

    The env-var path is CRITICAL in production. Without it, on Agent
    Engine the code tries to sign as `service-NNNN@gcp-sa-aiplatform-re`,
    which fails with NOT_FOUND because that SA is Google-managed and
    cannot grant or receive tokenCreator roles.
    """
    # Explicit env-var override wins — always
    sa_email = os.environ.get("EXPORTS_SIGNING_SA_EMAIL")
    if sa_email:
        return sa_email

    sa_email = getattr(credentials, "service_account_email", None)
    if sa_email:
        return sa_email
    sa_email = getattr(credentials, "_target_principal", None)
    if sa_email:
        return sa_email

    raise RuntimeError(
        "Cannot determine signing service account email. On Agent Engine "
        "this is automatic via the runtime SA. For local dev, impersonate "
        "an SA via 'gcloud auth application-default login "
        "--impersonate-service-account=...' or set EXPORTS_SIGNING_SA_EMAIL."
    )


# ---------------------------------------------------------------------------
# V4 signed URL construction (manual, matching gcloud's output format)
# ---------------------------------------------------------------------------

def _generate_v4_signed_url(
    credentials,
    sa_email: str,
    bucket_name: str,
    object_name: str,
    bucket_region: str,
    expiration_seconds: int,
    http_method: str = "GET",
) -> str:
    """Build a V4 signed URL and sign it via IAM signBlob.

    Output format matches gcloud's `gcloud storage sign-url`:
        - Path-style: https://storage.googleapis.com/{bucket}/{object}
        - Host header: storage.googleapis.com
        - Query param names LOWERCASE (x-goog-algorithm, etc.)
        - Credential scope uses actual bucket region
        - No response-content-disposition (keeps the URL simple; the
          file uploads with Content-Disposition: attachment already via
          the upload metadata)
    """
    if expiration_seconds > 604800:
        raise ValueError("V4 signed URLs cannot exceed 7 days (604800 seconds)")

    escaped_object_name = quote(object_name.encode("utf-8"), safe=b"/~")
    canonical_uri = f"/{bucket_name}/{escaped_object_name}"

    datetime_now = _dt.datetime.now(tz=_dt.timezone.utc)
    request_timestamp = datetime_now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = datetime_now.strftime("%Y%m%d")

    credential_scope = f"{datestamp}/{bucket_region}/storage/goog4_request"
    credential = f"{sa_email}/{credential_scope}"

    host = "storage.googleapis.com"
    headers = {"host": host}
    ordered_headers = collections.OrderedDict(sorted(headers.items()))
    canonical_headers = ""
    for k, v in ordered_headers.items():
        canonical_headers += f"{str(k).lower()}:{str(v).lower()}\n"
    signed_headers = ";".join(str(k).lower() for k in ordered_headers.keys())

    # LOWERCASE query parameter names — this is what gcloud produces and
    # we need exact byte-match for V4 canonical string compatibility.
    query_parameters = {
        "x-goog-algorithm": "GOOG4-RSA-SHA256",
        "x-goog-credential": credential,
        "x-goog-date": request_timestamp,
        "x-goog-expires": expiration_seconds,
        "x-goog-signedheaders": signed_headers,
    }
    ordered_query = collections.OrderedDict(sorted(query_parameters.items()))
    canonical_query_string = "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
        for k, v in ordered_query.items()
    )

    canonical_request = "\n".join([
        http_method,
        canonical_uri,
        canonical_query_string,
        canonical_headers,
        signed_headers,
        "UNSIGNED-PAYLOAD",
    ])

    canonical_request_hash = hashlib.sha256(
        canonical_request.encode("utf-8")
    ).hexdigest()

    string_to_sign = "\n".join([
        "GOOG4-RSA-SHA256",
        request_timestamp,
        credential_scope,
        canonical_request_hash,
    ])

    iam_client = iam_credentials_v1.IAMCredentialsClient(credentials=credentials)
    sign_response = iam_client.sign_blob(
        request={
            "name": f"projects/-/serviceAccounts/{sa_email}",
            "payload": string_to_sign.encode("utf-8"),
        }
    )
    signature = binascii.hexlify(sign_response.signed_blob).decode()

    signed_url = (
        f"https://{host}{canonical_uri}"
        f"?{canonical_query_string}"
        f"&x-goog-signature={signature}"
    )
    return signed_url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_and_sign(
    raw_bytes: bytes,
    filename: str,
    mime_type: str,
) -> str:
    """Upload raw bytes to GCS and return a V4 signed URL for downloading.

    The file is uploaded with Content-Disposition: attachment metadata,
    so clicking the signed URL in a browser triggers a download. The
    Content-Disposition is set at upload time rather than via a query
    parameter because the query-parameter approach breaks V4 signatures
    in some environments (a mismatch between what was signed and what
    GCS serves).
    """
    bucket_name = get_bucket_name()
    bucket_region = get_bucket_region()
    expiry_days = get_url_expiry_days()
    expiry_seconds = expiry_days * 24 * 3600

    now = _dt.datetime.utcnow()
    rand_suffix = secrets.token_urlsafe(4)
    object_path = f"exports/{now:%Y/%m/%d}/{rand_suffix}/{filename}"

    credentials, project = google.auth.default(scopes=SIGNING_SCOPES)
    sa_email = _resolve_signing_sa_email(credentials)

    auth_request = google.auth.transport.requests.Request()
    credentials.refresh(auth_request)

    # Upload with Content-Disposition baked into object metadata
    client = storage.Client(project=project, credentials=credentials)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    blob.content_disposition = f'attachment; filename="{filename}"'
    blob.upload_from_string(raw_bytes, content_type=mime_type)
    logger.info(
        "Uploaded %d bytes to gs://%s/%s (disposition=attachment)",
        len(raw_bytes), bucket_name, object_path
    )

    # Sign URL — no response-content-disposition in query params since
    # the disposition is set on the object metadata instead.
    url = _generate_v4_signed_url(
        credentials=credentials,
        sa_email=sa_email,
        bucket_name=bucket_name,
        object_name=object_path,
        bucket_region=bucket_region,
        expiration_seconds=expiry_seconds,
    )
    logger.info(
        "Generated V4 signed URL (expires in %d days) for %s",
        expiry_days, filename
    )
    return url
