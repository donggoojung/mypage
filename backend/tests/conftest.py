import os

# 테스트는 개발용 DB(fulfillment)를 건드리지 않도록 별도의 fulfillment_test DB를 사용한다.
# app.core.config 가 임포트되기 전에 환경변수를 세팅해야 Settings()에 반영된다.
os.environ.setdefault(
    "DATABASE_URL_SYNC", "postgresql+psycopg2://fulfillment:fulfillment@localhost:5432/fulfillment_test"
)
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://fulfillment:fulfillment@localhost:5432/fulfillment_test"
)
os.environ.setdefault("USE_MOCK_SCRAPERS", "true")
os.environ.setdefault("USE_MOCK_STORAGE", "true")

import pytest  # noqa: E402

from app.core.database import Base, SessionLocalSync, sync_engine  # noqa: E402
from app.models import *  # noqa: E402,F401,F403 — Base.metadata에 전 테이블을 등록하기 위해 로드


@pytest.fixture(scope="session", autouse=True)
def _create_test_schema():
    Base.metadata.create_all(bind=sync_engine)
    yield
    Base.metadata.drop_all(bind=sync_engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """테스트마다 모든 테이블을 비워 이전 테스트의 데이터가 섞이지 않게 한다."""
    yield
    with SessionLocalSync() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()


@pytest.fixture
def db_session():
    with SessionLocalSync() as session:
        yield session
