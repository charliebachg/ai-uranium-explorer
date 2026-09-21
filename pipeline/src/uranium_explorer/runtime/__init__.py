"""The shared runtime every kind of run stands on.

Four things live here, and nothing that belongs to one agent:

* `runs`      run ids and run directories, the same for an extract, a memo, a chat or a bench run;
* `manifest`  the run manifest: what was asked, of which model, against which store, for how much;
* `spend`     one ledger for every backend family, a cumulative ceiling and a per-run budget, both checked
              before a live call is sent;
* `tracing`   spans for runs, cells, tool calls and model calls, written to the run directory and mirrored to
              MLflow Tracing when it is there;
* `rekey`     moves cached calls recorded under the old cache key (B22) to the key a live request computes now.

This package imports nothing from the agents, so the agents can import all of it.
"""
