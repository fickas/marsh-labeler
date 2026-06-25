"""Runtime configuration, read from environment / .env (twelve-factor).

Same code runs locally (against the Docker Postgres) and on Railway (against the
managed Postgres + object storage); only the env vars differ.
"""
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLAlchemy URL. Railway injects DATABASE_URL for its Postgres service.
    database_url: str = "postgresql+psycopg://marsh:marsh@localhost:5432/marsh"

    # Chip storage: "local" writes to a folder (dev); "s3" uploads to an
    # S3-compatible bucket such as Cloudflare R2 (prod).
    storage_backend: str = "local"
    storage_local_dir: str = "./_chips"
    storage_s3_bucket: str | None = None
    storage_s3_endpoint: str | None = None
    storage_s3_access_key: str | None = None
    storage_s3_secret_key: str | None = None

    # Public URL prefix under which chips are served back to the browser.
    chip_base_url: str = "/chips"

    # Optional HTTP Basic Auth gate. When BOTH are set (e.g. on Railway), every
    # request except /health requires these credentials -- a single shared
    # username/password that keeps the public URL closed to strangers and bots.
    # Left unset (local dev), the gate is disabled and the app is open.
    basic_auth_user: str | None = None
    basic_auth_pass: str | None = None

    @field_validator("database_url")
    @classmethod
    def _psycopg3_driver(cls, v: str) -> str:
        # Railway injects DATABASE_URL as postgres:// or postgresql://, but we run
        # the psycopg (v3) driver and don't install psycopg2. Pin the dialect so
        # the same value works locally and on Railway with no hand-editing.
        scheme, sep, rest = v.partition("://")
        if not sep:
            return v
        if scheme in ("postgres", "postgresql"):
            return f"postgresql+psycopg://{rest}"
        return v


settings = Settings()
