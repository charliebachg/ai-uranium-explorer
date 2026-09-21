"""Tracking is a configuration: without the mlflow extra a run is still a finished run, and the tracker call
returns nothing rather than failing the job that made it (found by a Compose user's analyst job)."""

from __future__ import annotations

import sys

from uranium_explorer.prospect import tracking as TR


def test_log_run_returns_none_when_mlflow_is_not_installed(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "mlflow", None)   # `import mlflow` now raises ModuleNotFoundError
    assert TR.log_run("arm-x", {"arm": "v1"}, {"pr_auc": 0.5}, tags={"kind": "test"}) is None


def test_other_missing_modules_still_raise(monkeypatch) -> None:
    import pytest

    monkeypatch.setattr(TR, "_mlflow", lambda: (_ for _ in ()).throw(ModuleNotFoundError("x", name="numpy_extra")))
    with pytest.raises(ModuleNotFoundError):
        TR.log_run("arm-x", {}, {})
