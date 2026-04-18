"""
ServerContext — the context object threaded through collectors, agent, and tools.

Replaces the implicit global `get_prod_connection()` with an explicit server reference.
Every collector receives a ServerContext to know which server to query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Generator


@dataclass(frozen=True)
class ServerContext:
    """Immutable context identifying a monitored MySQL server."""

    server_id: str
    display_name: str
    environment: str = "production"          # production, staging, dev
    role: str = "primary"                    # primary, replica
    cluster_id: str | None = None
    primary_server_id: str | None = None     # for replicas: which primary
    tags: dict = field(default_factory=dict)
    gcp_config: dict = field(default_factory=dict)

    @contextmanager
    def get_connection(self) -> Generator:
        """Get a MySQL connection for this server."""
        from storage.connection import get_prod_connection
        with get_prod_connection(self.server_id) as conn:
            yield conn

    @property
    def is_replica(self) -> bool:
        return self.role == "replica"

    @property
    def is_primary(self) -> bool:
        return self.role == "primary"
