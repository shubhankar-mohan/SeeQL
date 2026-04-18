"""Tests for config module."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import config as config_module
from config import (
    _deep_merge,
    _substitute_env_vars,
    get_config,
    get_excluded_schemas,
    get_excluded_schemas_sql,
    get_intervals,
    get_limits,
    get_mon_db_config,
    get_prod_db_config,
    load_config,
)


class TestSubstituteEnvVars:
    def test_replaces_env_var(self):
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert _substitute_env_vars("${MY_VAR}") == "hello"

    def test_keeps_placeholder_if_not_set(self):
        result = _substitute_env_vars("${NONEXISTENT_VAR_XYZ}")
        assert result == "${NONEXISTENT_VAR_XYZ}"

    def test_handles_dict(self):
        with patch.dict(os.environ, {"DB_PASS": "secret"}):
            result = _substitute_env_vars({"password": "${DB_PASS}", "host": "localhost"})
            assert result == {"password": "secret", "host": "localhost"}

    def test_handles_list(self):
        with patch.dict(os.environ, {"ITEM": "value"}):
            result = _substitute_env_vars(["${ITEM}", "static"])
            assert result == ["value", "static"]

    def test_handles_non_string(self):
        assert _substitute_env_vars(42) == 42
        assert _substitute_env_vars(None) is None
        assert _substitute_env_vars(True) is True


class TestDeepMerge:
    def test_simple_merge(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"db": {"host": "prod", "port": 3306}}
        override = {"db": {"host": "dev"}}
        result = _deep_merge(base, override)
        assert result == {"db": {"host": "dev", "port": 3306}}

    def test_override_dict_with_scalar(self):
        base = {"a": {"nested": True}}
        override = {"a": "flat"}
        result = _deep_merge(base, override)
        assert result == {"a": "flat"}

    def test_does_not_mutate_base(self):
        base = {"a": 1}
        _deep_merge(base, {"a": 2})
        assert base == {"a": 1}


class TestLoadConfig:
    def test_loads_default_config(self):
        config = load_config()
        assert "production_db" in config
        assert "monitoring_db" in config
        assert "intervals" in config

    def test_env_specific_config(self):
        """Dev config overrides defaults; local config is suppressed."""
        from config import _load_yaml_if_exists as _real_load

        def _no_local(path, config):
            if "local" in path.name:
                return config
            return _real_load(path, config)

        with patch.dict(os.environ, {"SEEQL_ENV": "dev"}):
            with patch("config._load_yaml_if_exists", side_effect=_no_local):
                config_module._config = None
                config = load_config()
                assert config["production_db"]["host"] == "127.0.0.1"
                assert config["production_db"]["port"] == 3307

    def test_test_env_config(self):
        """Test config overrides defaults; local config is suppressed."""
        from config import _load_yaml_if_exists as _real_load

        def _no_local(path, config):
            if "local" in path.name:
                return config
            return _real_load(path, config)

        with patch.dict(os.environ, {"SEEQL_ENV": "test"}):
            with patch("config._load_yaml_if_exists", side_effect=_no_local):
                config_module._config = None
                config = load_config()
                assert config["monitoring_db"]["path"] == ":memory:"
                assert config["intervals"]["fast_loop"] == 1


class TestAccessors:
    def test_get_config_singleton(self):
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2

    def test_get_prod_db_config(self):
        config = get_prod_db_config()
        assert "host" in config
        assert "user" in config

    def test_get_mon_db_config(self):
        config = get_mon_db_config()
        assert "path" in config

    def test_get_intervals(self):
        intervals = get_intervals()
        assert "fast_loop" in intervals
        assert "medium_loop" in intervals
        assert "slow_loop" in intervals

    def test_get_limits(self):
        limits = get_limits()
        assert "top_queries" in limits

    def test_get_excluded_schemas(self):
        schemas = get_excluded_schemas()
        assert "mysql" in schemas
        assert "performance_schema" in schemas

    def test_get_excluded_schemas_sql(self):
        sql = get_excluded_schemas_sql()
        assert "'mysql'" in sql
        assert "'performance_schema'" in sql
