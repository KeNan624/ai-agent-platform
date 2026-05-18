import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/ai_agent_platform",
)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


engine_options = {
    "pool_pre_ping": True,
}

if not DATABASE_URL.startswith("sqlite"):
    engine_options.update(
        {
            "pool_size": _int_env("DB_POOL_SIZE", 5),
            "max_overflow": _int_env("DB_MAX_OVERFLOW", 10),
            "pool_timeout": _int_env("DB_POOL_TIMEOUT", 30),
            "pool_recycle": _int_env("DB_POOL_RECYCLE", 1800),
        }
    )

engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
