"""
Tools available to the LLM DBA Agent.

Two categories:
    1. Snapshot tools — read pre-collected data from monitoring SQLite DB.
    2. Live tools — query production MySQL directly (read-only, with timeouts).

Tools are defined as Anthropic tool_use schemas and have matching execution
functions. The Gemini conversion happens in llm_agent.py.
"""

import contextvars
import json
import logging
import time

from agent import queries as Q
from storage.connection import get_mon_reader, get_prod_connection

logger = logging.getLogger(__name__)

# Timeout for live production queries (seconds)
_LIVE_QUERY_TIMEOUT = 10

# Retry config for live tools (collectors have retry, tools should too)
_LIVE_TOOL_MAX_RETRIES = 2
_LIVE_TOOL_RETRY_DELAY = 0.5

# Per-context server + budget for live tool calls.
#
# These MUST be ContextVars, not module globals: investigations run in a
# ThreadPoolExecutor (multiple concurrent) and the MCP server runs concurrent
# async tasks. A plain global would let one investigation overwrite or clear
# another's target server / budget mid-flight. A ContextVar gives each thread
# its own context (threads start with the default) and each asyncio task its
# own copy, so set()/get()/reset within one context never leak across others.
_current_server_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "seeql_current_server_id", default=None
)

# Current tool budget (set by run_llm_analysis when invoked from the webhook
# investigator). When set, execute_tool rejects calls that the budget disallows
# by returning a rejection message the LLM sees — same shape as existing
# tool errors, so the model naturally backs off.
_current_budget: contextvars.ContextVar = contextvars.ContextVar(
    "seeql_current_budget", default=None
)


def set_current_server(server_id: str):
    """Set which server live tools should connect to (for this context)."""
    _current_server_id.set(server_id)


def get_current_server() -> str | None:
    """Return the server live tools should connect to for this context."""
    return _current_server_id.get()


def set_current_budget(budget) -> None:
    """Set the active tool budget for this context. Pass None to clear."""
    _current_budget.set(budget)


def get_current_budget():
    """Return the active tool budget for this context (or None)."""
    return _current_budget.get()


# --- Tool Definitions (Anthropic tool_use format) ---

TOOL_DEFINITIONS = [
    # ---------------------------------------------------------------
    # Snapshot tools (read from monitoring SQLite)
    # ---------------------------------------------------------------
    {
        "name": "run_explain",
        "description": (
            "Get the EXPLAIN JSON plan for a query digest. First checks the "
            "explain_captures table for a recent capture. If none exists, runs "
            "EXPLAIN FORMAT=JSON against production (SELECT/WITH queries only). "
            "Use this for EVERY slow or regressed query to understand its "
            "execution plan before making recommendations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "digest": {
                    "type": "string",
                    "description": "The query digest hash to explain",
                },
            },
            "required": ["digest"],
        },
    },
    {
        "name": "get_table_schema",
        "description": (
            "Get the full CREATE TABLE statement for a table, including "
            "columns, indexes, constraints, and engine info. Use this to "
            "understand index coverage before recommending new indexes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_name": {
                    "type": "string",
                    "description": "Database/schema name",
                },
                "table_name": {
                    "type": "string",
                    "description": "Table name",
                },
            },
            "required": ["schema_name", "table_name"],
        },
    },
    {
        "name": "get_query_history",
        "description": (
            "Get performance history for a query digest over time. Returns "
            "avg_time_sec, exec_count, rows_examined trend data. Use this to "
            "understand WHEN a query started degrading and correlate with "
            "DDL changes or traffic patterns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "digest": {
                    "type": "string",
                    "description": "The query digest hash",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days of history (default 7)",
                    "default": 7,
                },
            },
            "required": ["digest"],
        },
    },
    {
        "name": "get_lock_graph",
        "description": (
            "Get the most recent lock wait graph from monitoring snapshots, "
            "showing which transactions are blocking which. Use this as a "
            "first look; follow up with get_live_locks for real-time data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ---------------------------------------------------------------
    # Live tools (query production MySQL directly, read-only)
    # ---------------------------------------------------------------
    {
        "name": "get_live_processlist",
        "description": (
            "Get the LIVE active processlist from production MySQL right now. "
            "Shows all non-sleeping threads with their current query, user, "
            "database, time running, and state. Use this to see what's "
            "happening on the server RIGHT NOW — not a snapshot from minutes ago."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_live_locks",
        "description": (
            "Get LIVE lock waits from production MySQL right now. Queries "
            "performance_schema.data_lock_waits joined with innodb_trx to "
            "show waiting and blocking transactions, queries, and wait times. "
            "Use this during active lock contention to see the real-time "
            "blocker chain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_live_innodb_status",
        "description": (
            "Get LIVE SHOW ENGINE INNODB STATUS output from production MySQL. "
            "Contains detailed info about deadlocks (latest detected deadlock), "
            "semaphore waits, buffer pool state, row operations, and active "
            "transactions. Use this when investigating deadlocks, semaphore "
            "contention, or InnoDB internals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_live_transactions",
        "description": (
            "Get LIVE active InnoDB transactions from production MySQL. Shows "
            "transaction state, age, rows locked/modified, isolation level, "
            "and current query. Use this to identify long-running transactions "
            "that may be holding locks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_index_stats",
        "description": (
            "Get index usage statistics for a specific table from production "
            "MySQL. Shows each index with read/write counts from "
            "performance_schema, plus the index columns and cardinality from "
            "information_schema. Use this to identify unused or underused "
            "indexes and to validate whether a proposed new index duplicates "
            "an existing one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_name": {
                    "type": "string",
                    "description": "Database/schema name",
                },
                "table_name": {
                    "type": "string",
                    "description": "Table name",
                },
            },
            "required": ["schema_name", "table_name"],
        },
    },
    {
        "name": "get_table_status",
        "description": (
            "Get LIVE table status for a specific table from production MySQL. "
            "Shows row count estimate, data size, index size, auto increment "
            "value, row format, and fragmentation. Use this to understand "
            "table size and whether maintenance (OPTIMIZE TABLE) may help."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schema_name": {
                    "type": "string",
                    "description": "Database/schema name",
                },
                "table_name": {
                    "type": "string",
                    "description": "Table name",
                },
            },
            "required": ["schema_name", "table_name"],
        },
    },
    {
        "name": "explain_query",
        "description": (
            "Run EXPLAIN FORMAT=JSON for an arbitrary SELECT query against "
            "production MySQL. Unlike run_explain (which works with digest "
            "hashes), this accepts raw SQL. Only SELECT and WITH (CTE) "
            "queries are allowed. Use this ONLY to test a rewritten query "
            "against the original plan. For normal queries, use run_explain "
            "first (it checks the cache). Keep test queries simple — the "
            "10-second timeout will kill expensive ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The SELECT query to EXPLAIN (must start with SELECT or WITH)",
                },
                "schema_name": {
                    "type": "string",
                    "description": "Database/schema to USE before running EXPLAIN (optional)",
                },
            },
            "required": ["query"],
        },
    },

    # ---------------------------------------------------------------
    # Slow log tool (real SQL with actual values)
    # ---------------------------------------------------------------
    {
        "name": "search_slow_log",
        "description": (
            "Search the slow query log for queries matching a keyword or table name. "
            "Unlike query digests (which have parameterized ? placeholders), slow log "
            "entries contain REAL SQL with actual values — useful for seeing exact "
            "WHERE clause values, understanding query patterns, and identifying which "
            "users/hosts generate problematic queries. Returns the slowest matching "
            "entries with timing, user, host, and full SQL text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Search term (table name, column name, or SQL fragment)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10)",
                    "default": 10,
                },
            },
            "required": ["keyword"],
        },
    },

    # ---------------------------------------------------------------
    # Memory tool (read agent's own prior analyses)
    # ---------------------------------------------------------------
    {
        "name": "get_recent_analyses",
        "description": (
            "Get your own recent analysis results — findings and recommendations "
            "from previous runs. Use this to avoid repeating the same recommendation "
            "and to check if prior advice was acted on. Returns the last N analyses "
            "within the given time window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Look back this many hours (default 24)",
                    "default": 24,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of analyses to return (default 5)",
                    "default": 5,
                },
            },
        },
    },
]


# --- Tool Execution Functions ---

def execute_tool(name: str, input_data: dict) -> str:
    """Execute a tool by name and return the result as a string."""
    handlers = {
        # Snapshot tools
        "run_explain": _tool_run_explain,
        "get_table_schema": _tool_get_table_schema,
        "get_query_history": _tool_get_query_history,
        "get_lock_graph": _tool_get_lock_graph,
        # Live tools
        "get_live_processlist": _tool_get_live_processlist,
        "get_live_locks": _tool_get_live_locks,
        "get_live_innodb_status": _tool_get_live_innodb_status,
        "get_live_transactions": _tool_get_live_transactions,
        "get_index_stats": _tool_get_index_stats,
        "get_table_status": _tool_get_table_status,
        "explain_query": _tool_explain_query,
        # Slow log tool
        "search_slow_log": _tool_search_slow_log,
        # Memory tool
        "get_recent_analyses": _tool_get_recent_analyses,
    }
    handler = handlers.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})

    # Budget gate (webhook investigator only; no-op when no budget is set).
    budget = _current_budget.get()
    if budget is not None and not budget.can_call(name):
        msg = budget.rejection_message(name)
        logger.info(f"Tool {name} rejected by budget: {msg}")
        return json.dumps({"error": msg, "budget_rejected": True})

    try:
        result = handler(input_data)
        if budget is not None:
            try:
                budget.record(name)
            except Exception:
                logger.debug(f"budget.record({name}) failed; continuing")
        return json.dumps(result, default=str)
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return json.dumps({"error": str(e)})


# --- Snapshot tool implementations ---

def _tool_run_explain(input_data: dict) -> dict:
    digest = input_data["digest"]

    # Check for recent cached EXPLAIN
    with get_mon_reader() as conn:
        row = conn.execute(Q.EXPLAIN_FOR_DIGEST, (digest,)).fetchone()
        if row and row["explain_json"]:
            return {
                "source": "cached",
                "captured_at": row["captured_at"],
                "explain": json.loads(row["explain_json"]),
            }

        # Get query text — prefer query_sample_text (real SQL) over digest_text (parameterized)
        digest_row = conn.execute(
            "SELECT digest_text, query_sample_text, schema_name FROM query_digest_snapshots "
            "WHERE digest = ? ORDER BY snapshot_time DESC LIMIT 1",
            (digest,)
        ).fetchone()

    if not digest_row:
        return {"error": f"No digest found for {digest}"}

    # Use sample text (real values) if available, fall back to digest text
    sql_text = digest_row["query_sample_text"] or digest_row["digest_text"]
    schema = digest_row["schema_name"]

    if not sql_text:
        return {"error": f"No SQL text available for {digest}"}

    # Only EXPLAIN SELECT queries
    if not sql_text.strip().upper().startswith(("SELECT", "WITH")):
        return {"error": f"Cannot EXPLAIN non-SELECT query: {sql_text[:50]}"}

    try:
        with get_prod_connection(_current_server_id.get()) as conn:
            cursor = conn.cursor(dictionary=True)
            if schema:
                cursor.execute(f"USE `{schema}`")
            cursor.execute(f"EXPLAIN FORMAT=JSON {sql_text}")
            result = cursor.fetchone()
            if result:
                explain_json = result.get("EXPLAIN", "")
                return {
                    "source": "live",
                    "explain": json.loads(explain_json),
                }
    except Exception as e:
        return {"error": f"EXPLAIN failed: {e}"}

    return {"error": "No EXPLAIN result"}


def _tool_get_table_schema(input_data: dict) -> dict:
    schema_name = input_data["schema_name"]
    table_name = input_data["table_name"]

    # Try monitoring DB first
    with get_mon_reader() as conn:
        row = conn.execute(Q.SCHEMA_FOR_TABLE, (schema_name, table_name)).fetchone()
        if row and row["create_stmt"]:
            return {"source": "snapshot", "create_statement": row["create_stmt"]}

    # Fall back to production
    try:
        with get_prod_connection(_current_server_id.get()) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SHOW CREATE TABLE `{schema_name}`.`{table_name}`")
            result = cursor.fetchone()
            if result:
                return {"source": "live", "create_statement": result[1]}
    except Exception as e:
        return {"error": f"Failed to get schema: {e}"}

    return {"error": f"Table {schema_name}.{table_name} not found"}


def _tool_get_query_history(input_data: dict) -> dict:
    digest = input_data["digest"]
    days = input_data.get("days", 7)

    with get_mon_reader() as conn:
        rows = conn.execute(Q.QUERY_HISTORY, (digest, f"-{days} days")).fetchall()
        history = [dict(r) for r in rows]

        # Also get latest EXPLAIN if available
        explain_row = conn.execute(Q.EXPLAIN_FOR_DIGEST, (digest,)).fetchone()
        explain = None
        if explain_row and explain_row["explain_json"]:
            try:
                explain = json.loads(explain_row["explain_json"])
            except json.JSONDecodeError:
                pass

    return {
        "data_points": len(history),
        "history": history,
        "latest_explain": explain,
    }


def _tool_get_lock_graph(input_data: dict) -> dict:
    with get_mon_reader() as conn:
        lock_rows = conn.execute(Q.LOCK_GRAPH).fetchall()
        txn_rows = conn.execute(Q.ACTIVE_TRANSACTIONS).fetchall()

    return {
        "lock_waits": [dict(r) for r in lock_rows],
        "active_transactions": [dict(r) for r in txn_rows],
    }


# --- Live tool implementations (query production MySQL directly) ---

def _run_live_query(query: str, params: tuple = (), dictionary: bool = True) -> list[dict]:
    """Execute a read-only query against production MySQL with timeout and retry."""
    last_err = None
    for attempt in range(_LIVE_TOOL_MAX_RETRIES + 1):
        try:
            with get_prod_connection(_current_server_id.get()) as conn:
                cursor = conn.cursor(dictionary=dictionary)
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {_LIVE_QUERY_TIMEOUT * 1000}")
                cursor.execute(query, params)
                rows = cursor.fetchall()
                return [dict(r) if dictionary else r for r in rows]
        except Exception as e:
            last_err = e
            if attempt < _LIVE_TOOL_MAX_RETRIES:
                time.sleep(_LIVE_TOOL_RETRY_DELAY * (attempt + 1))
    raise last_err


def _tool_get_live_processlist(input_data: dict) -> dict:
    query = """
        SELECT
            PROCESSLIST_ID AS pid,
            PROCESSLIST_USER AS user,
            PROCESSLIST_DB AS db,
            PROCESSLIST_COMMAND AS command,
            PROCESSLIST_STATE AS state,
            PROCESSLIST_TIME AS time_sec,
            LEFT(PROCESSLIST_INFO, 500) AS query
        FROM performance_schema.threads
        WHERE PROCESSLIST_COMMAND != 'Sleep'
          AND PROCESSLIST_COMMAND != 'Daemon'
          AND PROCESSLIST_INFO IS NOT NULL
          AND TYPE = 'FOREGROUND'
        ORDER BY PROCESSLIST_TIME DESC
        LIMIT 50
    """
    rows = _run_live_query(query)
    return {
        "source": "live",
        "timestamp": _now_iso(),
        "active_threads": len(rows),
        "processes": rows,
    }


def _tool_get_live_locks(input_data: dict) -> dict:
    query = """
        SELECT
            r.trx_id AS waiting_trx_id,
            r.trx_mysql_thread_id AS waiting_pid,
            LEFT(r.trx_query, 500) AS waiting_query,
            TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_seconds,
            b.trx_id AS blocking_trx_id,
            b.trx_mysql_thread_id AS blocking_pid,
            LEFT(b.trx_query, 500) AS blocking_query,
            TIMESTAMPDIFF(SECOND, b.trx_started, NOW()) AS blocking_trx_age_sec,
            b.trx_rows_locked AS blocking_rows_locked,
            b.trx_rows_modified AS blocking_rows_modified
        FROM performance_schema.data_lock_waits w
        JOIN information_schema.innodb_trx r ON r.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID
        JOIN information_schema.innodb_trx b ON b.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
    """
    rows = _run_live_query(query)
    return {
        "source": "live",
        "timestamp": _now_iso(),
        "lock_wait_count": len(rows),
        "lock_waits": rows,
    }


def _tool_get_live_innodb_status(input_data: dict) -> dict:
    with get_prod_connection(_current_server_id.get()) as conn:
        cursor = conn.cursor()
        cursor.execute("SHOW ENGINE INNODB STATUS")
        result = cursor.fetchone()

    if not result:
        return {"error": "No INNODB STATUS output"}

    # result is (Type, Name, Status) — the full text is in Status
    status_text = result[2] if len(result) > 2 else str(result)

    # Parse key sections for easier consumption
    sections = _parse_innodb_sections(status_text)

    return {
        "source": "live",
        "timestamp": _now_iso(),
        "sections": sections,
        "raw_length": len(status_text),
    }


def _tool_get_live_transactions(input_data: dict) -> dict:
    query = """
        SELECT
            trx_id,
            trx_state,
            TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
            trx_mysql_thread_id AS pid,
            LEFT(trx_query, 500) AS trx_query,
            trx_operation_state AS operation_state,
            trx_tables_in_use AS tables_in_use,
            trx_tables_locked AS tables_locked,
            trx_rows_locked AS rows_locked,
            trx_rows_modified AS rows_modified,
            trx_isolation_level AS isolation_level
        FROM information_schema.innodb_trx
        ORDER BY trx_started ASC
    """
    rows = _run_live_query(query)
    return {
        "source": "live",
        "timestamp": _now_iso(),
        "transaction_count": len(rows),
        "transactions": rows,
    }


def _tool_get_index_stats(input_data: dict) -> dict:
    schema_name = input_data["schema_name"]
    table_name = input_data["table_name"]

    # Index usage from performance_schema
    usage_query = """
        SELECT
            INDEX_NAME AS index_name,
            COUNT_READ AS read_count,
            COUNT_WRITE AS write_count,
            COUNT_FETCH AS fetch_count,
            COUNT_INSERT AS insert_count,
            COUNT_UPDATE AS update_count,
            COUNT_DELETE AS delete_count
        FROM performance_schema.table_io_waits_summary_by_index_usage
        WHERE OBJECT_SCHEMA = %s AND OBJECT_NAME = %s
          AND INDEX_NAME IS NOT NULL
        ORDER BY COUNT_READ + COUNT_WRITE DESC
    """

    # Index definition from information_schema
    defn_query = """
        SELECT
            INDEX_NAME AS index_name,
            GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns,
            NON_UNIQUE AS non_unique,
            INDEX_TYPE AS index_type,
            MAX(CARDINALITY) AS cardinality
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        GROUP BY INDEX_NAME, NON_UNIQUE, INDEX_TYPE
        ORDER BY INDEX_NAME
    """

    usage_rows = _run_live_query(usage_query, (schema_name, table_name))
    defn_rows = _run_live_query(defn_query, (schema_name, table_name))

    # Merge usage + definition by index name
    usage_map = {r["index_name"]: r for r in usage_rows}
    indexes = []
    for defn in defn_rows:
        idx_name = defn["index_name"]
        usage = usage_map.get(idx_name, {})
        indexes.append({
            "index_name": idx_name,
            "columns": defn["columns"],
            "unique": not defn["non_unique"],
            "index_type": defn["index_type"],
            "cardinality": defn["cardinality"],
            "read_count": usage.get("read_count", 0),
            "write_count": usage.get("write_count", 0),
        })

    return {
        "source": "live",
        "timestamp": _now_iso(),
        "schema": schema_name,
        "table": table_name,
        "index_count": len(indexes),
        "indexes": indexes,
    }


def _tool_get_table_status(input_data: dict) -> dict:
    schema_name = input_data["schema_name"]
    table_name = input_data["table_name"]

    query = """
        SELECT
            TABLE_ROWS AS row_count,
            ROUND(DATA_LENGTH / 1024 / 1024, 2) AS data_mb,
            ROUND(INDEX_LENGTH / 1024 / 1024, 2) AS index_mb,
            ROUND(DATA_FREE / 1024 / 1024, 2) AS free_mb,
            ENGINE AS engine,
            ROW_FORMAT AS row_format,
            AUTO_INCREMENT AS auto_increment,
            AVG_ROW_LENGTH AS avg_row_length,
            CREATE_TIME AS create_time,
            UPDATE_TIME AS update_time
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
    """
    rows = _run_live_query(query, (schema_name, table_name))
    if not rows:
        return {"error": f"Table {schema_name}.{table_name} not found"}

    result = rows[0]
    # Calculate fragmentation ratio
    data_mb = result.get("data_mb") or 0
    free_mb = result.get("free_mb") or 0
    if data_mb > 0:
        result["fragmentation_pct"] = round(free_mb / (data_mb + free_mb) * 100, 1)
    else:
        result["fragmentation_pct"] = 0

    result["source"] = "live"
    result["timestamp"] = _now_iso()
    return result


def _tool_explain_query(input_data: dict) -> dict:
    query = input_data["query"].strip()
    schema_name = input_data.get("schema_name")

    # Safety: only EXPLAIN SELECT/WITH queries
    upper = query.upper().lstrip()
    if not upper.startswith(("SELECT", "WITH")):
        return {"error": "Only SELECT and WITH (CTE) queries can be EXPLAINed"}

    # Safety: reject if it contains multiple statements
    if ";" in query.rstrip(";"):
        return {"error": "Multiple statements not allowed"}

    try:
        with get_prod_connection(_current_server_id.get()) as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {_LIVE_QUERY_TIMEOUT * 1000}")
            if schema_name:
                cursor.execute(f"USE `{schema_name}`")
            cursor.execute(f"EXPLAIN FORMAT=JSON {query}")
            result = cursor.fetchone()
            if result:
                explain_json = result.get("EXPLAIN", "")
                return {
                    "source": "live",
                    "query": query[:200],
                    "explain": json.loads(explain_json),
                }
    except Exception as e:
        return {"error": f"EXPLAIN failed: {e}"}

    return {"error": "No EXPLAIN result"}


# --- Slow log tool implementation ---

def _tool_search_slow_log(input_data: dict) -> dict:
    keyword = input_data["keyword"]
    limit = input_data.get("limit", 10)

    with get_mon_reader() as conn:
        rows = conn.execute(
            """SELECT snapshot_time, user, host, query_time_sec, lock_time_sec,
                      rows_sent, rows_examined, sql_text
               FROM slow_query_log
               WHERE sql_text LIKE ?
               ORDER BY query_time_sec DESC
               LIMIT ?""",
            (f"%{keyword}%", limit),
        ).fetchall()

    return {
        "match_count": len(rows),
        "keyword": keyword,
        "entries": [
            {
                "time": r["snapshot_time"],
                "user": r["user"],
                "host": r["host"],
                "query_time_sec": r["query_time_sec"],
                "lock_time_sec": r["lock_time_sec"],
                "rows_sent": r["rows_sent"],
                "rows_examined": r["rows_examined"],
                "sql": r["sql_text"][:1000] if r["sql_text"] else None,
            }
            for r in rows
        ],
    }


# --- Memory tool implementation ---

def _tool_get_recent_analyses(input_data: dict) -> dict:
    hours = input_data.get("hours", 24)
    limit = input_data.get("limit", 5)

    with get_mon_reader() as conn:
        rows = conn.execute(
            Q.RECENT_ANALYSES, (f"-{hours} hours", limit)
        ).fetchall()

    analyses = []
    for r in rows:
        entry = {
            "analyzed_at": r["analyzed_at"],
            "analysis_type": r["analysis_type"],
            "severity": r["severity"],
        }
        # Parse stored JSON strings back
        for field in ("findings", "recommendations"):
            raw = r[field]
            if raw:
                try:
                    entry[field] = json.loads(raw)
                except json.JSONDecodeError:
                    entry[field] = raw
            else:
                entry[field] = ""
        analyses.append(entry)

    return {
        "count": len(analyses),
        "hours_back": hours,
        "analyses": analyses,
    }


# --- Helpers ---

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _parse_innodb_sections(text: str) -> dict:
    """Parse SHOW ENGINE INNODB STATUS into named sections."""
    sections = {}
    current_section = None
    current_lines = []

    for line in text.split("\n"):
        # Section headers look like: "---\nSECTION NAME\n---"
        if line.strip().startswith("---"):
            if current_section and current_lines:
                sections[current_section] = "\n".join(current_lines).strip()
            current_lines = []
            continue

        # Detect section name (all caps line after ---)
        if line.strip() and line.strip() == line.strip().upper() and len(line.strip()) > 3:
            candidate = line.strip()
            # Known section names from InnoDB status
            known = [
                "BACKGROUND THREAD", "SEMAPHORES", "LATEST DETECTED DEADLOCK",
                "LATEST FOREIGN KEY ERROR", "TRANSACTIONS", "FILE I/O",
                "INSERT BUFFER AND ADAPTIVE HASH INDEX", "LOG",
                "BUFFER POOL AND MEMORY", "ROW OPERATIONS",
                "INDIVIDUAL BUFFER POOL INFO",
            ]
            if candidate in known:
                if current_section and current_lines:
                    sections[current_section] = "\n".join(current_lines).strip()
                current_section = candidate
                current_lines = []
                continue

        current_lines.append(line)

    if current_section and current_lines:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections
