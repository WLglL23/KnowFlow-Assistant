from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from knowflow.app.config import Settings


class PrivateObjectStore:
    """Private MinIO object operations shared by document capabilities."""

    def __init__(self, settings: Settings) -> None:
        self._endpoint = settings.minio_endpoint.rstrip("/")
        self._access_key = settings.minio_access_key
        self._secret_key = settings.minio_secret_key.get_secret_value()
        self._bucket = settings.minio_bucket

    async def put(self, key: str, content: bytes) -> None:
        bucket = quote(self._bucket, safe="")
        async with httpx.AsyncClient(timeout=10) as client:
            bucket_path = f"/{bucket}"
            response = await client.put(
                f"{self._endpoint}{bucket_path}",
                headers=self._headers("PUT", bucket_path, b""),
                content=b"",
            )
            if response.status_code not in {200, 409}:
                response.raise_for_status()
            path = self._object_path(key)
            response = await client.put(
                f"{self._endpoint}{path}",
                headers=self._headers("PUT", path, content),
                content=content,
            )
            response.raise_for_status()

    async def get(self, key: str) -> bytes:
        path = self._object_path(key)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{self._endpoint}{path}",
                headers=self._headers("GET", path, b""),
            )
            response.raise_for_status()
            return response.content

    async def delete(self, key: str) -> None:
        path = self._object_path(key)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.delete(
                f"{self._endpoint}{path}",
                headers=self._headers("DELETE", path, b""),
            )
        if response.status_code not in {200, 204, 404}:
            response.raise_for_status()

    def _object_path(self, key: str) -> str:
        return f"/{quote(self._bucket, safe='')}/{quote(key, safe='/')}"

    def _headers(self, method: str, path: str, payload: bytes) -> dict[str, str]:
        parsed = urlsplit(self._endpoint)
        now = datetime.now(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_headers = (
            f"host:{parsed.netloc}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{stamp}\n"
        )
        signed = "host;x-amz-content-sha256;x-amz-date"
        request = "\n".join([method, path, "", canonical_headers, signed, payload_hash])
        scope = f"{date}/us-east-1/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()]
        )
        key = hmac.new(
            f"AWS4{self._secret_key}".encode(),
            date.encode(),
            hashlib.sha256,
        ).digest()
        for value in ("us-east-1", "s3", "aws4_request"):
            key = hmac.new(key, value.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._access_key}/{scope},"
            f"SignedHeaders={signed},Signature={signature}"
        )
        return {
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": stamp,
            "Authorization": authorization,
        }
