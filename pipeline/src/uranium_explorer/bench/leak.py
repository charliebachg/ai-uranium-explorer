"""`ue bench leak`: can the passages alone rank the cells? No model call.

A text arm that beats the fitted model is only credited if the text did not simply hand it the label. So before
any arm reads benchmark v3's passages, three things are measured on the open labelled cells, out of fold on the
benchmark's own spatial folds:

* a **text-only** model: TF-IDF over a cell's passages, logistic regression, per view (redacted, raw). The
  redacted view is what arms read; the pre-registration's gate is that it stays well below the effort null.
* a **count-only** model: how many passages and characters a cell has. Deposit cells have about four times the
  reports of their matched negatives; if volume ranks the cells, the cap failed.
* the **words** the text-only model leans on most, so an outcome word that slipped through redaction shows.

The raw view is expected to leak (it keeps the outcomes); it is the ceiling a redaction is measured against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..analyst import frozen as F
from ..prospect import models as M


def _texts(bench: F.Bench, sub: str, ids: list[str]) -> list[str]:
    out = []
    for b in ids:
        p = bench.dir / sub / f"{b}.json"
        rows = json.loads(p.read_text()).get("passages", []) if p.is_file() else []
        out.append(" ".join(str(r.get("text") or "") for r in rows))
    return out


def out_of_fold(x: Any, y: np.ndarray, folds: np.ndarray, c: float = 1.0) -> np.ndarray:
    """Probabilities for each fold from a logistic regression fitted on the other folds."""
    from sklearn.linear_model import LogisticRegression

    p = np.full(len(y), np.nan)
    for k in np.unique(folds):
        test, train = folds == k, folds != k
        if len(np.unique(y[train])) < 2:
            continue
        model = LogisticRegression(C=c, max_iter=2000, class_weight="balanced").fit(x[train], y[train])
        p[test] = model.predict_proba(x[test])[:, 1]
    return p


def audit(version: str) -> dict[str, Any]:
    """The text-only and count-only rankings of the open labelled cells, with the words that carry them."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    from ..analyst.compare import labelled
    from ..prospect import headline as H

    bench = F.load_bench(version)
    ids, y, folds_by, _cells = labelled(version)
    folds = np.array([folds_by[b] for b in ids])
    out: dict[str, Any] = {"version": version, "cells": len(ids), "positives": int(y.sum()),
                           "base_rate": float(y.mean())}
    for view, sub in (("redacted", "passages"), ("raw", "passages_raw")):
        texts = _texts(bench, sub, ids)
        have = np.array([bool(t.strip()) for t in texts])
        vec = TfidfVectorizer(min_df=2, max_df=0.9, sublinear_tf=True, token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]+\b")
        try:
            x = vec.fit_transform(texts)
        except ValueError:
            out[view] = {"note": "no text"}
            continue
        p = out_of_fold(x, y, folds)
        p = np.where(np.isfinite(p), p, 0.5)
        full = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(x, y)
        terms = np.array(vec.get_feature_names_out())
        order = np.argsort(full.coef_[0])
        out[view] = {"pr_auc": M.pr_auc(y, p), "pr_auc_ci": H.bootstrap_ci(y, p, M.pr_auc, n=2000, seed=0),
                     "roc_auc": M.roc_auc(y, p), "cells_with_text": int(have.sum()),
                     "positives_with_text": int(y[have].sum()),
                     "toward_positive": [str(t) for t in terms[order[::-1][:25]]],
                     "toward_negative": [str(t) for t in terms[order[:15]]]}
    counts = []
    for b in ids:
        p = bench.dir / "passages" / f"{b}.json"
        rows = json.loads(p.read_text()).get("passages", []) if p.is_file() else []
        counts.append((len(rows), sum(len(str(r.get("text") or "")) for r in rows)))
    c = np.array(counts, dtype=float)
    out["count_only"] = {"pr_auc_passages": M.pr_auc(y, c[:, 0]), "pr_auc_chars": M.pr_auc(y, c[:, 1]),
                         "mean_passages": {"positive": float(c[y == 1, 0].mean()), "negative": float(c[y == 0, 0].mean())}}
    return out


def write(version: str, result: dict[str, Any], root: Path | None = None) -> Path:
    from ..analyst.score import out_dir

    d = root or out_dir(version)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "leak.json"
    path.write_text(json.dumps(result, indent=1) + "\n")
    return path


__all__ = ["audit", "out_of_fold", "write"]
