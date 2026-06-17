"""FastAPI entry point: labeling API + the static pages.

Serves the API from app.api, the rendered chips (when using local storage), and
three pages: the tasks landing (/), the labeler (/label?flight=ID), and progress
(/progress[?flight=ID|?project=ID]).
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


def _page(name: str) -> FileResponse:
    return FileResponse(os.path.join(_WEB, name))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Serve chips locally. With the S3/R2 backend, chip_keys are absolute URLs and
# this mount is unused.
if settings.storage_backend == "local":
    os.makedirs(settings.storage_local_dir, exist_ok=True)
    app.mount("/chips", StaticFiles(directory=settings.storage_local_dir), name="chips")


@app.get("/style.css")
def style() -> FileResponse:
    return FileResponse(os.path.join(_WEB, "style.css"), media_type="text/css")


@app.get("/")
def landing() -> FileResponse:
    return _page("projects.html")


@app.get("/label")
def labeler() -> FileResponse:
    return _page("index.html")


@app.get("/progress")
def progress() -> FileResponse:
    return _page("progress.html")
