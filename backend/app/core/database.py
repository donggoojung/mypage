from collections.abc import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

# FastAPI 요청 처리용 비동기 엔진.
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Celery 워커(동기 컨텍스트)용 엔진 — 비동기 이벤트 루프 없이 태스크 안에서 바로 사용한다.
sync_engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
SessionLocalSync: sessionmaker[Session] = sessionmaker(bind=sync_engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


def get_db_sync() -> Generator[Session, None, None]:
    with SessionLocalSync() as session:
        yield session
