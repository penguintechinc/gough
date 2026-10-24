#!/usr/bin/env python3
"""Quart Backend Entry Point."""

import os
import sys
import time
import asyncio

from app import create_app
from app.config import Config
from app.lifecycle import build_hypercorn_config, serve_app


def wait_for_database(max_retries: int = 30, retry_delay: int = 2) -> bool:
    """Wait for database to be available."""
    from pydal import DAL

    db_uri = Config.get_db_uri()
    print(f"Waiting for database connection...")

    for attempt in range(1, max_retries + 1):
        try:
            db = DAL(db_uri, pool_size=1, migrate=False)
            db.executesql("SELECT 1")
            db.close()
            print(f"Database connection successful after {attempt} attempt(s)")
            return True
        except Exception as e:
            print(f"Database connection attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)

    return False


async def main():
    """Main entry point."""
    # Wait for database
    if not wait_for_database():
        print("ERROR: Could not connect to database after maximum retries")
        sys.exit(1)

    # Create Quart app
    app = await create_app()

    # Get configuration. Graceful shutdown window comes from GRACEFUL_SHUTDOWN_TIMEOUT
    # (default 25s, must stay under the pod's 30s terminationGracePeriodSeconds).
    host = os.getenv("QUART_HOST", "0.0.0.0")
    port = int(os.getenv("QUART_PORT", "5000"))
    server_config = build_hypercorn_config(f"{host}:{port}")

    print(f"Starting Quart backend on {host}:{port} (graceful_timeout={server_config.graceful_timeout}s)")

    # serve_app handles SIGINT/SIGTERM -> lifespan shutdown -> after_serving teardown.
    await serve_app(app, server_config)


if __name__ == "__main__":
    asyncio.run(main())
