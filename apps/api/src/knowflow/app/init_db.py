"""Install the release schema into an empty MySQL database."""

from __future__ import annotations

import asyncio
import os
from importlib.resources import files

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


async def initialize_database(connection: AsyncConnection) -> None:
    database = await connection.scalar(text("SELECT DATABASE()"))
    if not database:
        raise RuntimeError("Select a database in KNOWFLOW_MYSQL_URL first.")
    lock_name = f"knowflow-init:{database}"[:64]
    acquired = await connection.scalar(
        text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}
    )
    if acquired != 1:
        raise RuntimeError("Another database initialization is running.")
    try:
        existing = await connection.scalar(
            text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()")
        )
        if existing:
            raise RuntimeError(
                "Database must be empty. Existing tables were found. "
                "Use Alembic for an existing installation."
            )
        schema = files("knowflow.app").joinpath("schema.sql").read_text(encoding="utf-8")
        # The release SQL contains plain DDL and one version INSERT, without routines.
        for statement in schema.split(";"):
            if statement.strip():
                await connection.exec_driver_sql(statement)
        await connection.commit()
    finally:
        await connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})


async def main() -> None:
    database_url = os.environ.get("KNOWFLOW_MYSQL_URL")
    if not database_url:
        raise RuntimeError("KNOWFLOW_MYSQL_URL is required. Load .env with uv --env-file.")
    url = make_url(database_url)
    if url.drivername != "mysql+asyncmy" or not url.database:
        raise RuntimeError("KNOWFLOW_MYSQL_URL must use mysql+asyncmy and select a database.")
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await initialize_database(connection)
        print("KnowFlow database initialized at revision 20260822_0009.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
