"""The interface agent (PRD §8.3): the chat panel's router, its deterministic plans, its three actions and the gate.

It adds no signal. It finds (the eight tools), explains (one answer call over what the tools returned), records
(a geologist's insight into the `expert` tier) and invokes (the analyst, as a job). The pieces:

* `router`       one cheap structured call that classifies the question into a fixed kind; each kind has a
                 plan of tool calls written in Python, so the model decides *what is being asked* and never
                 what the answer is
* `sensitivity`  the one computation the router can ask for: which unknown criterion, measured and met,
                 would move the criteria score most, over the weights and memberships the store holds
* `actions`      `abstain`, `record_insight` and `invoke_analyst`, through the same functions the MCP server
                 runs, so the chat and a geologist's own client write the same rows
* `diff`         the session assessment: a finished analyst job's verdict beside the cell's stored chain
                 without the insight, node by node
* `agent`        the turn: route, plan or loop, answer, gate (refuse and retry once), report
* `loop`         the plain tool loop the unrouted kind falls back to, capped at `MAX_STEPS`

The model is cheap on purpose: the value ids and the gate carry the correctness, not the model. It defaults
to `z-ai/glm-5.3-flash` through OpenRouter and is read from `LR_INTERFACE_MODEL` when that is set.
"""

from __future__ import annotations

import os

#: the cheap model the router and the answer run on unless `LR_INTERFACE_MODEL` says otherwise
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
MODEL_VAR = "LR_INTERFACE_MODEL"


def default_model() -> str:
    """The interface model: the environment's choice, else the cheap default."""
    return os.environ.get(MODEL_VAR, "").strip() or DEFAULT_MODEL


__all__ = ["DEFAULT_MODEL", "MODEL_VAR", "default_model"]
