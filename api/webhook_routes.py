"""
Inbound webhook router.

POST /webhooks/{provider}
  - Verifies the provider-specific signature over the RAW request body
    (always before JSON parsing).
  - Normalizes the payload to an InboundAlert via the provider's adapter.
  - Dedups on (provider, external_id) within `webhooks.dedup_window_minutes`.
  - Caps concurrent open investigations per server.
  - Persists `inbound_alerts` + `investigations` rows and schedules the
    investigator on the existing APScheduler instance.

Rate-limiting is a process-local token bucket per provider (good enough for
a single-replica API; callers already have their own rate-limits upstream).
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request, status

from alerting.inbound import get_adapter
from storage import writer
from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Process-local rate limit + concurrency cap
# ---------------------------------------------------------------------------

_BUCKET_LOCK = threading.Lock()
_buckets: dict[str, dict] = {}  # provider -> {tokens, last_refill}


def _check_rate_limit(provider: str, rate_per_minute: int) -> bool:
    """
    Simple token-bucket: capacity = rate_per_minute, refilled linearly.
    Returns True if the request is allowed, False if throttled.
    """
    if rate_per_minute <= 0:
        return True
    now = time.monotonic()
    with _BUCKET_LOCK:
        b = _buckets.get(provider)
        if b is None:
            b = {"tokens": float(rate_per_minute), "last": now}
            _buckets[provider] = b
        else:
            elapsed = now - b["last"]
            refill = elapsed * (rate_per_minute / 60.0)
            b["tokens"] = min(float(rate_per_minute), b["tokens"] + refill)
            b["last"] = now
        if b["tokens"] >= 1.0:
            b["tokens"] -= 1.0
            return True
        return False


def _reset_rate_limiter_for_tests() -> None:
    """Test hook — clears the per-provider buckets."""
    with _BUCKET_LOCK:
        _buckets.clear()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _webhook_cfg() -> dict:
    try:
        from config import get_config
        return dict(get_config().get("webhooks") or {})
    except Exception:
        return {}


def _inv_cfg() -> dict:
    try:
        from config import get_config
        return dict(get_config().get("investigator") or {})
    except Exception:
        return {}


def _provider_cfg(provider: str) -> dict:
    return dict((_webhook_cfg().get("providers") or {}).get(provider) or {})


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

_DEDUP_STATUSES = (
    "queued", "phase1", "phase2", "phase3", "load_guard_paused",
)


def _find_dedup_investigation(
    provider: str, external_id: str, dedup_window_minutes: int
) -> int | None:
    """
    Look for an in-flight investigation whose inbound_alert matches
    (provider, external_id) and was received within the dedup window.
    """
    if not external_id:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=dedup_window_minutes)).isoformat()
    placeholders = ", ".join(["?"] * len(_DEDUP_STATUSES))
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                f"""
                SELECT i.id
                FROM investigations i
                JOIN inbound_alerts a ON i.inbound_alert_id = a.id
                WHERE a.provider = ?
                  AND a.external_id = ?
                  AND a.received_at >= ?
                  AND i.status IN ({placeholders})
                ORDER BY i.id DESC
                LIMIT 1
                """,
                (provider, external_id, cutoff, *_DEDUP_STATUSES),
            ).fetchone()
            return int(row["id"]) if row else None
    except Exception as e:
        logger.debug(f"dedup lookup failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------

def _active_investigations_for_server(server_id: str) -> int:
    placeholders = ", ".join(["?"] * len(_DEDUP_STATUSES))
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM investigations
                WHERE server_id = ? AND status IN ({placeholders})
                """,
                (server_id, *_DEDUP_STATUSES),
            ).fetchone()
            return int(row["c"] or 0)
    except Exception as e:
        logger.debug(f"active count lookup failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Scheduler hook
# ---------------------------------------------------------------------------

def _enqueue_investigation(investigation_id: int) -> None:
    """Schedule run_investigation(id) via the existing APScheduler instance,
    or fall back to a daemon thread when no scheduler is available
    (unit tests, single-shot CLI usage)."""
    try:
        from scheduler.runner import _scheduler_instance
        from alerting.investigator import run_investigation
    except Exception as e:
        logger.warning(f"Investigator imports unavailable: {e}")
        return

    if _scheduler_instance is None:
        def _bg():
            try:
                run_investigation(investigation_id)
            except Exception as e:
                logger.exception(f"inline investigation {investigation_id} failed: {e}")
        t = threading.Thread(target=_bg, daemon=True, name=f"inv-{investigation_id}")
        t.start()
        return

    try:
        from apscheduler.triggers.date import DateTrigger
        _scheduler_instance.add_job(
            run_investigation,
            trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
            args=[investigation_id],
            id=f"investigation:{investigation_id}",
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
        )
    except Exception as e:
        logger.warning(f"Failed to schedule investigation {investigation_id}: {e}")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/{provider}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(provider: str, request: Request) -> dict:
    cfg = _webhook_cfg()
    if not cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail="webhooks disabled")

    prov_cfg = _provider_cfg(provider)
    if not prov_cfg.get("enabled", False):
        raise HTTPException(status_code=404, detail=f"provider '{provider}' not enabled")

    adapter = get_adapter(provider)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown provider '{provider}'")

    rate = int(cfg.get("rate_limit_per_minute", 60) or 60)
    if not _check_rate_limit(provider, rate):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    # RAW body first — signature verification MUST happen before JSON parse.
    body: bytes = await request.body()
    headers: dict[str, str] = dict(request.headers)

    secret = str(prov_cfg.get("secret") or "")
    try:
        verified = adapter.verify_signature(body, headers, secret)
    except Exception as e:
        logger.warning(f"signature verify raised for {provider}: {e}")
        verified = False
    if not verified:
        logger.info(f"webhook {provider}: signature verification failed")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}")

    # Normalize (each adapter has a slightly different kwarg set).
    normalize_kwargs: dict[str, Any] = {
        "headers": headers,
        "provider_default_server_id": prov_cfg.get("default_server_id"),
    }
    if provider == "gcp":
        normalize_kwargs["policy_map"] = prov_cfg.get("policy_map") or {}
    try:
        alert = adapter.normalize(payload, **normalize_kwargs)
    except TypeError:
        # Some adapters may not accept every kwarg (e.g., older signatures).
        alert = adapter.normalize(payload, headers=headers)
    alert.signature_verified = True

    # Dedup
    dedup_minutes = int(cfg.get("dedup_window_minutes", 5) or 5)
    existing = _find_dedup_investigation(
        alert.provider, alert.external_id, dedup_minutes
    )
    if existing is not None:
        return {
            "investigation_id": existing,
            "status": "dedup",
            "message": (
                f"attached to existing investigation within "
                f"{dedup_minutes}-minute window"
            ),
        }

    # Concurrency cap
    inv_cfg = _inv_cfg()
    max_concurrent = int(inv_cfg.get("max_concurrent_per_server", 2) or 2)
    if _active_investigations_for_server(alert.server_id) >= max_concurrent:
        raise HTTPException(
            status_code=429,
            detail=f"too many active investigations for server '{alert.server_id}'",
        )

    # Persist inbound_alert + investigation (writer pattern — no inline SQL)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        alert_id = writer.write_inbound_alert({
            "provider": alert.provider,
            "received_at": now_iso,
            "server_id": alert.server_id,
            "external_id": alert.external_id,
            "alert_type": alert.alert_type,
            "severity": alert.severity,
            "summary": alert.summary,
            "payload": json.dumps(alert.raw_payload)[:200_000],
            "signature_verified": 1 if alert.signature_verified else 0,
            "callback_url": alert.callback_url,
        })
    except Exception as e:
        logger.exception(f"failed to persist inbound alert: {e}")
        raise HTTPException(status_code=500, detail="persistence error")

    try:
        inv_id = writer.write_investigation({
            "inbound_alert_id": alert_id,
            "server_id": alert.server_id,
            "started_at": now_iso,
            "status": "queued",
        })
    except Exception as e:
        logger.exception(f"failed to create investigation: {e}")
        raise HTTPException(status_code=500, detail="investigation create error")

    _enqueue_investigation(inv_id)

    return {
        "investigation_id": inv_id,
        "inbound_alert_id": alert_id,
        "status": "accepted",
        "alert_type": alert.alert_type,
        "server_id": alert.server_id,
    }
