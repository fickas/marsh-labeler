"""FastAPI entry point: labeling API + the static pages.

Serves the API from app.api, the rendered chips (when using local storage), and
three pages: the tasks landing (/), the labeler (/label?flight=ID), and progress
(/progress[?flight=ID|?project=ID]).
"""
from __future__ import annotations

import base64
import os
import secrets

from fastapi import FastAPI, HTTPException, Request
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


# Chip serving. The bucket is private (no public URL), so on the s3 backend the
# app proxies reads: GET /chips/<key> -> fetch from the bucket -> stream back.
# In local dev, chips are files under storage_local_dir, served by a static mount.
if settings.storage_backend == "s3":
    _s3_client = None

    def _s3():
        global _s3_client
        if _s3_client is None:
            import boto3

            _s3_client = boto3.client(
                "s3",
                endpoint_url=settings.storage_s3_endpoint,
                aws_access_key_id=settings.storage_s3_access_key,
                aws_secret_access_key=settings.storage_s3_secret_key,
                region_name=settings.storage_s3_region,
            )
        return _s3_client

    @app.get("/chips/{key:path}")
    def serve_chip(key: str) -> Response:
        try:
            obj = _s3().get_object(Bucket=settings.storage_s3_bucket, Key=key)
            data = obj["Body"].read()
        except Exception:
            raise HTTPException(status_code=404, detail="chip not found")
        # chips are immutable once written, so let the browser cache them
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

elif settings.storage_backend == "local":
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
