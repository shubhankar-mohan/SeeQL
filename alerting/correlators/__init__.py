"""
Signal correlators — pure functions that join SQLite-stored monitoring
data to produce structured evidence for the investigator.

Correlators never touch the production MySQL server. They only read the
SQLite monitoring DB and return structured findings. The investigator
decides whether those findings are enough to conclude Phase 1 (and skip
Phase 2 MySQL calls), or whether the LLM needs to dig further.
"""

from alerting.correlators.missing_index import (
    MissingIndexEvidence,
    MissingIndexCorrelation,
    correlate_missing_index,
)

__all__ = [
    "MissingIndexEvidence",
    "MissingIndexCorrelation",
    "correlate_missing_index",
]
