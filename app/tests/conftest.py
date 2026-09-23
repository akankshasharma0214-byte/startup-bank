import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base


@pytest.fixture()
def db():
    # StaticPool: one shared in-memory connection, so tables created here are visible across the session.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
