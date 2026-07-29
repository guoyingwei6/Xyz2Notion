"""Bounded Notion-only repair for fragile external podcast cover images."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from xyz2notion.asr.audio import AudioPreparationError, validate_public_audio_url
from xyz2notion.notion.client import (
    NOTION_SINGLE_UPLOAD_LIMIT,
    JsonObject,
    NotionAPIError,
)

MAX_COVER_BYTES = min(10 * 1024 * 1024, NOTION_SINGLE_UPLOAD_LIMIT)
MAX_COVER_REDIRECTS = 5
_IMAGE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


class CoverRepairAPI(Protocol):
    def query_data_source_page(
        self,
        data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]: ...

    def upload_file(self, filename: str, content_type: str, content: bytes) -> str: ...

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject: ...


@dataclass(frozen=True)
class CoverRepairReport:
    repaired: int
    skipped: int
    failed: int


def _external_cover_url(page: Mapping[str, Any]) -> str | None:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return None
    cover = properties.get("Cover")
    if not isinstance(cover, Mapping):
        return None
    files = cover.get("files")
    if not isinstance(files, list) or not files:
        return None
    first = files[0]
    if not isinstance(first, Mapping) or first.get("type") != "external":
        return None
    external = first.get("external")
    if not isinstance(external, Mapping) or not external.get("url"):
        return None
    return str(external["url"])


def _bounded_image_download(
    url: str,
    http: httpx.Client,
) -> tuple[bytes, str]:
    current = validate_public_audio_url(url)
    try:
        for _redirect in range(MAX_COVER_REDIRECTS + 1):
            with http.stream("GET", current, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise AudioPreparationError("Cover redirect has no Location header")
                    current = validate_public_audio_url(urljoin(current, location))
                    continue
                if response.is_error:
                    raise AudioPreparationError(
                        f"Cover download failed with HTTP {response.status_code}"
                    )
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in _IMAGE_EXTENSIONS:
                    raise AudioPreparationError("Cover response is not a supported image")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_COVER_BYTES:
                    raise AudioPreparationError("Cover exceeds size limit")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_COVER_BYTES:
                        raise AudioPreparationError("Cover exceeds size limit")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise AudioPreparationError("Cover response is empty")
                return content, content_type
        raise AudioPreparationError("Cover download exceeded redirect limit")
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise AudioPreparationError(
            f"Cover download transport failure: {type(exc).__name__}"
        ) from exc


def _filename(url: str, content_type: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    path_suffix = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    extension = _IMAGE_EXTENSIONS[content_type]
    if "." in path_suffix:
        candidate = "." + path_suffix.rsplit(".", 1)[-1]
        if candidate in _IMAGE_EXTENSIONS.values():
            extension = candidate
    return f"xyz2notion-cover-{digest}{extension}"


class NotionCoverLocalizer:
    """Replace at most ``limit`` external Cover fields with Notion uploads."""

    def __init__(
        self,
        api: CoverRepairAPI,
        data_source_ids: Sequence[str],
        *,
        sort_property: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api = api
        self.data_source_ids = tuple(data_source_ids)
        self.sort_property = sort_property
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=30)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> NotionCoverLocalizer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def repair(self, *, limit: int) -> CoverRepairReport:
        if not 1 <= limit <= 10:
            raise ValueError("cover repair limit must be between 1 and 10")
        repaired = skipped = failed = 0
        candidates: list[tuple[str, str]] = []
        for data_source_id in self.data_source_ids:
            payload: JsonObject = {"page_size": 100}
            if self.sort_property:
                payload["sorts"] = [
                    {
                        "property": self.sort_property,
                        "direction": "descending",
                    }
                ]
            pages = self.api.query_data_source_page(
                data_source_id,
                payload,
            )
            for page in pages:
                page_id = page.get("id")
                url = _external_cover_url(page)
                if not page_id or not url:
                    skipped += 1
                    continue
                candidates.append((str(page_id), url))
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break

        for page_id, url in candidates:
            try:
                content, content_type = _bounded_image_download(url, self._http)
                filename = _filename(url, content_type)
                upload_id = self.api.upload_file(filename, content_type, content)
                uploaded: JsonObject = {
                    "type": "file_upload",
                    "file_upload": {"id": upload_id},
                }
                self.api.update_page(
                    page_id,
                    {
                        "properties": {
                            "Cover": {
                                "files": [
                                    {
                                        "name": filename,
                                        **uploaded,
                                    }
                                ]
                            }
                        },
                        "icon": uploaded,
                        "cover": uploaded,
                    },
                )
                repaired += 1
            except (AudioPreparationError, NotionAPIError, ValueError, httpx.HTTPError):
                failed += 1
        return CoverRepairReport(repaired=repaired, skipped=skipped, failed=failed)
