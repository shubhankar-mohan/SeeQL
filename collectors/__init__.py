"""
Collectors package.

Provides shared GCP credential loading for monitoring collectors.
"""

import json
import logging
import os

import google.auth
import google.oauth2.service_account

logger = logging.getLogger(__name__)

_monitoring_credentials = None


def get_monitoring_credentials():
    """Load GCP credentials for Cloud Monitoring / Cloud Logging.

    Resolution order:
      1. MONITORING_APPLICATION_CREDENTIALS env var (dedicated SA or user key)
      2. gcp.monitoring_credentials_file in config
      3. Fall back to Application Default Credentials (ADC from gcloud CLI
         or GCE metadata), ignoring GOOGLE_APPLICATION_CREDENTIALS so the
         Vertex AI SA doesn't override monitoring access.
    """
    global _monitoring_credentials
    if _monitoring_credentials is not None:
        return _monitoring_credentials

    SCOPES = [
        "https://www.googleapis.com/auth/monitoring.read",
        "https://www.googleapis.com/auth/logging.read",
    ]

    # 1. Dedicated env var
    cred_path = os.environ.get("MONITORING_APPLICATION_CREDENTIALS")

    # 2. Config file setting
    if not cred_path:
        try:
            from config import get_config
            cred_path = get_config().get("gcp", {}).get("monitoring_credentials_file")
        except Exception:
            pass

    if cred_path and os.path.exists(cred_path):
        logger.info(f"Loading monitoring credentials from: {cred_path}")
        _monitoring_credentials = (
            google.oauth2.service_account.Credentials.from_service_account_file(
                cred_path, scopes=SCOPES,
            )
        )
        return _monitoring_credentials

    # 3. ADC — temporarily clear GOOGLE_APPLICATION_CREDENTIALS so
    #    google.auth.default() picks up gcloud user creds or GCE metadata
    #    instead of the Vertex AI service account.
    saved = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    try:
        credentials, project = google.auth.default(scopes=SCOPES)
        logger.info("Using ADC (gcloud/GCE) for monitoring credentials")
        _monitoring_credentials = credentials
        return _monitoring_credentials
    finally:
        if saved is not None:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = saved
