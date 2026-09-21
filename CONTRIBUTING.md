# Contributing

## Set up

    cd pipeline && uv sync --dev --extra mlflow
    cd web && npm install

The analytics store is not in git. Unpack a seed pack first (see README.md, Quick start).

## Run the checks

    cd pipeline && uv run pytest -q                 # about five minutes; needs no model and no key
    cd web && npm run typecheck && npm run lint && npm test && npm run validate:data
    cd web && npm run e2e                           # about three minutes

The pipeline suite uses fake backends. Nothing in the suites calls a model.

## Rules that every change keeps

- A model never computes. Tools compute; every number a tool returns carries a value id.
- Every number an agent states must resolve to a stored value id, or the answer is withheld.
- Unknown and absent are distinct. A missing observation is never a low reading.
- No drilling advice, and no wording that implies it. Colour on the map encodes status or source, never merit.
- Anything unfinished is either finished, removed, or a one-sentence stated boundary at its site with the item
  in PRD.md's backlog.
- Nothing that carries a licence it cannot pass on goes into git: report PDFs, page images, the analytics store.

## Add an arm

An analyst arm is one file under `pipeline/configs/arms/`. Copy the nearest one, change one switch, name it
after the switch, and run `uv run ue arm run --arm <name>`. GUIDE.md describes every switch.

## Report a problem

Open an issue with the command you ran, the run id if there is one (under `pipeline/data/runs/`), and what
you expected. For a wrong number on screen, include the value id shown in the chip.
