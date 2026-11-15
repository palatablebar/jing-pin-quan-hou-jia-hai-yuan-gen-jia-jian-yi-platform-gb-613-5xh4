"""测试夹具：用临时 SQLite 替换 PostgreSQL，跑真实服务层（不连外部依赖）。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from src.config import database as db_module
    from src.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'pricewatch.db'}", future=True)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    Base.metadata.create_all(engine)
    session = factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()
        engine.dispose()
