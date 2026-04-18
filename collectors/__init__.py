"""
Collectors package.

Provides shared GCP credential loading for the optional Cloud Monitoring
and Cloud Logging collectors. Returns ``None`` gracefully when the google
SDK is not installed (base image, no ``[gcp]`` extra) or when no
credentials are configured, so the rest of the agent runs fine against
any MySQL without a cloud dependency.
"""

import logging
import os

logger = logging.getLogger(__name__)

_monitoring_credentials = None
_credentials_resolved = False

try:
    import google.auth  # noqa: F401  (probe-import only)
    import google.oauth2.service_account  # noqa: F401
    _GOOGLE_AVAILABLE = True
except ImportError:
    _GOOGLE_AVAILABLE = False
    logger.debug(
        "google-auth not installed; GCP Cloud Monitoring / Cloud Logging "
        "collectors will skip. Install the [gcp] extra to enable them."
    )


def get_monitoring_credentials():
    """Load GCP credentials for Cloud Monitoring / Cloud Logging.

    Returns ``None`` when the google SDK is not installed, no credentials
    are configured, or ADC discovery fails. Callers must be prepared to
    skip their collection cycle on ``None``.

    Resolution order:
      1. ``MONITORING_APPLICATION_CREDENTIALS`` env var (dedicated SA)
      2. ``gcp.monitoring_credentials_file`` in config
      3. Application Default Credentials (``gcloud`` / GCE metadata),
         with ``GOOGLE_APPLICATION_CREDENTIALS`` temporarily unset so a
         Vertex AI SA does not override monitoring access.
    """
    global _monitoring_credentials, _credentials_resolved

    if _credentials_resolved:
        return _monitoring_credentials
    _credentials_resolved = True

    if not _GOOGLE_AVAILABLE:
        return None

    import google.auth
    import google.oauth2.service_account

    SCOPES = [
        "https://www.googleapis.com/auth/monitoring.read",
        "https://www.googleapis.com/auth/logging.read",
    ]

    cred_path = os.environ.get("MONITORING_APPLICATION_CREDENTIALS")
    if not cred_path:
        try:
            from config import get_config
            cred_path = get_config().get("gcp", {}).get("monitoring_credentials_file")
        except Exception:
            pass

    if cred_path and os.path.exists(cred_path):
        logger.info("Loading monitoring credentials from: %s", cred_path)
        _monitoring_credentials = (
            google.oauth2.service_account.Credentials.from_service_account_file(
                cred_path, scopes=SCOPES,
            )
        )
        return _monitoring_credentials

    saved = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    try:
        credentials, _ = google.auth.default(scopes=SCOPES)
        logger.info("Using ADC (gcloud/GCE) for monitoring credentials")
        _monitoring_credentials = credentials
        return _monitoring_credentials
    except Exception as e:
        logger.debug("ADC unavailable; skipping GCP collectors: %s", e)
        return None
    finally:
        if saved is not None:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = saved
