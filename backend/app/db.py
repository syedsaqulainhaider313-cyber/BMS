"""Database engine and session management.

Configured with production-grade QueuePool sizing, connection recycling,
and pgBouncer transaction pooling compatibility (Psycopg 3 prepared statement detachment).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.settings import settings


class Base(DeclarativeBase):
    """Base class for persistent BMS records."""


def get_engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "pool_pre_ping": True,
    }

    # QueuePool and Psycopg-specific arguments apply only to PostgreSQL
    if settings.database_url.startswith("postgresql"):
        kwargs.update(
            {
                "pool_size": getattr(settings, "db_pool_size", 10),
                "max_overflow": getattr(settings, "db_max_overflow", 15),
                "pool_recycle": getattr(settings, "db_pool_recycle", 1800),
                "pool_timeout": getattr(settings, "db_pool_timeout", 30),
                "connect_args": {
                    # Disable server-side prepared statement cache in psycopg 3.
                    # Mandatory for pgBouncer transaction pooling mode to prevent
                    # 'prepared statement does not exist' errors across pooled backends.
                    "prepare_threshold": None,
                },
            }
        )
    return kwargs


engine = create_engine(settings.database_url, **get_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
