"""Alembic environment. URL: ALGO_DB_URL env var, else config's database.url."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from app.config.loader import load_config
from app.db.models import Base
from app.db.session import make_engine

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return os.environ.get("ALGO_DB_URL") or load_config().database.url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
