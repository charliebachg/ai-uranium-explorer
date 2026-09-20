"""The one frame every bench stage reads: a row per scorable cell with its label, position, block and features.

The models module builds complete-case matrices per feature set, and that is right for a fair model
comparison. A benchmark needs something slightly different: every sampled cell must be scorable by every
baseline, so the geological features are required complete, while the effort features may carry a null
(`holes_first_year` is unknown wherever nothing was drilled, and a never-drilled cell is exactly what the
probe stratum is for). The gradient-boosting fits handle that null natively; the effort index ranks it as zero.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..prospect import models as M
from ..store import connect

#: what the sampler, the folds and the baselines need beside the features
CORE_COLUMNS = ("cell_id", "label_tier", "label_name", "camp_id", "block_id", "lon", "lat", "cx", "cy")


def latest_grid(con: Any) -> str | None:
    row = con.execute("select grid_id from derived.grid order by built_at desc limit 1").fetchone()
    return row[0] if row else None


def load_frame(grid_id: str | None = None, con: Any = None) -> pd.DataFrame:
    """Cells of the grid with complete geological features, their effort features (nulls kept), and the stored
    criteria score. Read-only on the store; `con` lets a test point it at a temporary one."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        grid_id = grid_id or latest_grid(con)
        keys = [*M.LEARNED_FEATURES, *M.EFFORT_FEATURES]
        placeholders = ",".join("?" * len(keys))
        wide = con.execute(
            f"select cell_id, feature_key, value from derived.cell_feature where feature_key in ({placeholders})",
            keys,
        ).df()
        labels = con.execute(
            "select l.cell_id, l.label_tier, l.label_name, l.camp_id, l.block_id, c.lon, c.lat, c.cx, c.cy "
            "from derived.cell_label l join derived.cell c using (cell_id) where c.grid_id = ?", [grid_id]
        ).df()
        crit = con.execute(
            "select cell_id, score as criteria_score from derived.cell_score where model = 'criteria'"
        ).df()
    finally:
        if own:
            con.close()
    if wide.empty:
        pivot = pd.DataFrame(columns=["cell_id", *keys])
    else:
        pivot = wide.pivot_table(index="cell_id", columns="feature_key", values="value", dropna=False).reset_index()
        pivot.columns.name = None
    for k in keys:
        if k not in pivot.columns:
            pivot[k] = np.nan
    df = labels.merge(pivot, on="cell_id", how="inner").merge(crit, on="cell_id", how="left")
    df = df.dropna(subset=list(M.LEARNED_FEATURES)).sort_values("cell_id").reset_index(drop=True)
    df["holes_n"] = df["holes_n"].fillna(0.0)
    return df
