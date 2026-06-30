"""
Collectors package.

Provides shared GCP credential loading for the optional Cloud Monitoring
and Cloud Logging collectors. Returns ``None`` gracefully when the google
SDK is not installed (base image, no ``[gcp]`` extra) or when no
credentials are configured, so the rest of the agent runs fine against
any MySQL without a cloud dependency.
"""

import importlib.util
import logging
import os
import time

logger = logging.getLogger(__name__)

_monitoring_credentials = None
_credentials_resolved = False
# After a failed resolution we briefly back off so that multiple collectors in
# the same cycle don't each re-probe (the ADC path momentarily unsets
# GOOGLE_APPLICATION_CREDENTIALS). The window is intentionally short so creds
# still self-heal within ~one medium loop after a startup blip — NOT a long
# latch that would blind the GCP collectors for many minutes.
_credentials_failed_at = 0.0
_CREDENTIALS_RETRY_BACKOFF_SEC = 60

def _google_sdk_available() -> bool:
    """Whether the google-auth SDK is importable, WITHOUT importing it.

    We use ``find_spec`` rather than a module-level ``import`` so that merely
    importing a collector (every loop imports this package) never eagerly loads
    ``google.oauth2.service_account`` — that pulls in the ``cryptography`` Rust
    OpenSSL bindings, which are only needed on the service-account-key path. A
    pure-MySQL or ADC-based deployment shouldn't drag in (or risk a load-time
    crash on) the crypto stack it never uses.
    """
    try:
        return importlib.util.find_spec("google.auth") is not None
    except (ImportError, ValueError):
        return False


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

    if not _google_sdk_available():
        return None

    import google.auth

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
        # Imported lazily, only on the service-account-key path: this is what
        # loads the cryptography Rust OpenSSL bindings.
        import google.oauth2.service_account
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
