"""Chip storage abstraction.

`put_chip` writes a rendered PNG either to a local folder (dev) or to an
S3-compatible bucket (prod) and returns the public URL the browser will use.
Chips are binary and large -- they never go in git; they live here.
"""
from __future__ import annotations

import os
import shutil

from .config import settings


def put_chip(local_path: str, key: str) -> str:
    """Store one chip under `key`; return its public URL."""
    if settings.storage_backend == "local":
        dest = os.path.join(settings.storage_local_dir, key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(local_path, dest)
        return f"{settings.chip_base_url}/{key}"

    if settings.storage_backend == "s3":
        import boto3  # imported lazily so dev doesn't need boto3

        s3 = boto3.client(
            "s3",
            endpoint_url=settings.storage_s3_endpoint,
            aws_access_key_id=settings.storage_s3_access_key,
            aws_secret_access_key=settings.storage_s3_secret_key,
        )
        s3.upload_file(local_path, settings.storage_s3_bucket, key)
        return f"{settings.chip_base_url}/{key}"

    raise ValueError(f"unknown storage backend: {settings.storage_backend!r}")
