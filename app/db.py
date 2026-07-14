from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})

# ponytail: no alembic for this MVP; new nullable columns are added by hand here.
# Upgrade to a real migration tool if the schema starts changing often.
_MESSAGE_COLUMNS_ADDED_AFTER_LAUNCH = {
    "wa_message_id": "VARCHAR",
    "wa_status": "VARCHAR",
    "wa_status_at": "DATETIME",
    "media_path": "VARCHAR",
    "sent_by": "VARCHAR",
}


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns("message", _MESSAGE_COLUMNS_ADDED_AFTER_LAUNCH)
    with engine.begin() as conn:
        # Respaldo contra webhooks duplicados concurrentes: el chequeo en
        # conversation._already_processed cubre el caso normal; este índice
        # garantiza unicidad si dos reintentos de Meta llegan a la vez.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_message_wa_message_id "
                "ON message(wa_message_id) WHERE wa_message_id IS NOT NULL"
            )
        )


def _add_missing_columns(table: str, columns: dict[str, str]) -> None:
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        for name, col_type in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))


def get_session() -> Session:
    return Session(engine)
