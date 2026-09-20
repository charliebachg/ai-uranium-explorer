"""Alembic environment: raw-SQL migrations, no ORM models. The URL is set by `ue store migrate`."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine


def run() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run()
