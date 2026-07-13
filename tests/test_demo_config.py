import importlib
import os


def _reload_config():
    import config
    importlib.reload(config)
    return config


def test_demo_config_resolves_grandline_primary(monkeypatch):
    monkeypatch.setenv("SEEQL_CONFIG", "config/settings.demo.yaml")
    cfg = _reload_config()
    loaded = cfg.load_config()
    assert loaded["monitoring_db"]["path"] == "data/grandline_demo.db"

    # reset the cached registry so it re-reads the demo config
    from config import server_registry
    prev_registry = server_registry._registry
    try:
        server_registry._registry = None
        reg = server_registry.get_server_registry()
        assert reg.get_default_server_id() == "grandline-prod"
    finally:
        server_registry._registry = prev_registry
