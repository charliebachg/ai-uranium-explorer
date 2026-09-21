"""Experiment tracking: every fitted model and every reported number lands in MLflow, with a run id that the
store's metric rows and the Eval page carry. Nothing is reported that is not in the tracker.

The tracking store is a local SQLite file under data/, which also supports the model registry, so "the served
model" is a registry stage and not a file someone remembers to copy. The promotion rule is applied here and
nowhere else: a candidate is *validated* only if it beats the exploration-effort null under spatial folds with
non-overlapping intervals, and it is *served* only if it is validated. Today nothing is.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np

from ..paths import PATHS

EXPERIMENT = "prospect"
REGISTERED = "learned-prospectivity"
#: where the evaluation runs leave the JSON the web export reads; tests point it at a temporary directory
OUT_DIR = PATHS.data / "out" / "prospect"


def tracking_uri() -> str:
    env = os.environ.get("UE_MLFLOW_URI", "").strip()
    if env:
        return env
    return f"sqlite:///{PATHS.data / 'mlflow.db'}"


def artifact_dir() -> Path:
    p = PATHS.data / "mlruns"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _mlflow():
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import mlflow

    mlflow.set_tracking_uri(tracking_uri())
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        # artefacts beside the tracking database, not in whatever directory the command ran from
        mlflow.create_experiment(EXPERIMENT, artifact_location=str(artifact_dir()))
    mlflow.set_experiment(EXPERIMENT)
    return mlflow


def jsonable(obj: Any) -> Any:
    """The object as json.dumps takes it: numpy scalars as Python ones, NaN and infinities as null, tuples as lists."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, np.ndarray)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if obj is None or isinstance(obj, str):
        return obj
    return str(obj)


def write_json(name: str, payload: Any) -> Path:
    """One file under OUT_DIR, the JSON the web export reads. Returns its path."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(json.dumps(jsonable(payload), indent=1) + "\n")
    return path


def log_run(name: str, params: dict[str, Any], metrics: dict[str, float], tags: dict[str, str | None] | None = None,
            artifacts: dict[str, Any] | None = None) -> str:
    """One MLflow run: flat params, numeric metrics, JSON artefacts. Returns the run id.

    A tag whose value is None is not set, so a run on a store no snapshot names carries no `snapshot` tag
    rather than the string "None"."""
    try:
        mlflow = _mlflow()
    except ModuleNotFoundError as err:
        # the mlflow extra is optional (a container, a slim clone): the run directory and its manifest stay the
        # record, and a run that cannot be mirrored to the tracker is still a finished run, not a failed one
        if err.name != "mlflow":
            raise
        return None
    with mlflow.start_run(run_name=name) as run:
        mlflow.log_params({k: (v if isinstance(v, (int, float, str, bool)) else json.dumps(v)) for k, v in params.items()})
        mlflow.log_metrics({k: float(v) for k, v in metrics.items() if v is not None and v == v})
        set_tags = {k: str(v) for k, v in (tags or {}).items() if v is not None}
        if set_tags:
            mlflow.set_tags(set_tags)
        for fname, payload in (artifacts or {}).items():
            path = artifact_dir() / f"{run.info.run_id}-{fname}"
            path.write_text(json.dumps(jsonable(payload), indent=1))
            mlflow.log_artifact(str(path))
            path.unlink(missing_ok=True)
        return run.info.run_id


def beats_null(candidate: dict[str, Any], null: dict[str, Any]) -> bool:
    """The promotion rule: the candidate's PR-AUC interval lies wholly above the effort null's, spatial folds."""
    lo_c, _ = candidate.get("pr_auc_ci", (float("nan"), float("nan")))
    _, hi_n = null.get("pr_auc_ci", (float("nan"), float("nan")))
    return lo_c == lo_c and hi_n == hi_n and lo_c > hi_n


def record_decision(run_id: str, model_name: str, validated: bool, reason: str,
                    store_sha256: str | None = None) -> dict[str, Any]:
    """Register the run's model in the registry with the stage the rule allows, and write the decision down,
    naming the store it was decided on."""
    mlflow = _mlflow()
    client = mlflow.MlflowClient()
    try:
        client.get_registered_model(REGISTERED)
    except Exception:  # noqa: BLE001 - first time
        client.create_registered_model(REGISTERED, description="The learned prospectivity model, if any is served. "
                                       "Promotion requires beating the exploration-effort null under spatial folds.")
    version = client.create_model_version(REGISTERED, source=f"runs:/{run_id}/model", run_id=run_id,
                                          description=f"{model_name}: {reason}")
    stage = "validated" if validated else "candidate"
    client.set_model_version_tag(REGISTERED, version.version, "stage", stage)
    client.set_model_version_tag(REGISTERED, version.version, "served", "false")
    if store_sha256:
        client.set_model_version_tag(REGISTERED, version.version, "store_sha256", store_sha256)
    decision = {"registered_model": REGISTERED, "version": int(version.version), "run_id": run_id, "model": model_name,
                "stage": stage, "served": False, "reason": reason, "store_sha256": store_sha256}
    write_json("registry.json", decision)
    return decision


def served_model() -> dict[str, Any] | None:
    """What is served, from the written decision: None means the dashboard shows no learned predictor as such."""
    p = OUT_DIR / "registry.json"
    if not p.is_file():
        return None
    d = json.loads(p.read_text())
    return d if d.get("served") else None
