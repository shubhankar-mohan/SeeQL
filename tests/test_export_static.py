import importlib.util


def _load():
    spec = importlib.util.spec_from_file_location("export_static", "scripts/export_static.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_url_and_path_planning():
    m = _load()
    # pages map to clean index.html paths
    assert m.local_path("/dashboard") == "dashboard/index.html"
    assert m.local_path("/dashboard/queries") == "dashboard/queries/index.html"
    # api json path keeps a .json extension, query string dropped
    assert m.local_path("/api/v1/metrics/qps?range=1h") == "api/v1/metrics/qps.json"
    # partial path preserved for HTMX swap
    assert m.local_path("/dashboard/partials/health-bar") == "dashboard/partials/health-bar"
    # per-digest urls are generated
    urls = m.api_urls(["7107e33a"])
    assert any("/queries/7107e33a/trend" in u for u in urls)
