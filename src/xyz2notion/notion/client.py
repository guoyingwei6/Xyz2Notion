"""Minimal typed Notion client for API version 2026-03-11."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import SecretStr

from xyz2notion.security import (
    CredentialKind,
    redact_text,
    validate_credential_destination,
)

NOTION_API_VERSION = "2026-03-11"
NOTION_API_BASE_URL = "https://api.notion.com/v1"
NOTION_RICH_TEXT_LIMIT = 2000
NOTION_CHILDREN_LIMIT = 100
NOTION_SINGLE_UPLOAD_LIMIT = 20 * 1024 * 1024

JsonObject = dict[str, Any]


class NotionAPIError(RuntimeError):
    """Safe Notion error that never includes request credentials."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(redact_text(message))
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


def split_text(text: str, limit: int = NOTION_RICH_TEXT_LIMIT) -> list[str]:
    """Split text without loss so every rich-text object fits Notion limits."""
    if limit < 1:
        raise ValueError("text chunk limit must be positive")
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]


def rich_text(text: str) -> list[JsonObject]:
    """Create Notion rich-text objects with automatic 2,000-character splitting."""
    return [{"type": "text", "text": {"content": chunk}} for chunk in split_text(text) if chunk]


def paragraph_blocks(text: str) -> list[JsonObject]:
    """Turn arbitrary text into paragraph blocks while preserving every character."""
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text(chunk)},
        }
        for chunk in split_text(text)
    ]


class NotionClient:
    """Synchronous Notion client with retries, pagination, and safety checks."""

    _retryable_statuses = frozenset({429, 500, 502, 503, 504, 529})

    def __init__(
        self,
        token: str | SecretStr,
        *,
        client: httpx.Client | None = None,
        base_url: str = NOTION_API_BASE_URL,
        max_retries: int = 4,
        timeout_seconds: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        token_value = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not token_value:
            raise ValueError("Notion token cannot be empty")
        self.base_url = base_url.rstrip("/")
        validate_credential_destination(self.base_url, CredentialKind.NOTION)
        self.max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._headers = {
            "Authorization": f"Bearer {token_value}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "Xyz2Notion/0.1",
        }

    def close(self) -> None:
        """Close an internally-owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0, float(retry_after))
                except ValueError:
                    try:
                        target = parsedate_to_datetime(retry_after)
                        if target.tzinfo is None:
                            target = target.replace(tzinfo=UTC)
                        seconds = float((target - datetime.now(UTC)).total_seconds())
                        return max(0.0, seconds)
                    except (TypeError, ValueError):
                        pass
        return min(30.0, 2.0**attempt) + self._jitter()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        """Perform one safe request, retrying transient failures."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        validate_credential_destination(url, CredentialKind.NOTION)
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=self._headers,
                    params=params,
                    json=json_body,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise NotionAPIError(
                        f"Notion transport failure: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
            else:
                if response.status_code not in self._retryable_statuses:
                    return self._decode_response(response)
                if attempt >= self.max_retries:
                    self._raise_response_error(response, retryable=True)
            self._sleep(self._retry_delay(response, attempt))
        raise AssertionError("retry loop exhausted unexpectedly")

    def _decode_response(self, response: httpx.Response) -> JsonObject:
        if response.is_error:
            self._raise_response_error(response, retryable=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotionAPIError(
                "Notion returned a non-JSON response",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise NotionAPIError(
                "Notion returned a non-object JSON response",
                status_code=response.status_code,
            )
        return payload

    def _raise_response_error(self, response: httpx.Response, *, retryable: bool) -> None:
        code: str | None = None
        message = f"Notion request failed with HTTP {response.status_code}"
        try:
            payload = response.json()
            if isinstance(payload, dict):
                code_value = payload.get("code")
                message_value = payload.get("message")
                code = str(code_value) if code_value is not None else None
                if message_value:
                    message = str(message_value)
        except ValueError:
            pass
        raise NotionAPIError(
            message,
            status_code=response.status_code,
            code=code,
            retryable=retryable,
        )

    def paginate(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Iterator[JsonObject]:
        """Yield every result from a standard Notion cursor list."""
        current_params = dict(params or {})
        current_body = dict(json_body or {})
        while True:
            if method.upper() == "GET":
                page = self.request(method, path, params=current_params)
            else:
                page = self.request(method, path, json_body=current_body)
            results = page.get("results", [])
            if not isinstance(results, list):
                raise NotionAPIError("Notion list response has invalid results")
            for result in results:
                if not isinstance(result, dict):
                    raise NotionAPIError("Notion list contains a non-object result")
                yield result
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")
            if not cursor:
                raise NotionAPIError("Notion pagination has_more without next_cursor")
            if method.upper() == "GET":
                current_params["start_cursor"] = str(cursor)
            else:
                current_body["start_cursor"] = str(cursor)

    def search_databases(self, title: str) -> list[JsonObject]:
        """Search database containers by title."""
        return list(
            self.paginate(
                "POST",
                "/search",
                json_body={
                    "query": title,
                    "filter": {"property": "object", "value": "database"},
                    "page_size": 100,
                },
            )
        )

    def retrieve_page(self, page_id: str) -> JsonObject:
        return self.request("GET", f"/pages/{page_id}")

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject:
        return self.request("PATCH", f"/pages/{page_id}", json_body=payload)

    def create_page(
        self,
        parent_page_id: str,
        title: str,
        *,
        icon: str | None = None,
    ) -> JsonObject:
        body: JsonObject = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": {"type": "title", "title": rich_text(title)}},
        }
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        return self.request("POST", "/pages", json_body=body)

    def create_data_source_page(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
        *,
        icon: Mapping[str, Any] | None = None,
        cover: Mapping[str, Any] | None = None,
        children: Sequence[Mapping[str, Any]] = (),
    ) -> JsonObject:
        """Create one row page under a data source."""
        body: JsonObject = {
            "parent": {
                "type": "data_source_id",
                "data_source_id": data_source_id,
            },
            "properties": dict(properties),
        }
        if icon is not None:
            body["icon"] = dict(icon)
        if cover is not None:
            body["cover"] = dict(cover)
        if children:
            body["children"] = [dict(child) for child in children]
        return self.request("POST", "/pages", json_body=body)

    def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: Mapping[str, Any],
        *,
        icon: str | None = None,
        is_inline: bool = False,
    ) -> JsonObject:
        body: JsonObject = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": rich_text(title),
            "is_inline": is_inline,
            "initial_data_source": {"properties": dict(properties)},
        }
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        return self.request("POST", "/databases", json_body=body)

    def retrieve_database(self, database_id: str) -> JsonObject:
        return self.request("GET", f"/databases/{database_id}")

    def retrieve_data_source(self, data_source_id: str) -> JsonObject:
        return self.request("GET", f"/data_sources/{data_source_id}")

    def update_data_source(
        self,
        data_source_id: str,
        properties: Mapping[str, Any],
    ) -> JsonObject:
        return self.request(
            "PATCH",
            f"/data_sources/{data_source_id}",
            json_body={"properties": dict(properties)},
        )

    def query_data_source(
        self,
        data_source_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        return list(
            self.paginate(
                "POST",
                f"/data_sources/{data_source_id}/query",
                json_body=payload or {"page_size": 100},
            )
        )

    def list_views(
        self,
        *,
        database_id: str | None = None,
        data_source_id: str | None = None,
    ) -> list[JsonObject]:
        if not database_id and not data_source_id:
            raise ValueError("database_id or data_source_id is required")
        params: dict[str, str | int] = {"page_size": 100}
        if database_id:
            params["database_id"] = database_id
        if data_source_id:
            params["data_source_id"] = data_source_id
        return list(self.paginate("GET", "/views", params=params))

    def retrieve_view(self, view_id: str) -> JsonObject:
        return self.request("GET", f"/views/{view_id}")

    def create_view(self, payload: Mapping[str, Any]) -> JsonObject:
        return self.request("POST", "/views", json_body=payload)

    def update_view(self, view_id: str, payload: Mapping[str, Any]) -> JsonObject:
        return self.request("PATCH", f"/views/{view_id}", json_body=payload)

    def list_block_children(self, block_id: str) -> list[JsonObject]:
        return list(self.paginate("GET", f"/blocks/{block_id}/children", params={"page_size": 100}))

    def append_block_children(
        self,
        block_id: str,
        children: Sequence[Mapping[str, Any]],
    ) -> list[JsonObject]:
        """Append any number of blocks in Notion's maximum batches of 100."""
        created: list[JsonObject] = []
        for index in range(0, len(children), NOTION_CHILDREN_LIMIT):
            batch = [dict(child) for child in children[index : index + NOTION_CHILDREN_LIMIT]]
            response = self.request(
                "PATCH",
                f"/blocks/{block_id}/children",
                json_body={
                    "children": batch,
                    "position": {"type": "end"},
                },
            )
            results = response.get("results", [])
            if isinstance(results, list):
                created.extend(result for result in results if isinstance(result, dict))
        return created

    def update_block(
        self,
        block_id: str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        """Update the managed body of one existing block."""
        return self.request(
            "PATCH",
            f"/blocks/{block_id}",
            json_body=payload,
        )

    def delete_block(self, block_id: str) -> JsonObject:
        """Archive one precisely identified block and its managed subtree."""
        return self.request("DELETE", f"/blocks/{block_id}")

    def upload_file(
        self,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> str:
        """Direct-upload a file up to 20 MiB and return its file_upload ID."""
        if not filename or "/" in filename or "\\" in filename:
            raise ValueError("filename must be a non-empty basename")
        if not content_type:
            raise ValueError("content_type cannot be empty")
        if not content:
            raise ValueError("file content cannot be empty")
        if len(content) > NOTION_SINGLE_UPLOAD_LIMIT:
            raise ValueError("single-part Notion upload cannot exceed 20 MiB")
        created = self.request(
            "POST",
            "/file_uploads",
            json_body={
                "mode": "single_part",
                "filename": filename,
                "content_type": content_type,
            },
        )
        upload_id = created.get("id")
        if not upload_id:
            raise NotionAPIError("Notion file upload response has no id")
        url = f"{self.base_url}/file_uploads/{upload_id}/send"
        validate_credential_destination(url, CredentialKind.NOTION)
        headers = {
            key: value for key, value in self._headers.items() if key.lower() != "content-type"
        }
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.post(
                    url,
                    headers=headers,
                    files={"file": (filename, content, content_type)},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise NotionAPIError(
                        f"Notion file upload transport failure: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
            else:
                if response.status_code not in self._retryable_statuses:
                    uploaded = self._decode_response(response)
                    if uploaded.get("status") != "uploaded":
                        raise NotionAPIError("Notion file upload did not reach uploaded status")
                    return str(upload_id)
                if attempt >= self.max_retries:
                    self._raise_response_error(response, retryable=True)
            self._sleep(self._retry_delay(response, attempt))
        raise AssertionError("file upload retry loop exhausted unexpectedly")
