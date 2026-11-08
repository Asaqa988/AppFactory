"""Syntax and import checks for every module in the package."""
import importlib
import pkgutil

import app


def test_every_module_imports():
    failures = []
    for module in pkgutil.walk_packages(app.__path__, prefix="app."):
        try:
            importlib.import_module(module.name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{module.name}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


def test_openapi_schema_builds_and_lists_required_endpoints():
    from app.main import app as fastapi_app
    paths = fastapi_app.openapi()["paths"]
    expected = {
        ("/api/v1/research-runs", "post"), ("/api/v1/research-runs", "get"), ("/api/v1/research-runs/{run_id}", "get"),
        ("/api/v1/opportunities", "get"), ("/api/v1/opportunities/{opportunity_id}", "get"),
        ("/api/v1/opportunities/{opportunity_id}/decision", "post"), ("/api/v1/approvals", "get"), ("/health", "get"),
    }
    actual = {(p, m) for p, ops in paths.items() for m in ops}
    assert expected <= actual
