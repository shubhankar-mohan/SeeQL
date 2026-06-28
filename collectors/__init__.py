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
import time

logger = logging.getLogger(__name__)

_monitoring_credentials = None
_credentials_resolved = False
# After a failed resolution we back off instead of re-probing every cycle: the
# ADC path briefly unsets GOOGLE_APPLICATION_CREDENTIALS (which a Vertex client
# may need), so probing every 5-minute medium loop would churn it. We still
# retry periodically so credentials self-heal once they become available.
_credentials_failed_at = 0.0
_CREDENTIALS_RETRY_BACKOFF_SEC = 1800

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
    global _monitoring_credentials, _credentials_resolved, _credentials_failed_at

    # Only a SUCCESSFUL resolution is cached. A failed/transient resolution
    # (ADC or GCE metadata endpoint not ready yet, momentarily unreadable
    # key) is never cached, so the next collection cycle retries and the
    # GCP collectors self-heal instead of being disabled for the process
    # lifetime.
    if _credentials_resolved:
        return _monitoring_credentials

    # ...but don't re-probe on every cycle after a failure (the ADC path
    # briefly unsets GOOGLE_APPLICATION_CREDENTIALS); back off, then retry.
    if _credentials_failed_at and (time.monotonic() - _credentials_failed_at) < _CREDENTIALS_RETRY_BACKOFF_SEC:
        return None

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
        try:
            credentials = (
                google.oauth2.service_account.Credentials.from_service_account_file(
                    cred_path, scopes=SCOPES,
                )
            )
        except Exception as e:
            logger.debug(
                "Monitoring credentials file unreadable (%s); will retry: %s",
                cred_path, e,
            )
            _credentials_failed_at = time.monotonic()
            return None
        logger.info("Loading monitoring credentials from: %s", cred_path)
        _monitoring_credentials = credentials
        _credentials_resolved = True
        return _monitoring_credentials

    saved = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    try:
        credentials, _ = google.auth.default(scopes=SCOPES)
        logger.info("Using ADC (gcloud/GCE) for monitoring credentials")
        _monitoring_credentials = credentials
        _credentials_resolved = True
        return _monitoring_credentials
    except Exception as e:
        logger.debug("ADC unavailable; skipping GCP collectors: %s", e)
        _credentials_failed_at = time.monotonic()
        return None
    finally:
        if saved is not None:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = saved
