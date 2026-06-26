"""
Server Registry — manages the list of monitored MySQL servers.

Loads server configurations from YAML and provides lookup methods.
If no `servers:` key exists in config, auto-creates a 'default' server
from the legacy `production_db:` section for backward compatibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.server_context import ServerContext

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Full configuration for a monitored MySQL server."""

    server_id: str
    display_name: str
    environment: str = "production"
    role: str = "primary"
    cluster_id: str | None = None
    primary_server_id: str | None = None
    tags: dict = field(default_factory=dict)
    db_config: dict = field(default_factory=dict)   # host, port, user, password, database, pool_size
    gcp_config: dict = field(default_factory=dict)   # project_id, cloud_sql_instance_id, region
    is_active: bool = True

    def to_context(self) -> ServerContext:
        """Create a ServerContext for use in collectors and tools."""
        return ServerContext(
            server_id=self.server_id,
            display_name=self.display_name,
            environment=self.environment,
            role=self.role,
            cluster_id=self.cluster_id,
            primary_server_id=self.primary_server_id,
            tags=self.tags,
            gcp_config=self.gcp_config,
        )


class ServerRegistry:
    """Manages the list of monitored MySQL servers."""

    def __init__(self):
        self._servers: dict[str, ServerConfig] = {}

    def load_from_config(self, config: dict) -> None:
        """Load servers from the full config dict.

        If config has a `servers:` key, load from there.
        Otherwise, synthesize a single 'default' server from `production_db:`.
        """
        servers_config = config.get("servers")

        if servers_config and isinstance(servers_config, dict):
            for server_id, srv_cfg in servers_config.items():
                self._servers[server_id] = ServerConfig(
                    server_id=server_id,
                    display_name=srv_cfg.get("display_name", server_id),
                    environment=srv_cfg.get("environment", "production"),
                    role=srv_cfg.get("role", "primary"),
                    cluster_id=srv_cfg.get("cluster_id"),
                    primary_server_id=srv_cfg.get("primary_server_id"),
                    tags=srv_cfg.get("tags", {}),
                    db_config={
                        k: srv_cfg[k]
                        for k in ("host", "port", "user", "password", "database", "pool_size", "connect_timeout")
                        if k in srv_cfg
                    },
                    gcp_config=srv_cfg.get("gcp", {}),
                    is_active=srv_cfg.get("is_active", True),
                )
            logger.info(f"Loaded {len(self._servers)} server(s) from config: {list(self._servers.keys())}")
        else:
            # Backward compatibility: create 'default' from production_db
            prod_db = config.get("production_db", {})
            gcp = config.get("gcp", {})
            self._servers["default"] = ServerConfig(
                server_id="default",
                display_name="Production",
                environment="production",
                role="primary",
                db_config=prod_db,
                gcp_config=gcp,
                is_active=True,
            )
            logger.info("No 'servers' config found, using legacy 'production_db' as 'default' server")

    def get_server(self, server_id: str) -> ServerConfig | None:
        return self._servers.get(server_id)

    def get_all_servers(self) -> list[ServerConfig]:
        return list(self._servers.values())

    def get_active_servers(self) -> list[ServerConfig]:
        return [s for s in self._servers.values() if s.is_active]

    def get_servers_by_environment(self, env: str) -> list[ServerConfig]:
        return [s for s in self._servers.values() if s.environment == env]

    def get_cluster(self, cluster_id: str) -> list[ServerConfig]:
        return [s for s in self._servers.values() if s.cluster_id == cluster_id]

    def get_default_server_id(self) -> str:
        """Return the first primary server_id, or just the first one."""
        for s in self._servers.values():
            if s.role == "primary":
                return s.server_id
        return next(iter(self._servers)).server_id if self._servers else "default"

    def get_servers_grouped_by_env(self) -> dict[str, list[ServerConfig]]:
        """Group servers by environment for the UI dropdown."""
        groups: dict[str, list[ServerConfig]] = {}
        for s in self._servers.values():
            groups.setdefault(s.environment, []).append(s)
        return groups

    def sync_to_db(self) -> None:
        """Upsert server registry into the `servers` SQLite table."""
        from storage.connection import get_mon_connection

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        with get_mon_connection() as conn:
            for s in self._servers.values():
                import json
                conn.execute(
                    """INSERT INTO servers (server_id, display_name, environment, role,
                           cluster_id, tags, host, port, is_active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(server_id) DO UPDATE SET
                           display_name=excluded.display_name,
                           environment=excluded.environment,
                           role=excluded.role,
                           cluster_id=excluded.cluster_id,
                           tags=excluded.tags,
                           host=excluded.host,
                           port=excluded.port,
                           is_active=excluded.is_active,
                           updated_at=excluded.updated_at
                    """,
                    (
                        s.server_id, s.display_name, s.environment, s.role,
                        s.cluster_id, json.dumps(s.tags),
                        s.db_config.get("host"), s.db_config.get("port", 3306),
                        1 if s.is_active else 0, now, now,
                    ),
                )
        logger.info(f"Synced {len(self._servers)} server(s) to database")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: ServerRegistry | None = None


def get_server_registry() -> ServerRegistry:
    """Get or lazily create the global server registry."""
    global _registry
    if _registry is None:
        from config import get_config
        _registry = ServerRegistry()
        _registry.load_from_config(get_config())
    return _registry
