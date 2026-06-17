"""FastAPI entry point: labeling API + the single-page labeling UI.

Serves the API from app.api, the rendered chips (when using local storage),
and the labeling page at /.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import settings

app = FastAPI(title="Marsh Labeler")
app.include_router(router)

_WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Serve chips locally. With the S3/R2 backend, chip_keys are absolute URLs and
# this mount is unused.
if settings.storage_backend == "local":
    os.makedirs(settings.storage_local_dir, exist_ok=True)
    app.mount("/chips", StaticFiles(directory=settings.storage_local_dir), name="chips")


@app.get("/")
def index():
    return FileResponse(os.path.join(_WEB, "index.html"))
