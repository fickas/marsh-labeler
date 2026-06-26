"""FastAPI entry point: labeling API + the static pages.

Serves the API from app.api, the rendered chips (when using local storage), and
three pages: the tasks landing (/), the labeler (/label?flight=ID), and progress
(/progress[?flight=ID|?project=ID]).
"""
from __future__ import annotations

import base64
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import settings

app = FastAPI(title="Marsh Labeler")


# --- HTTP Basic Auth gate -------------------------------------------------
# One shared secret: the PASSWORD. The username can be anything -- whatever the
# labeler types at the browser prompt becomes their identity (see /whoami), so
# there's no separate in-app username step. Active only when basic_auth_pass is
# set (on Railway; unset in local dev). /health is always exempt so Railway's
# health check can reach the app without credentials.
@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    pw = settings.basic_auth_pass
    if not pw or request.url.path == "/health":
        return await call_next(request)

    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            got_user, _, got_pw = base64.b64decode(header[6:]).decode().partition(":")
            # any non-empty username; constant-time check on the password only
            if got_user and secrets.compare_digest(got_pw, pw):
                request.state.auth_user = got_user
                return await call_next(request)
        except Exception:
            pass
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Marsh Labeler"'},
    )


app.include_router(router)

_WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


def _page(name: str) -> FileResponse:
    return FileResponse(os.path.join(_WEB, name))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/whoami")
def whoami(request: Request) -> dict[str, str]:
    # The username typed at the Basic Auth prompt, surfaced to the front end so
    # it can label as that identity. Empty when the gate is off (local dev).
    return {"user": getattr(request.state, "auth_user", "")}


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
