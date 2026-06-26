"""
Inbound webhook adapters.

External alerting systems POST to `/webhooks/{provider}`. Each adapter is
responsible for (a) verifying the provider-specific authentication scheme and
(b) normalizing the provider's payload into an `InboundAlert`.

Adapters expose two methods:

    verify_signature(body: bytes, headers: Mapping[str, str], secret: str) -> bool
    normalize(payload: dict, headers: Mapping[str, str]) -> InboundAlert

The router reads the RAW request body first, passes it to `verify_signature`,
and only after that parses JSON to call `normalize`. This ordering is
deliberate and security-relevant — never parse a payload an attacker may have
tampered with before you've verified the signature.
"""

from alerting.inbound.models import InboundAlert
from alerting.inbound.generic import GenericAdapter
from alerting.inbound.gcp import GCPAdapter
from alerting.inbound.pagerduty import PagerDutyAdapter
from alerting.inbound.grafana import GrafanaAdapter


ADAPTERS = {
    "generic": GenericAdapter(),
    "gcp": GCPAdapter(),
    "pagerduty": PagerDutyAdapter(),
    "grafana": GrafanaAdapter(),
}


def get_adapter(provider: str):
    """Return the adapter instance for `provider`, or None if unknown."""
    return ADAPTERS.get(provider)


__all__ = [
    "InboundAlert",
    "GenericAdapter",
    "GCPAdapter",
    "PagerDutyAdapter",
    "GrafanaAdapter",
    "ADAPTERS",
    "get_adapter",
]
