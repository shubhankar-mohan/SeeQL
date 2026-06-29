"""
Configuration loader for MySQL DBA Agent.

Reads settings.yaml (defaults), environment-specific overrides, and
optionally overlays settings.local.yaml.
Supports environment variable substitution for secrets: ${VAR_NAME} in YAML
values will be replaced with the corresponding environment variable.
"""

import os
import re
import yaml
import logging
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent
_PROJECT_ROOT = _CONFIG_DIR.parent
_DEFAULT_CONFIG = _CONFIG_DIR / "settings.yaml"
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _substitute_env_vars(value: Any) -> Any:
    """
    Recursively walk a config tree and replace ${VAR} placeholders
    with actual environment variable values.

    If an env var is not set, the placeholder is left as-is and a
    warning is logged — lets the app fail explicitly at connection
    time rather than silently at config load time.
    """
    if isinstance(value, str):
        def _replace(match):
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                logger.warning(f"Environment variable '{var_name}' not set, placeholder kept.")
                return match.group(0)
            return env_value
        return _ENV_VAR_PATTERN.sub(_replace, value)

    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]

    return value


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into `base`. Override wins for leaf values."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml_if_exists(path: Path, config: dict) -> dict:
    """Load a YAML file and deep-merge into config if it exists."""
    if path.exists():
        logger.info(f"Loading config override: {path}")
        with open(path) as f:
            override = yaml.safe_load(f)
            if override:
                config = _deep_merge(config, override)
    return config


def load_config() -> dict:
    """
    Load configuration with this priority (highest wins):
        1. ${VAR} substitution from the environment (for secrets)
        2. User config file: --config / SEEQL_CONFIG / /etc/seeql/seeql.yml,
           or the legacy settings.local.yaml (deep-merged)
        3. settings.{SEEQL_ENV}.yaml (dev/test internals)
        4. settings.yaml (built-in operational defaults, baked in the image)

    Connections and the list of monitored servers live in the user config file
    (see seeql.example.yml) — there are NO PROD_DB_* / SEEQL_SERVER_* env
    overrides. Inject secrets into the file via ${VAR} placeholders.
    """
    # Load .env (secrets) from project root so ${VAR} placeholders resolve.
    if load_dotenv is not None:
        dotenv_path = _PROJECT_ROOT / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path)

    if not _DEFAULT_CONFIG.exists():
        raise FileNotFoundError(f"Default config not found: {_DEFAULT_CONFIG}")

    with open(_DEFAULT_CONFIG) as f:
        config = yaml.safe_load(f) or {}

    # Environment-specific internals (settings.dev.yaml, settings.test.yaml)
    env_name = os.environ.get("SEEQL_ENV", "production")
    config = _load_yaml_if_exists(_CONFIG_DIR / f"settings.{env_name}.yaml", config)

    # User config file — the canonical place to configure servers/agent/etc.
    user_path = _resolve_user_config_path()
    if user_path is not None:
        config = _load_yaml_if_exists(user_path, config)

    config = _substitute_env_vars(config)
    _apply_operational_env_overrides(config)
    return config


def _resolve_user_config_path():
    """Resolve the user config file path (returns a Path or None).

    Priority: ``SEEQL_CONFIG`` (set directly or via ``--config``) → the default
    container path ``/etc/seeql/seeql.yml`` → the legacy ``settings.local.yaml``
    (config/ then project root). Returns None when nothing is found — the agent
    then runs on built-in defaults only (no DB configured; ``seeql doctor``
    flags this).
    """
    explicit = os.environ.get("SEEQL_CONFIG")
    if explicit:
        p = Path(explicit)
        if not p.exists():
            logger.warning(f"SEEQL_CONFIG points to a missing file: {p}")
        return p
    default = Path("/etc/seeql/seeql.yml")
    if default.exists():
        return default
    for legacy in (_CONFIG_DIR / "settings.local.yaml", _PROJECT_ROOT / "settings.local.yaml"):
        if legacy.exists():
            return legacy
    return None


def _apply_operational_env_overrides(config: dict):
    """Apply the handful of *operational* env vars that behave like CLI flags
    (data path, storage caps, log level) — analogous to Prometheus's
    ``--storage.tsdb.*`` / ``--log.level`` flags.

    Connection and server CONFIG is intentionally NOT settable via env — it
    lives in the config file (see seeql.example.yml). Secrets reach the file via
    ${VAR} substitution.
    """
    _env = os.environ.get
    if _env("SEEQL_MON_DB_PATH"):
        config.setdefault("monitoring_db", {})["path"] = _env("SEEQL_MON_DB_PATH")
    if _env("SEEQL_DB_MAX_SIZE_MB"):
        config.setdefault("monitoring_db", {})["max_size_mb"] = int(_env("SEEQL_DB_MAX_SIZE_MB"))
    if _env("SEEQL_LOG_MAX_SIZE_MB"):
        config.setdefault("logging", {})["max_total_mb"] = int(_env("SEEQL_LOG_MAX_SIZE_MB"))
    if _env("SEEQL_RETENTION_DAYS"):
        config.setdefault("retention", {})["days"] = int(_env("SEEQL_RETENTION_DAYS"))
    if _env("SEEQL_LOG_LEVEL"):
        config.setdefault("logging", {})["level"] = _env("SEEQL_LOG_LEVEL")
    if _env("SEEQL_PROM_CACHE_TTL"):
        config.setdefault("prometheus", {})["cache_ttl_seconds"] = int(_env("SEEQL_PROM_CACHE_TTL"))


# ---------------------------------------------------------------------------
# Global singleton + convenience accessors
# ---------------------------------------------------------------------------

_config = None


def get_config() -> dict:
    """Get or lazily load the global config singleton."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_prod_db_config() -> dict:
    """Connection params for the production MySQL (Cloud SQL).

    DEPRECATED for multi-server use — use ServerRegistry instead.
    Kept for backward compatibility with code that doesn't use ServerContext.
    """
    return get_config().get("production_db", {})


def get_mon_db_config() -> dict:
    """SQLite config for the monitoring database."""
    return get_config()["monitoring_db"]


def get_intervals() -> dict:
    """Collection intervals in seconds."""
    return get_config()["intervals"]


def get_limits() -> dict:
    """Collection limits (top N queries, max lengths, etc.)."""
    return get_config()["limits"]


def get_excluded_schemas() -> list[str]:
    """Schemas to exclude from monitoring queries."""
    return get_config()["excluded_schemas"]


def get_excluded_schemas_sql() -> str:
    """
    Returns a SQL-safe comma-separated string for use in IN clauses.
    Example: "'mysql', 'performance_schema', 'sys', 'information_schema'"
    """
    schemas = get_excluded_schemas()
    return ", ".join(f"'{s}'" for s in schemas)
