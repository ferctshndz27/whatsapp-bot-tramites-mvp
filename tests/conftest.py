import pytest
from sqlmodel import Session, SQLModel, create_engine

import app.db as db


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """Cada test corre contra su propia base en memoria: el catálogo ahora
    vive en SQLite, así que sin esto los tests que no parchan el engine
    escribirían en la base local real."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    yield


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
