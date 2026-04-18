"""
Database connection manager for MySQL DBA Agent.

Two separate backends:
    - Production MySQL (Cloud SQL): per-server connection pools via mysql-connector.
    - Monitoring SQLite: local file database for storing collected metrics.

Multi-server support:
    - ConnectionManager maintains a dict of pools keyed by server_id.
    - get_prod_connection(server_id) returns a connection for a specific server.
    - get_prod_connection() with no argument uses the default/legacy server.

SQLite design notes:
    - WAL mode enables concurrent readers + one writer without blocking.
    - We use a single shared connection for writes (SQLite is single-writer anyway).
    - busy_timeout prevents "database is locked" errors during brief contention.
    - Connection is created once and reused — no pool needed for SQLite.
"""

import sqlite3
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import mysql.connector
from mysql.connector import pooling

from config import get_mon_db_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Production MySQL — Multi-Server Connection Pools
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages MySQL connection pools for multiple monitored servers."""

    def __init__(self):
        self._pools: dict[str, pooling.MySQLConnectionPool] = {}
        self._lock = threading.Lock()

    def _create_pool(self, server_id: str, db_config: dict) -> pooling.MySQLConnectionPool:
        """Create a connection pool for a specific server."""
        pool_size = db_config.get("pool_size", 5)
        connect_timeout = db_config.get("connect_timeout", 10)

        mysql_params = {
            "host": db_config["host"],
            "port": db_config.get("port", 3306),
            "user": db_config["user"],
            "password": db_config["password"],
            "database": db_config.get("database"),
            "connect_timeout": connect_timeout,
        }

        pool = pooling.MySQLConnectionPool(
            pool_name=f"seeql_{server_id}",
            pool_size=pool_size,
            pool_reset_session=True,
            **mysql_params,
        )
        logger.info(
            f"Created MySQL connection pool for '{server_id}' "
            f"({db_config['host']}:{db_config.get('port', 3306)}) "
            f"with {pool_size} connections."
        )
        return pool

    def get_pool(self, server_id: str) -> pooling.MySQLConnectionPool:
        """Get or lazily create the pool for a server."""
        if server_id not in self._pools:
            with self._lock:
                if server_id not in self._pools:
                    from config.server_registry import get_server_registry
                    server = get_server_registry().get_server(server_id)
                    if not server:
                        raise ValueError(f"Unknown server_id: {server_id}")
                    self._pools[server_id] = self._create_pool(server_id, server.db_config)
        return self._pools[server_id]

    def health_check(self, server_id: str) -> bool:
        """Check if a specific server is reachable."""
        try:
            pool = self.get_pool(server_id)
            conn = pool.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                return True
            finally:
                if conn.is_connected():
                    conn.close()
        except Exception as e:
            logger.error(f"Health check failed for '{server_id}': {e}")
            return False

    def close_all(self) -> None:
        """Close all connection pools."""
        self._pools.clear()


# Module-level singleton
_connection_manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    return _connection_manager


@contextmanager
def get_prod_connection(server_id: str | None = None) -> Generator:
    """
    Get a read-only connection to a production MySQL database.

    Args:
        server_id: Which server to connect to. None = default/legacy server.

    Usage:
        with get_prod_connection("prod-main") as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SHOW PROCESSLIST")
            rows = cursor.fetchall()
    """
    if server_id is None:
        from config.server_registry import get_server_registry
        server_id = get_server_registry().get_default_server_id()

    conn = None
    try:
        conn = _connection_manager.get_pool(server_id).get_connection()
        yield conn
    except mysql.connector.Error as e:
        logger.error(f"Production DB connection error ({server_id}): {e}")
        raise
    finally:
        if conn and conn.is_connected():
            conn.close()  # returns to pool


# ---------------------------------------------------------------------------
# Monitoring SQLite — Single Connection with WAL
# ---------------------------------------------------------------------------

_mon_conn: sqlite3.Connection | None = None
_mon_lock = threading.Lock()


def _get_mon_connection_raw() -> sqlite3.Connection:
    """
    Get or create the shared SQLite connection for the monitoring database.

    Configures:
        - WAL journal mode (concurrent reads + single writer)
        - busy_timeout to wait instead of failing on lock
        - Foreign keys ON
        - Row factory for dict-like access
    """
    global _mon_conn
    if _mon_conn is not None:
        return _mon_conn

    config = get_mon_db_config()
    db_path = Path(config["path"])

    # Create parent directory if needed (skip for in-memory databases)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        # check_same_thread=False because APScheduler runs collectors
        # in different threads, but we serialize writes via _mon_lock.
        check_same_thread=False,
        timeout=config.get("busy_timeout_ms", 5000) / 1000,
    )

    # --- SQLite pragmas for performance ---
    if config.get("wal_mode", True):
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = %d" % config.get("busy_timeout_ms", 5000))
    conn.execute("PRAGMA synchronous = NORMAL")   # Safe with WAL, faster than FULL
    conn.execute("PRAGMA cache_size = -64000")     # 64 MB page cache
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")

    # Row factory: allows column access by name
    conn.row_factory = sqlite3.Row

    _mon_conn = conn
    logger.info(f"Opened SQLite monitoring database: {db_path}")
    return _mon_conn


@contextmanager
def get_mon_connection() -> Generator:
    """
    Get a write connection to the monitoring SQLite database.

    Uses a threading lock to serialize writes (SQLite is single-writer).
    Auto-commits on success, rolls back on exception.

    Usage:
        with get_mon_connection() as conn:
            conn.execute("INSERT INTO ...", values)
            # auto-committed on exit
    """
    with _mon_lock:
        conn = _get_mon_connection_raw()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Monitoring DB error: {e}")
            raise


@contextmanager
def get_mon_reader() -> Generator:
    """
    Get a read-only connection to the monitoring SQLite database.

    In WAL mode, readers don't block writers and vice versa.
    Creates a separate connection for reads to avoid contention
    with the writer connection.
    """
    config = get_mon_db_config()
    db_path = Path(config["path"])

    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=config.get("busy_timeout_ms", 5000) / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")

    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def reset_connections():
    """Reset all connection state. Used by tests."""
    global _mon_conn
    if _mon_conn is not None:
        try:
            _mon_conn.close()
        except Exception:
            pass
        _mon_conn = None
    _connection_manager.close_all()


def check_prod_connection(server_id: str | None = None) -> bool:
    """Verify we can connect to a production database."""
    if server_id is None:
        from config.server_registry import get_server_registry
        server_id = get_server_registry().get_default_server_id()
    return _connection_manager.health_check(server_id)


def check_mon_connection() -> bool:
    """Verify we can open the monitoring SQLite database."""
    try:
        with get_mon_connection() as conn:
            conn.execute("SELECT 1")
            return True
    except Exception as e:
        logger.error(f"Monitoring DB health check failed: {e}")
        return False
