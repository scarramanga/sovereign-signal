from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from server.config import settings


def build_engine():
    raw_url = settings.database_url
    if not raw_url:
        return None

    # Rewrite scheme for asyncpg
    url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # Strip sslmode and connect_timeout from URL — asyncpg handles these via connect_args
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("sslmode", None)
    params.pop("connect_timeout", None)
    clean_query = urlencode({k: v[0] for k, v in params.items()})
    clean_url = urlunparse(parsed._replace(query=clean_query))

    return create_async_engine(
        clean_url,
        echo=False,
        connect_args={"ssl": "require", "timeout": 5},
    )


engine = build_engine()
AsyncSessionLocal = (
    sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    if engine
    else None
)

async def get_db():
    if AsyncSessionLocal is None:
        raise RuntimeError("Database not configured")
    async with AsyncSessionLocal() as session:
        yield session
