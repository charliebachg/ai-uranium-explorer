"""The analyst: a model that reads a frozen benchmark cell closed-book and is scored against the answer key.

`arms` are the configurations (one question each), `v0` is the one-call agent, `frozen` reads the benchmark,
`run` is the harness that runs an arm over the open cells, `score` turns runs and the fitted baselines into
one table with intervals, and `cli` exposes them as `ue bench-run ...`.
"""
