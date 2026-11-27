"""Create or upgrade the database and sync the strategies table.

python -m app.tools.init_db
"""

from __future__ import annotations

import datetime as dt

from alembic.config import Config

from alembic import command
from app.config.loader import REPO_ROOT, load_config
from app.db.seed import sync_strategies_table
from app.db.session import make_engine, make_session_factory


def main() -> None:
    cfg = load_config()
    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(alembic_cfg, "head")
    engine = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    with make_session_factory(engine)() as session:
        inserted = sync_strategies_table(session, dt.datetime.now(dt.UTC))
        session.commit()
    print(f"database ready: {cfg.database.url}; strategy rows added: {len(inserted)}")


if __name__ == "__main__":
    main()
