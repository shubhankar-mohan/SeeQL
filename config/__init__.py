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
        1. Environment variables (via ${VAR} substitution)
        2. settings.local.yaml (git-ignored, machine-specific)
        3. settings.{SEEQL_ENV}.yaml (environment-specific)
        4. settings.yaml (checked-in defaults)
    """
    # Load .env file from project root
    if load_dotenv is not None:
        dotenv_path = _PROJECT_ROOT / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path)

    if not _DEFAULT_CONFIG.exists():
        raise FileNotFoundError(f"Default config not found: {_DEFAULT_CONFIG}")

    with open(_DEFAULT_CONFIG) as f:
        config = yaml.safe_load(f)

    # Environment-specific config (e.g. settings.dev.yaml, settings.test.yaml)
    env_name = os.environ.get("SEEQL_ENV", "production")
    env_config_path = _CONFIG_DIR / f"settings.{env_name}.yaml"
    config = _load_yaml_if_exists(env_config_path, config)

    # Local overrides — check both config/ dir and project root
    config = _load_yaml_if_exists(_CONFIG_DIR / "settings.local.yaml", config)
    config = _load_yaml_if_exists(_PROJECT_ROOT / "settings.local.yaml", config)

    config = _substitute_env_vars(config)

    # Apply SEEQL_* env var overrides (for Docker runtime config)
    _apply_env_overrides(config)

    return config


def _apply_env_overrides(config: dict):
    """Override config values from SEEQL_* and PROD_DB_* environment variables.

    This allows passing all config at docker run time without needing
    settings.local.yaml or .env files inside the container.
    """
    _env = os.environ.get

    # Production DB
    if _env("PROD_DB_HOST"):
        config.setdefault("production_db", {})["host"] = _env("PROD_DB_HOST")
    if _env("PROD_DB_PORT"):
        config.setdefault("production_db", {})["port"] = int(_env("PROD_DB_PORT"))
    if _env("PROD_DB_USER"):
        config.setdefault("production_db", {})["user"] = _env("PROD_DB_USER")
    if _env("PROD_DB_PASSWORD"):
        config.setdefault("production_db", {})["password"] = _env("PROD_DB_PASSWORD")
    if _env("PROD_DB_DATABASE"):
        config.setdefault("production_db", {})["database"] = _env("PROD_DB_DATABASE")

    # GCP
    if _env("GCP_PROJECT_ID"):
        config.setdefault("gcp", {})["project_id"] = _env("GCP_PROJECT_ID")
    if _env("GCP_REGION"):
        config.setdefault("gcp", {})["region"] = _env("GCP_REGION")
    if _env("GCP_CLOUD_SQL_INSTANCE"):
        config.setdefault("gcp", {})["cloud_sql_instance_id"] = _env("GCP_CLOUD_SQL_INSTANCE")
    if _env("MONITORING_APPLICATION_CREDENTIALS"):
        config.setdefault("gcp", {})["monitoring_credentials_file"] = _env("MONITORING_APPLICATION_CREDENTIALS")

    # Collection intervals
    if _env("SEEQL_FAST_INTERVAL"):
        config.setdefault("intervals", {})["fast_loop"] = int(_env("SEEQL_FAST_INTERVAL"))
    if _env("SEEQL_MEDIUM_INTERVAL"):
        config.setdefault("intervals", {})["medium_loop"] = int(_env("SEEQL_MEDIUM_INTERVAL"))
    if _env("SEEQL_SLOW_INTERVAL"):
        config.setdefault("intervals", {})["slow_loop"] = int(_env("SEEQL_SLOW_INTERVAL"))

    # Size limits
    if _env("SEEQL_DB_MAX_SIZE_MB"):
        config.setdefault("monitoring_db", {})["max_size_mb"] = int(_env("SEEQL_DB_MAX_SIZE_MB"))
    if _env("SEEQL_LOG_MAX_SIZE_MB"):
        config.setdefault("logging", {})["max_total_mb"] = int(_env("SEEQL_LOG_MAX_SIZE_MB"))
    if _env("SEEQL_RETENTION_DAYS"):
        config.setdefault("retention", {})["days"] = int(_env("SEEQL_RETENTION_DAYS"))

    # Agent
    if _env("SEEQL_AGENT_ENABLED"):
        config.setdefault("agent", {})["enabled"] = _env("SEEQL_AGENT_ENABLED").lower() == "true"
    if _env("SEEQL_AGENT_MODEL"):
        config.setdefault("agent", {})["model"] = _env("SEEQL_AGENT_MODEL")

    # Alerting
    if _env("SEEQL_ALERTING_ENABLED"):
        config.setdefault("alerting", {})["enabled"] = _env("SEEQL_ALERTING_ENABLED").lower() == "true"
    if _env("SLACK_WEBHOOK_URL") and not _env("SLACK_WEBHOOK_URL", "").startswith("${"):
        alerting = config.setdefault("alerting", {})
        channels = alerting.setdefault("channels", {})
        slack = channels.setdefault("slack", {})
        slack["enabled"] = True
        slack["webhook_url"] = _env("SLACK_WEBHOOK_URL")

    # Prometheus
    if _env("SEEQL_PROM_CACHE_TTL"):
        config.setdefault("prometheus", {})["cache_ttl_seconds"] = int(_env("SEEQL_PROM_CACHE_TTL"))

    # Logging
    if _env("SEEQL_LOG_LEVEL"):
        config.setdefault("logging", {})["level"] = _env("SEEQL_LOG_LEVEL")


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
