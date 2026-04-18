"""Alert data models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Alert:
    rule_name: str
    severity: Severity
    message: str
    context: dict = field(default_factory=dict)
    fired_at: str = ""
    channels: list[str] = field(default_factory=list)
    delivered: bool = False
    resolved_at: str | None = None

    def __post_init__(self):
        if not self.fired_at:
            self.fired_at = datetime.utcnow().isoformat()
