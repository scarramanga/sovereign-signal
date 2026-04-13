from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from server.config import settings

engine = None
AsyncSessionLocal = None

if settings.database_url:
    engine = create_async_engine(settings.database_url, echo=False)
    AsyncSessionLocal = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

async def get_db():
    if AsyncSessionLocal is None:
        raise RuntimeError("Database not configured")
    async with AsyncSessionLocal() as session:
        yield session
