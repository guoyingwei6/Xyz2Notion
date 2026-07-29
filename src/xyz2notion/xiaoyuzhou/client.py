"""Safe read-only client for Xiaoyuzhou's private mobile API."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import httpx
from pydantic import SecretStr

from xyz2notion.security import (
    CredentialKind,
    redact_text,
    validate_credential_destination,
)

XIAOYUZHOU_API_BASE_URL = "https://api.xiaoyuzhoufm.com"
APPLICATION_ID = "app.podcast.cosmos"

JsonObject = dict[str, Any]


class XiaoyuzhouAPIError(RuntimeError):
    """Credential-safe Xiaoyuzhou failure with an actionable category."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
        authentication_failed: bool = False,
    ) -> None:
        super().__init__(redact_text(message))
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.authentication_failed = authentication_failed


class XiaoyuzhouClient:
    """Synchronous read-only API client with one-shot token refresh."""

    _authentication_statuses = frozenset({401, 403})
    _retryable_statuses = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        refresh_token: str | SecretStr,
        device_id: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = XIAOYUZHOU_API_BASE_URL,
        max_retries: int = 3,
        max_pages: int = 200,
        timeout_seconds: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        token_value = (
            refresh_token.get_secret_value()
            if isinstance(refresh_token, SecretStr)
            else refresh_token
        )
        if not token_value:
            raise ValueError("Xiaoyuzhou refresh token cannot be empty")
        if not device_id:
            raise ValueError("Xiaoyuzhou device ID cannot be empty")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")

        self.base_url = base_url.rstrip("/")
        validate_credential_destination(self.base_url, CredentialKind.XIAOYUZHOU)
        self.device_id = device_id
        self.max_retries = max_retries
        self.max_pages = max_pages
        self._sleep = sleep
        self._jitter = jitter
        self._refresh_token = SecretStr(token_value)
        self._access_token: SecretStr | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        """Close an internally-owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> XiaoyuzhouClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _base_headers(self) -> dict[str, str]:
        return {
            "ApplicationId": APPLICATION_ID,
            "X-Jike-Device-ID": self.device_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Xyz2Notion/0.1",
        }

    def _refresh_headers(self) -> dict[str, str]:
        headers = self._base_headers()
        headers["X-Jike-Refresh-Token"] = self._refresh_token.get_secret_value()
        return headers

    def _access_headers(self) -> dict[str, str]:
        if self._access_token is None:
            self.refresh_access_token()
        if self._access_token is None:
            raise AssertionError("token refresh did not set access token")
        headers = self._base_headers()
        headers["X-Jike-Access-Token"] = self._access_token.get_secret_value()
        return headers

    def _url(self, path: str) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        validate_credential_destination(url, CredentialKind.XIAOYUZHOU)
        return url

    def _backoff(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass
        return min(30.0, 2.0**attempt) + self._jitter()

    @staticmethod
    def _token_from_response(response: httpx.Response, name: str) -> str | None:
        header_value = response.headers.get(name)
        if header_value:
            return str(header_value)
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        direct = payload.get(name) or payload.get(name.lower())
        if direct:
            return str(direct)
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get(name) or data.get(name.lower())
            if nested:
                return str(nested)
        return None

    def refresh_access_token(self) -> None:
        """Exchange the long-lived secret for in-memory access credentials."""
        url = self._url("/app_auth_tokens.refresh")
        try:
            response = self._client.post(url, headers=self._refresh_headers())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise XiaoyuzhouAPIError(
                f"Xiaoyuzhou authentication transport failure: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.is_error:
            self._raise_response_error(response, refreshing=True)

        access_token = self._token_from_response(response, "X-Jike-Access-Token")
        rotated_refresh = self._token_from_response(response, "X-Jike-Refresh-Token")
        if not access_token:
            raise XiaoyuzhouAPIError(
                "Xiaoyuzhou authentication succeeded but returned no access token; "
                "capture a new X-Jike-Refresh-Token and update the GitHub Secret",
                authentication_failed=True,
            )
        self._access_token = SecretStr(access_token)
        if rotated_refresh:
            # A rotated token remains memory-only for the rest of this workflow run.
            self._refresh_token = SecretStr(rotated_refresh)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        """Perform an authenticated request and refresh authentication once."""
        url = self._url(path)
        refreshed_after_failure = False
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=self._access_headers(),
                    params=params,
                    json=json_body,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise XiaoyuzhouAPIError(
                        f"Xiaoyuzhou transport failure: {type(exc).__name__}",
                        retryable=True,
                    ) from exc
                self._sleep(self._backoff(None, attempt))
                attempt += 1
                continue

            if response.status_code in self._authentication_statuses:
                if refreshed_after_failure:
                    self._raise_response_error(response, refreshing=True)
                self._access_token = None
                self.refresh_access_token()
                refreshed_after_failure = True
                continue
            if response.status_code in self._retryable_statuses:
                if attempt >= self.max_retries:
                    self._raise_response_error(response)
                self._sleep(self._backoff(response, attempt))
                attempt += 1
                continue
            return self._decode_response(response)

    @staticmethod
    def _decode_response(response: httpx.Response) -> JsonObject:
        if response.is_error:
            XiaoyuzhouClient._raise_response_error(response)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise XiaoyuzhouAPIError(
                "Xiaoyuzhou returned a non-JSON response",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise XiaoyuzhouAPIError(
                "Xiaoyuzhou returned a non-object JSON response",
                status_code=response.status_code,
            )
        return payload

    @staticmethod
    def _raise_response_error(
        response: httpx.Response,
        *,
        refreshing: bool = False,
    ) -> None:
        code: str | None = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                raw_code = payload.get("code") or payload.get("errorCode")
                code = str(raw_code) if raw_code is not None else None
        except ValueError:
            pass
        authentication_failed = (
            refreshing or response.status_code in XiaoyuzhouClient._authentication_statuses
        )
        if authentication_failed:
            message = (
                "Xiaoyuzhou authentication failed; capture a new "
                "X-Jike-Refresh-Token and update XIAOYUZHOU_REFRESH_TOKEN"
            )
        else:
            message = f"Xiaoyuzhou request failed with HTTP {response.status_code}"
        raise XiaoyuzhouAPIError(
            message,
            status_code=response.status_code,
            code=code,
            retryable=response.status_code in XiaoyuzhouClient._retryable_statuses,
            authentication_failed=authentication_failed,
        )

    def _paginate(
        self,
        path: str,
        body: Mapping[str, Any],
    ) -> Iterator[JsonObject]:
        cursor: object | None = None
        seen_cursors: set[str] = set()
        for _page_number in range(self.max_pages):
            request_body = dict(body)
            if cursor is not None:
                request_body["loadMoreKey"] = cursor
            page = self.request("POST", path, json_body=request_body)
            data = page.get("data", [])
            if not isinstance(data, list):
                raise XiaoyuzhouAPIError("Xiaoyuzhou list response has invalid data")
            for item in data:
                if not isinstance(item, dict):
                    raise XiaoyuzhouAPIError("Xiaoyuzhou list response contains a non-object item")
                yield item
            next_cursor = page.get("loadMoreKey")
            if next_cursor is None:
                return
            cursor_key = repr(next_cursor)
            if cursor_key in seen_cursors:
                raise XiaoyuzhouAPIError("Xiaoyuzhou pagination returned a repeated cursor")
            seen_cursors.add(cursor_key)
            cursor = next_cursor
        raise XiaoyuzhouAPIError(
            f"Xiaoyuzhou pagination exceeded the safety limit of {self.max_pages} pages"
        )

    def subscriptions(self, *, limit: int = 25) -> list[JsonObject]:
        """Return every subscribed podcast."""
        return list(
            self._paginate(
                "/v1/subscription/list",
                {"limit": limit, "sortBy": "subscribedAt", "sortOrder": "desc"},
            )
        )

    def mileage(self, *, rank: str = "TOTAL") -> list[JsonObject]:
        """Return podcasts with cumulative listening seconds."""
        return list(self._paginate("/v1/mileage/list", {"rank": rank}))

    def podcast(self, pid: str) -> JsonObject:
        """Return one podcast, used when history references an unsubscribed show."""
        if not pid:
            raise ValueError("pid cannot be empty")
        payload = self.request("GET", "/v1/podcast/get", params={"pid": pid})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise XiaoyuzhouAPIError("Xiaoyuzhou podcast response has invalid data")
        return data

    def episodes(self, pid: str, *, limit: int = 25) -> list[JsonObject]:
        """Return every episode currently exposed for a podcast."""
        if not pid:
            raise ValueError("pid cannot be empty")
        return list(self._paginate("/v1/episode/list", {"limit": limit, "pid": pid}))

    def play_history(self, *, limit: int = 25) -> list[JsonObject]:
        """Return played-history wrapper objects in API order."""
        return list(self._paginate("/v1/episode-played/list-history", {"limit": limit}))

    def playback_progress(
        self,
        eids: Sequence[str],
        *,
        batch_size: int = 100,
    ) -> list[JsonObject]:
        """Return playback progress in bounded batches."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        unique_eids = tuple(dict.fromkeys(eid for eid in eids if eid))
        results: list[JsonObject] = []
        for offset in range(0, len(unique_eids), batch_size):
            payload = self.request(
                "POST",
                "/v1/playback-progress/list",
                json_body={"eids": list(unique_eids[offset : offset + batch_size])},
            )
            data = payload.get("data", [])
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise XiaoyuzhouAPIError("Xiaoyuzhou playback-progress response has invalid data")
            results.extend(data)
        return results

    def profile(self) -> JsonObject:
        """Return the authenticated user's profile object."""
        payload = self.request("GET", "/v1/profile/get")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise XiaoyuzhouAPIError("Xiaoyuzhou profile response has invalid data")
        return data

    def monthly_wrapped(
        self,
        year: int,
        month: int,
        *,
        uid: str | None = None,
    ) -> JsonObject:
        """Return historical monthly listening totals."""
        if not 1 <= month <= 12:
            raise ValueError("month must be between 1 and 12")
        profile_uid = uid or str(self.profile().get("uid", ""))
        if not profile_uid:
            raise XiaoyuzhouAPIError("Xiaoyuzhou profile does not contain uid")
        payload = self.request(
            "GET",
            "/v1/monthly-wrapped/get",
            params={"uid": profile_uid, "year": year, "month": month},
        )
        data = payload.get("data")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise XiaoyuzhouAPIError("Xiaoyuzhou monthly-wrapped response has invalid data")
        return data
