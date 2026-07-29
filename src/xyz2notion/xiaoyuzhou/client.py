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
    """Synchronous read-only API client with strict anti-abuse limits."""

    _authentication_statuses = frozenset({401, 403})
    _circuit_breaker_statuses = frozenset({401, 403, 429})
    _retryable_statuses = frozenset({500, 502, 503, 504})

    def __init__(
        self,
        refresh_token: str | SecretStr,
        device_id: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = XIAOYUZHOU_API_BASE_URL,
        max_retries: int = 1,
        max_pages: int = 1,
        max_requests_per_run: int = 20,
        min_request_interval_seconds: float = 3.0,
        timeout_seconds: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
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
        if max_requests_per_run < 1:
            raise ValueError("max_requests_per_run must be positive")
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds cannot be negative")

        self.base_url = base_url.rstrip("/")
        validate_credential_destination(self.base_url, CredentialKind.XIAOYUZHOU)
        self.device_id = device_id
        self.max_retries = max_retries
        self.max_pages = max_pages
        self.max_requests_per_run = max_requests_per_run
        self.min_request_interval_seconds = min_request_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter
        self._request_count = 0
        self._last_request_started_at: float | None = None
        self._circuit_open = False
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

    def _before_outbound_request(self) -> None:
        """Apply one shared budget and minimum interval to every HTTP request."""
        if self._circuit_open:
            raise XiaoyuzhouAPIError(
                "Xiaoyuzhou safety circuit is open; stop this run and retry manually later"
            )
        if self._request_count >= self.max_requests_per_run:
            raise XiaoyuzhouAPIError(
                "Xiaoyuzhou request budget exhausted; stopped before sending another request"
            )
        now = self._monotonic()
        if self._last_request_started_at is not None:
            remaining = self.min_request_interval_seconds - (now - self._last_request_started_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._request_count += 1
        self._last_request_started_at = now

    def _trip_circuit(self) -> None:
        self._circuit_open = True

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
            self._before_outbound_request()
            response = self._client.post(url, headers=self._refresh_headers())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise XiaoyuzhouAPIError(
                f"Xiaoyuzhou authentication transport failure: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.is_error:
            if response.status_code in self._circuit_breaker_statuses:
                self._trip_circuit()
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
        """Perform an authenticated request within the run-wide safety budget."""
        url = self._url(path)
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                headers = self._access_headers()
                self._before_outbound_request()
                response = self._client.request(
                    method,
                    url,
                    headers=headers,
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

            if response.status_code in self._circuit_breaker_statuses:
                self._trip_circuit()
                self._raise_response_error(
                    response,
                    refreshing=response.status_code in self._authentication_statuses,
                )
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
        *,
        max_items: int = 25,
    ) -> Iterator[JsonObject]:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        cursor: object | None = None
        seen_cursors: set[str] = set()
        emitted = 0
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
                emitted += 1
                if emitted >= max_items:
                    return
            next_cursor = page.get("loadMoreKey")
            if next_cursor is None:
                return
            cursor_key = repr(next_cursor)
            if cursor_key in seen_cursors:
                raise XiaoyuzhouAPIError("Xiaoyuzhou pagination returned a repeated cursor")
            seen_cursors.add(cursor_key)
            cursor = next_cursor
        return

    def subscriptions(self, *, limit: int = 25) -> list[JsonObject]:
        """Return at most the newest 25 subscribed podcasts."""
        safe_limit = min(max(1, limit), 25)
        return list(
            self._paginate(
                "/v1/subscription/list",
                {"limit": safe_limit, "sortBy": "subscribedAt", "sortOrder": "desc"},
                max_items=25,
            )
        )

    def mileage(self, *, rank: str = "TOTAL") -> list[JsonObject]:
        """Return at most 25 podcast mileage rows."""
        return list(self._paginate("/v1/mileage/list", {"rank": rank}, max_items=25))

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
        """Return at most the newest 25 episodes for a podcast."""
        if not pid:
            raise ValueError("pid cannot be empty")
        safe_limit = min(max(1, limit), 25)
        return list(
            self._paginate(
                "/v1/episode/list",
                {"limit": safe_limit, "pid": pid},
                max_items=25,
            )
        )

    def episode(self, eid: str) -> JsonObject:
        """Return one episode by EID."""
        if not eid:
            raise ValueError("eid cannot be empty")
        payload = self.request("GET", "/v1/episode/get", params={"eid": eid})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise XiaoyuzhouAPIError("Xiaoyuzhou episode response has invalid data")
        return data

    def play_history(self, *, limit: int = 25) -> list[JsonObject]:
        """Return at most the newest 25 played-history rows."""
        safe_limit = min(max(1, limit), 25)
        return list(
            self._paginate(
                "/v1/episode-played/list-history",
                {"limit": safe_limit},
                max_items=25,
            )
        )

    def playlist_eids(self) -> list[str]:
        """Return the authenticated user's ordered listen-later playlist."""
        payload = self.request("POST", "/v1/playlist/pull", json_body={})
        data = payload.get("data")
        items = data.get("list") if isinstance(data, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise XiaoyuzhouAPIError("Xiaoyuzhou playlist response has invalid data")
        return list(dict.fromkeys(item for item in items if item))[:25]

    def favorites(self) -> list[JsonObject]:
        """Return at most the newest 25 episode bookmarks."""
        return list(self._paginate("/v1/favorite/list", {}, max_items=25))

    def playback_progress(
        self,
        eids: Sequence[str],
        *,
        batch_size: int = 25,
    ) -> list[JsonObject]:
        """Return playback progress in bounded batches."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        safe_batch_size = min(batch_size, 25)
        unique_eids = tuple(dict.fromkeys(eid for eid in eids if eid))[:25]
        results: list[JsonObject] = []
        for offset in range(0, len(unique_eids), safe_batch_size):
            payload = self.request(
                "POST",
                "/v1/playback-progress/list",
                json_body={"eids": list(unique_eids[offset : offset + safe_batch_size])},
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
