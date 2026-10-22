"""
Supabase Storage — object storage for files people upload.

The Data setup screen accepts a facility's historical demand CSV. The rows go
into the forecast model as before; the original file is now also kept in a
private Supabase Storage bucket under ``<facility>/<timestamp>.csv``, so a
forecast can always be traced back to exactly the data it was trained on.

It is also the fallback home for custody certificates when the AWS stack is
not deployed, so the certificate feature works with Supabase alone.

Plain REST calls with the service-role key (server side only — that key
bypasses row-level security and must never reach the browser).
"""

from typing import Optional

import httpx

from cloud.config import env

TIMEOUT = 15


class StorageError(Exception):
    pass


def configured() -> bool:
    return bool(env("SUPABASE_URL") and env("SUPABASE_SERVICE_ROLE_KEY"))


def bucket() -> str:
    return env("SUPABASE_STORAGE_BUCKET", "plateletiq-files")


def _headers(extra: Optional[dict] = None) -> dict:
    key = env("SUPABASE_SERVICE_ROLE_KEY")
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    headers.update(extra or {})
    return headers


def _base() -> str:
    return env("SUPABASE_URL").rstrip("/") + "/storage/v1"


def ensure_bucket() -> None:
    """Create the private bucket on first use; an existing bucket is fine."""
    res = httpx.post(
        f"{_base()}/bucket",
        headers=_headers(),
        json={"id": bucket(), "name": bucket(), "public": False},
        timeout=TIMEOUT,
    )
    if res.status_code not in (200, 201, 400, 409):
        raise StorageError(f"Supabase bucket check failed ({res.status_code}): {res.text[:200]}")


def upload(path: str, data: bytes, content_type: str) -> dict:
    if not configured():
        raise StorageError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set.")
    url = f"{_base()}/object/{bucket()}/{path}"
    headers = _headers({"Content-Type": content_type, "x-upsert": "true"})
    res = httpx.post(url, headers=headers, content=data, timeout=TIMEOUT)
    if res.status_code == 404 or (res.status_code == 400 and "Bucket not found" in res.text):
        ensure_bucket()
        res = httpx.post(url, headers=headers, content=data, timeout=TIMEOUT)
    if res.status_code >= 300:
        raise StorageError(f"Supabase upload failed ({res.status_code}): {res.text[:200]}")
    return {"bucket": bucket(), "path": path, "url": signed_url(path)}


def signed_url(path: str, seconds: int = 7 * 24 * 3600) -> Optional[str]:
    res = httpx.post(
        f"{_base()}/object/sign/{bucket()}/{path}",
        headers=_headers(),
        json={"expiresIn": seconds},
        timeout=TIMEOUT,
    )
    if res.status_code >= 300:
        return None
    signed = res.json().get("signedURL") or res.json().get("signedUrl")
    if not signed:
        return None
    return env("SUPABASE_URL").rstrip("/") + "/storage/v1" + signed if signed.startswith("/") else signed


def list_files(prefix: str, limit: int = 20) -> list:
    if not configured():
        return []
    res = httpx.post(
        f"{_base()}/object/list/{bucket()}",
        headers=_headers(),
        json={"prefix": prefix, "limit": limit, "sortBy": {"column": "created_at", "order": "desc"}},
        timeout=TIMEOUT,
    )
    if res.status_code >= 300:
        return []
    return res.json()


def status() -> dict:
    return {"configured": configured(), "bucket": bucket() if configured() else None}
