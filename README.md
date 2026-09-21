# AI Uranium Explorer

AI Uranium Explorer reads public Saskatchewan uranium records, provincial GIS layers and scanned assessment
reports, into a checked and traceable store, and scores 2 km cells over the Athabasca Basin three ways beside
a null model built from exploration effort alone. An agent sits beside each cell and can only speak from the
same tools the scores were computed from: every number it states is checked against a stored value before it
is shown, and an answer that fails is withheld with the objection in its place. It is a prototype on public
data; it gives no drilling advice, and no geologist has reviewed it.

> Retrospective scoring of public data. No geologist has seen this, and the fold tests show how much of the
> ranking is explained by where people already drilled.

## The acceptance story

One person opens the dashboard, sees the data and its readiness, inspects the model results on the Eval page
and the analyst's chains on the enabled cells, talks to the interface agent about a cell, and has it call the
analyst on that cell as a background job. Every model call in that story runs on a cheap pay-per-token model.
The benchmark runs afterwards, once the story works end to end. PRD.md states it in full.

## Quick start

The analytics store is not in git. A fresh clone first pulls a seed pack from the URL it is published at (an
`s3://` prefix or an `https://` directory; every file is hash-checked), or places one at
`pipeline/data/seed/latest`.

    cd pipeline && uv sync && uv run ue store seed pull <url>

With Docker, from the repository root:

    docker compose up -d --build        # the app unpacks the pack on first start and serves on http://localhost:8787

Without Docker:

    cd pipeline
    uv run ue store seed unpack data/seed/latest
    uv run ue prospect serve            # http://localhost:8787: the site, the evidence record, the chat, /docs
    cd ../web && npm install && npm run dev    # the site on http://localhost:5173 during development

The map, the scores, the layers and the tour are static files and work with nothing running; the evidence
panel and the chat need the service. The chat needs a model key in the repository's `.env` (gitignored; see `.env.example`): an OpenRouter
key for the default `auto` backend, which is API first (a `vendor/model` id to OpenRouter, a `claude-*` id
to OpenRouter's Anthropic listing, a `gpt-*` id to OpenAI), or an OpenAI key with `--backend openai`. The public seed pack holds only redistributable tables, so document readings and analyst chains
appear only when the private pack is unpacked instead.

## From your own MCP client

The same tools serve a stock client. Save this as `.mcp.json` in the repository root (Claude Code) or as
`.cursor/mcp.json` (Cursor):

    {"mcpServers": {"ai-uranium-explorer": {"command": "uv", "args": ["run", "--directory", "pipeline", "ue", "mcp", "serve", "--stdio"]}}}

Without a key register a local client has every scope; GUIDE.md's section on the MCP server covers keys,
scopes and the public-safe mode.

## The repository

    pipeline/             Python 3.13 with uv; the command is `uv run ue ...` (`ue --help` lists the groups)
    pipeline/knowledge/   the criteria table, the handbook, the data inventory, the enabled cells, the dated discoveries
    pipeline/configs/     analyst arms, benchmark specs, reader configurations
    pipeline/migrations/  the serving-database schema (Alembic)
    web/                  Vite + React + MapLibre: the map, the evidence rail, the chat; the data, Eval, limits and review pages
    gold/                 the held-out lock and the hand-keyed gold pages (none keyed yet)

Read GUIDE.md for how it works, control by control and stage by stage, and PRD.md for what it is, what it must
do, and what is left.

## Data and licences

Every source, its licence and whether it may be redistributed is listed in the inventory
(`pipeline/knowledge/data_inventory.toml`) and rendered in the app under "Data sources and licences"; each
layer carries its own licence. Provincial layers come from the Government of Saskatchewan under its open data
licence, the NTS grid under the Open Government Licence - Canada, imagery from Copernicus and the USGS, and
basemap tiles from OpenFreeMap on OpenStreetMap data. Assessment report PDFs carry no named licence: they are
read locally and never served, and their page images stay out of git. The public seed pack holds only
redistributable tables.

## Licence

The code and documents in this repository are under the MIT licence (see LICENSE). The data keeps the licences
of its sources.
