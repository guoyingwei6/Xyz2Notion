import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from xyz2notion.security import UnsafeCredentialDestinationError
from xyz2notion.xiaoyuzhou.client import (
    APPLICATION_ID,
    XiaoyuzhouAPIError,
    XiaoyuzhouClient,
)

FIXTURES = Path(__file__).parent / "fixtures" / "xiaoyuzhou"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def client_for(
    handler: httpx.MockTransport,
    *,
    sleeps: list[float] | None = None,
    max_retries: int = 2,
    max_pages: int = 10,
) -> XiaoyuzhouClient:
    return XiaoyuzhouClient(
        "refresh-fixture-secret",
        "11111111-2222-4333-8444-555555555555",
        client=httpx.Client(transport=handler),
        max_retries=max_retries,
        max_pages=max_pages,
        sleep=(sleeps if sleeps is not None else []).append,
        jitter=lambda: 0,
    )


def auth_response(access: str = "access-fixture-secret") -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "X-Jike-Access-Token": access,
            "X-Jike-Refresh-Token": "rotated-refresh-fixture-secret",
        },
        json={"success": True},
    )


def test_auth_uses_refresh_only_and_api_uses_access_only() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app_auth_tokens.refresh":
            assert request.headers["X-Jike-Refresh-Token"] == "refresh-fixture-secret"
            assert "X-Jike-Access-Token" not in request.headers
            assert request.headers["X-Jike-Device-ID"] == ("11111111-2222-4333-8444-555555555555")
            assert request.headers["ApplicationId"] == APPLICATION_ID
            return auth_response()
        assert request.headers["X-Jike-Access-Token"] == "access-fixture-secret"
        assert "X-Jike-Refresh-Token" not in request.headers
        return httpx.Response(200, json={"data": {"uid": "user-fixture"}})

    client = client_for(httpx.MockTransport(handle))
    assert client.profile()["uid"] == "user-fixture"
    assert [request.url.path for request in requests] == [
        "/app_auth_tokens.refresh",
        "/v1/profile/get",
    ]


def test_rotated_refresh_token_stays_in_memory_for_next_refresh() -> None:
    refresh_headers: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app_auth_tokens.refresh":
            refresh_headers.append(request.headers["X-Jike-Refresh-Token"])
            return auth_response(f"access-{len(refresh_headers)}")
        if len(refresh_headers) == 1:
            return httpx.Response(401, json={"code": "TOKEN_EXPIRED"})
        return httpx.Response(200, json={"data": {"uid": "user-fixture"}})

    client = client_for(httpx.MockTransport(handle))
    assert client.profile()["uid"] == "user-fixture"
    assert refresh_headers == [
        "refresh-fixture-secret",
        "rotated-refresh-fixture-secret",
    ]


def test_auth_failure_refreshes_once_then_stops_without_leak() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/app_auth_tokens.refresh" and calls == 1:
            return auth_response()
        return httpx.Response(
            401,
            json={
                "code": "AUTHENTICATION_FAILED",
                "message": "X-Jike-Refresh-Token: should-not-leak",
            },
        )

    client = client_for(httpx.MockTransport(handle))
    with pytest.raises(XiaoyuzhouAPIError) as caught:
        client.profile()
    assert calls == 3
    assert caught.value.authentication_failed is True
    assert caught.value.retryable is False
    assert "should-not-leak" not in str(caught.value)
    assert "XIAOYUZHOU_REFRESH_TOKEN" in str(caught.value)


def test_refresh_rejects_missing_token_response_and_unsafe_base_url() -> None:
    client = client_for(
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
    )
    with pytest.raises(XiaoyuzhouAPIError, match="returned no access token"):
        client.refresh_access_token()
    with pytest.raises(UnsafeCredentialDestinationError):
        XiaoyuzhouClient(
            "refresh-fixture-secret",
            "device",
            base_url="https://evil.example",
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        XiaoyuzhouClient("", "device")
    with pytest.raises(ValueError, match="cannot be empty"):
        XiaoyuzhouClient("refresh", "")
    with pytest.raises(ValueError, match="negative"):
        XiaoyuzhouClient("refresh", "device", max_retries=-1)
    with pytest.raises(ValueError, match="positive"):
        XiaoyuzhouClient("refresh", "device", max_pages=0)


def test_retry_after_and_transport_retries_are_bounded() -> None:
    sleeps: list[float] = []
    responses: Iterator[object] = iter(
        (
            httpx.ConnectError("offline"),
            httpx.Response(429, headers={"Retry-After": "2"}, json={"code": "SLOW"}),
            httpx.Response(200, json={"data": {"uid": "user-fixture"}}),
        )
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app_auth_tokens.refresh":
            return auth_response()
        item = next(responses)
        if isinstance(item, httpx.ConnectError):
            item.request = request
            raise item
        assert isinstance(item, httpx.Response)
        return item

    client = client_for(httpx.MockTransport(handle), sleeps=sleeps)
    assert client.profile()["uid"] == "user-fixture"
    assert sleeps == [1.0, 2.0]


def test_retry_exhaustion_is_safe() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app_auth_tokens.refresh":
            return auth_response()
        return httpx.Response(
            503,
            json={"message": "access_token=should-not-leak"},
        )

    client = client_for(httpx.MockTransport(handle), max_retries=0)
    with pytest.raises(XiaoyuzhouAPIError) as caught:
        client.profile()
    assert caught.value.retryable is True
    assert caught.value.status_code == 503
    assert "should-not-leak" not in str(caught.value)


def test_subscription_pagination_uses_cursor_and_sanitized_fixture() -> None:
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app_auth_tokens.refresh":
            return auth_response()
        body = json.loads(request.content)
        bodies.append(body)
        if "loadMoreKey" not in body:
            return httpx.Response(
                200,
                json={
                    "data": [{"pid": "podcast-fixture-0"}],
                    "loadMoreKey": {"cursor": "synthetic-cursor"},
                },
            )
        return httpx.Response(200, json=fixture("subscriptions-page.json"))

    client = client_for(httpx.MockTransport(handle))
    assert [item["pid"] for item in client.subscriptions()] == [
        "podcast-fixture-0",
        "podcast-fixture-1",
    ]
    assert bodies[1]["loadMoreKey"] == {"cursor": "synthetic-cursor"}
    serialized = json.dumps(fixture("subscriptions-page.json"))
    assert "nickname" not in serialized
    assert "uid" not in serialized


def test_all_read_endpoints_use_expected_contracts() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app_auth_tokens.refresh":
            return auth_response()
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/v1/mileage/list":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "podcast": {"pid": "podcast-fixture-1"},
                            "playedSeconds": 3600,
                        }
                    ],
                    "loadMoreKey": None,
                },
            )
        if request.url.path == "/v1/episode/list":
            return httpx.Response(
                200,
                json={"data": [{"eid": "episode-fixture-1"}], "loadMoreKey": None},
            )
        if request.url.path == "/v1/episode-played/list-history":
            return httpx.Response(200, json=fixture("history-page.json"))
        if request.url.path == "/v1/playback-progress/list":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "eid": eid,
                            "progress": 120,
                            "playedAt": "2026-01-01T00:00:00Z",
                        }
                        for eid in body["eids"]
                    ]
                },
            )
        if request.url.path == "/v1/profile/get":
            return httpx.Response(200, json={"data": {"uid": "user-fixture"}})
        if request.url.path == "/v1/monthly-wrapped/get":
            assert dict(request.url.params) == {
                "uid": "user-fixture",
                "year": "2026",
                "month": "7",
            }
            return httpx.Response(
                200,
                json={"data": {"playedDays": 7, "playedSeconds": 7200}},
            )
        raise AssertionError(request.url.path)

    client = client_for(httpx.MockTransport(handle))
    assert client.mileage()[0]["playedSeconds"] == 3600
    assert client.episodes("podcast-fixture-1")[0]["eid"] == "episode-fixture-1"
    assert client.play_history()[0]["episode"]["eid"] == "episode-fixture-1"
    assert len(client.playback_progress(["e1", "e1", "e2"], batch_size=1)) == 2
    assert client.profile()["uid"] == "user-fixture"
    assert client.monthly_wrapped(2026, 7)["playedSeconds"] == 7200
    assert (
        "POST",
        "/v1/episode/list",
        {"limit": 25, "pid": "podcast-fixture-1"},
    ) in requests


def test_validation_and_malformed_contracts() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"data": {}, "loadMoreKey": None}),
            httpx.Response(200, text="not-json"),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": None}),
        )
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app_auth_tokens.refresh":
            return auth_response()
        return next(responses)

    client = client_for(httpx.MockTransport(handle))
    with pytest.raises(XiaoyuzhouAPIError, match="invalid data"):
        client.subscriptions()
    with pytest.raises(XiaoyuzhouAPIError, match="non-JSON"):
        client.profile()
    with pytest.raises(XiaoyuzhouAPIError, match="non-object"):
        client.profile()
    with pytest.raises(XiaoyuzhouAPIError, match="invalid data"):
        client.profile()
    with pytest.raises(XiaoyuzhouAPIError, match="invalid data"):
        client.monthly_wrapped(2026, 1, uid="user-fixture")
    assert client.monthly_wrapped(2026, 1, uid="user-fixture") == {}
    with pytest.raises(ValueError, match="pid"):
        client.episodes("")
    with pytest.raises(ValueError, match="batch_size"):
        client.playback_progress(["e1"], batch_size=0)
    with pytest.raises(ValueError, match="month"):
        client.monthly_wrapped(2026, 13, uid="user-fixture")


def test_repeated_cursor_and_page_limit_stop_pagination() -> None:
    def repeated(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app_auth_tokens.refresh":
            return auth_response()
        return httpx.Response(
            200,
            json={"data": [], "loadMoreKey": "same-cursor"},
        )

    with pytest.raises(XiaoyuzhouAPIError, match="repeated cursor"):
        client_for(httpx.MockTransport(repeated)).subscriptions()

    cursor = 0

    def endless(request: httpx.Request) -> httpx.Response:
        nonlocal cursor
        if request.url.path == "/app_auth_tokens.refresh":
            return auth_response()
        cursor += 1
        return httpx.Response(200, json={"data": [], "loadMoreKey": cursor})

    with pytest.raises(XiaoyuzhouAPIError, match="safety limit"):
        client_for(httpx.MockTransport(endless), max_pages=2).subscriptions()
