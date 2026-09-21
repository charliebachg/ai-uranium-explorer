# One image: the built site and the API that serves it. The analytics store (pipeline/data) is mounted, not
# copied: it is a gigabyte of pulls and a DuckDB file, and it belongs to the machine that computed it. A fresh
# clone has no store; the first start unpacks the seed pack at UE_SEED_DIR (`ue store seed ensure`) into
# pipeline/data/ue.duckdb, verifying every table's hash on the way, pulling it from UE_SEED_URL first when
# there is none on disk, and does nothing when a store is present.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# the API serves the site, so service calls go to the same origin
ENV VITE_SERVICE_ROOT=""
RUN npm run build

FROM python:3.13-slim AS api
COPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UE_ROOT=/app
WORKDIR /app/pipeline
COPY pipeline/pyproject.toml pipeline/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY pipeline/src ./src
COPY pipeline/knowledge ./knowledge
COPY pipeline/configs ./configs
COPY pipeline/alembic.ini ./
COPY pipeline/migrations ./migrations
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/web/dist
EXPOSE 8787
CMD ["sh", "-c", "uv run ue store seed ensure && exec uv run ue prospect serve --host 0.0.0.0 --port 8787 --backend auto --web-dist /app/web/dist"]
