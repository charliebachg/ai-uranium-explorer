#!/bin/zsh
# The Opus-only read of the enabled cells' top drilling files (knowledge/enabled_cells.toml), carried through to the dashboard.
# Every stage is resumable: extract skips pages already in its results file and replays cached calls for
# free, so a run cut off part-way loses at most the call in flight. Run it from a plain terminal: on a
# small machine the CLI's low-memory watchdog kills long background jobs, so the reads run in the foreground.
set -e
cd "$(dirname "$0")/.."
FILES=(MAW00509 64L04-0130 64L04-0141 MAW00131 64L04-0140 MAW01968 MAW02845 74H04-0110 74H04-0091 74H04-0097 MAW00624 74G07-0064 MAW00645 MAW00193 74H04-0094 74G07-0070 74H16-0034 74K-0018)
ARGS=(); for f in $FILES; do ARGS+=(--files $f); done
echo "== extract (Opus, one worker, resumable)"; uv run ue extract --config opus1-assay --max-calls 260 $ARGS
echo "== usage";                                 uv run ue run-usage --expect-model claude-opus-5
echo "== assemble";                              uv run ue assemble $ARGS
echo "== validate";                              uv run ue validate $ARGS --mode enforce
echo "== crs transform";                         uv run ue crs transform $ARGS
echo "== crosscheck";                            uv run ue crosscheck $ARGS --no-fetch-lith
echo "== validate (with positions and matches)"; uv run ue validate $ARGS --mode enforce
echo "== store rebuild";                         uv run ue store rebuild
echo "== exports";                               uv run ue export-reports && uv run ue export-web && uv run ue export-eval
echo "== snapshot";                              uv run ue store snapshot
echo "== gate";                                  uv run ue prospect gate || true
echo "== prospect export";                       uv run ue prospect export
echo "read-enabled done"
