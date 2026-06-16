"""FastAPI entry point.

This is intentionally minimal -- the data spine (models + ingestion) comes
first. The labeling API (next-container, submit-label, resolve, agreement) and
the React front end are the next phase; they hang off the models in app.models.
"""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Marsh Labeler")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
