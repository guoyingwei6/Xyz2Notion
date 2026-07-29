from collections.abc import Iterator

import httpx
import pytest

from xyz2notion.notion.client import (
    NOTION_API_VERSION,
    NotionAPIError,
    NotionClient,
    paragraph_blocks,
    rich_text,
    split_text,
)
from xyz2notion.security import UnsafeCredentialDestinationError


def client_for(
    handler: httpx.MockTransport,
    *,
    sleeps: list[float] | None = None,
    max_retries: int = 2,
) -> NotionClient:
    return NotionClient(
        "notion-example",
        client=httpx.Client(transport=handler),
        sleep=(sleeps if sleeps is not None else []).append,
        jitter=lambda: 0,
        max_retries=max_retries,
    )


def test_request_sets_current_version_and_auth_header() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Notion-Version"] == NOTION_API_VERSION
        assert request.headers["Authorization"] == "Bearer notion-example"
        assert request.url.host == "api.notion.com"
        return httpx.Response(200, json={"object": "page", "id": "page-1"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = NotionClient("notion-example", client=http)
        assert client.retrieve_page("page-1")["id"] == "page-1"
        client.close()


def test_create_data_source_page_uses_2026_parent_contract() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        assert request.method == "POST"
        assert request.url.path == "/v1/pages"
        assert '"type":"data_source_id"' in payload
        assert '"data_source_id":"ds-1"' in payload
        assert '"Name"' in payload
        return httpx.Response(200, json={"object": "page", "id": "row-1"})

    client = client_for(httpx.MockTransport(handle))
    created = client.create_data_source_page(
        "ds-1",
        {"Name": {"title": rich_text("Row")}},
    )
    assert created["id"] == "row-1"


def test_search_databases_resolves_data_source_parents() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/search":
            payload = request.read().decode()
            assert request.method == "POST"
            assert '"value":"data_source"' in payload
            assert '"value":"database"' not in payload
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object": "data_source",
                            "id": "ds-1",
                            "parent": {"type": "database_id", "database_id": "db-1"},
                        },
                        {
                            "object": "data_source",
                            "id": "ds-2",
                            "parent": {"type": "database_id", "database_id": "db-1"},
                        },
                        {"object": "data_source", "id": "orphan"},
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        assert request.method == "GET"
        assert request.url.path == "/v1/databases/db-1"
        return httpx.Response(
            200,
            json={"object": "database", "id": "db-1", "data_sources": [{"id": "ds-1"}]},
        )

    client = client_for(httpx.MockTransport(handle))
    assert [database["id"] for database in client.search_databases("播客")] == ["db-1"]
    assert [request.url.path for request in requests] == ["/v1/search", "/v1/databases/db-1"]


def test_direct_file_upload_creates_sends_and_returns_id() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/file_uploads":
            return httpx.Response(
                200,
                json={
                    "object": "file_upload",
                    "id": "upload-1",
                    "status": "pending",
                },
            )
        assert request.url.path == "/v1/file_uploads/upload-1/send"
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert b"heatmap.png" in request.content
        assert b"png-bytes" in request.content
        return httpx.Response(
            200,
            json={
                "object": "file_upload",
                "id": "upload-1",
                "status": "uploaded",
            },
        )

    client = client_for(httpx.MockTransport(handle))
    upload_id = client.upload_file("heatmap.png", "image/png", b"png-bytes")
    assert upload_id == "upload-1"
    assert len(requests) == 2


def test_direct_file_upload_validates_inputs() -> None:
    client = client_for(httpx.MockTransport(lambda _request: httpx.Response(500)))
    with pytest.raises(ValueError, match="basename"):
        client.upload_file("../file.png", "image/png", b"x")
    with pytest.raises(ValueError, match="content_type"):
        client.upload_file("file.png", "", b"x")
    with pytest.raises(ValueError, match="content"):
        client.upload_file("file.png", "image/png", b"")


def test_base_url_must_be_notion_allowlisted() -> None:
    with pytest.raises(UnsafeCredentialDestinationError):
        NotionClient("notion-example", base_url="https://evil.example/v1")
    with pytest.raises(ValueError, match="cannot be empty"):
        NotionClient("")
    with pytest.raises(ValueError, match="negative"):
        NotionClient("notion-example", max_retries=-1)


def test_retry_after_header_is_honored() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2.5"},
                json={"code": "rate_limited", "message": "slow down"},
            )
        return httpx.Response(200, json={"ok": True})

    client = client_for(httpx.MockTransport(handle), sleeps=sleeps)
    assert client.request("GET", "/users/me") == {"ok": True}
    assert attempts == 2
    assert sleeps == [2.5]


def test_server_errors_use_exponential_backoff() -> None:
    sleeps: list[float] = []
    responses = iter(
        (
            httpx.Response(503, json={"code": "unavailable", "message": "try later"}),
            httpx.Response(502, json={"code": "gateway", "message": "try later"}),
            httpx.Response(200, json={"ok": True}),
        )
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = client_for(httpx.MockTransport(handle), sleeps=sleeps)
    assert client.request("GET", "/users/me") == {"ok": True}
    assert sleeps == [1.0, 2.0]


def test_notion_529_is_retryable() -> None:
    responses = iter(
        (
            httpx.Response(529, json={"code": "overloaded", "message": "try later"}),
            httpx.Response(200, json={"ok": True}),
        )
    )
    client = client_for(httpx.MockTransport(lambda _request: next(responses)))
    assert client.request("GET", "/users/me") == {"ok": True}


def test_transport_error_retries_then_returns() -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"ok": True})

    client = client_for(httpx.MockTransport(handle))
    assert client.request("GET", "/users/me") == {"ok": True}


def test_retry_exhaustion_returns_safe_error() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "code": "service_unavailable",
                "message": "Authorization: Bearer should-not-leak",
            },
        )

    client = client_for(httpx.MockTransport(handle), max_retries=0)
    with pytest.raises(NotionAPIError) as caught:
        client.request("GET", "/users/me")
    assert caught.value.retryable is True
    assert caught.value.status_code == 503
    assert caught.value.code == "service_unavailable"
    assert "should-not-leak" not in str(caught.value)


def test_non_retryable_and_invalid_responses_are_safe() -> None:
    responses: Iterator[httpx.Response] = iter(
        (
            httpx.Response(400, json={"code": "bad_request", "message": "bad input"}),
            httpx.Response(200, text="not-json"),
            httpx.Response(200, json=["not", "an", "object"]),
        )
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = client_for(httpx.MockTransport(handle))
    with pytest.raises(NotionAPIError, match="bad input") as bad:
        client.request("GET", "/one")
    assert bad.value.retryable is False
    with pytest.raises(NotionAPIError, match="non-JSON"):
        client.request("GET", "/two")
    with pytest.raises(NotionAPIError, match="non-object"):
        client.request("GET", "/three")


def test_get_and_post_pagination_follow_cursor() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        cursor = request.url.params.get("start_cursor")
        if request.method == "POST":
            body = request.read().decode()
            cursor = "cursor-1" if '"start_cursor":"cursor-1"' in body else None
        if cursor:
            return httpx.Response(
                200,
                json={"results": [{"id": "two"}], "has_more": False, "next_cursor": None},
            )
        return httpx.Response(
            200,
            json={
                "results": [{"id": "one"}],
                "has_more": True,
                "next_cursor": "cursor-1",
            },
        )

    client = client_for(httpx.MockTransport(handle))
    assert [item["id"] for item in client.paginate("GET", "/views")] == ["one", "two"]
    assert [item["id"] for item in client.paginate("POST", "/search")] == ["one", "two"]
    assert len(requests) == 4


def test_pagination_rejects_malformed_lists() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"results": {}, "has_more": False}),
            httpx.Response(200, json={"results": ["bad"], "has_more": False}),
            httpx.Response(200, json={"results": [], "has_more": True}),
        )
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = client_for(httpx.MockTransport(handle))
    with pytest.raises(NotionAPIError, match="invalid results"):
        list(client.paginate("GET", "/one"))
    with pytest.raises(NotionAPIError, match="non-object"):
        list(client.paginate("GET", "/two"))
    with pytest.raises(NotionAPIError, match="without next_cursor"):
        list(client.paginate("GET", "/three"))


def test_long_text_helpers_preserve_content() -> None:
    text = "播" * 4501
    chunks = split_text(text)
    assert [len(chunk) for chunk in chunks] == [2000, 2000, 501]
    assert "".join(chunks) == text
    objects = rich_text(text)
    assert "".join(item["text"]["content"] for item in objects) == text
    blocks = paragraph_blocks(text)
    assert len(blocks) == 3
    assert split_text("") == [""]
    assert rich_text("") == []
    emoji_chunks = split_text("😀" * 1001)
    assert [len(chunk.encode("utf-16-le")) // 2 for chunk in emoji_chunks] == [2000, 2]
    assert "".join(emoji_chunks) == "😀" * 1001
    with pytest.raises(ValueError, match="positive"):
        split_text("text", 0)
    with pytest.raises(ValueError, match="cannot fit"):
        split_text("😀", 1)


def test_append_children_uses_batches_of_100_and_position_end() -> None:
    batch_sizes: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        batch_sizes.append(payload.count('"object":"block"'))
        assert '"position":{"type":"end"}' in payload
        return httpx.Response(200, json={"results": [{"id": str(len(batch_sizes))}]})

    client = client_for(httpx.MockTransport(handle))
    children = [{"object": "block", "type": "divider", "divider": {}} for _ in range(205)]
    created = client.append_block_children("page-1", children)
    assert batch_sizes == [100, 100, 5]
    assert [item["id"] for item in created] == ["1", "2", "3"]


def test_resource_helpers_use_2026_endpoints() -> None:
    calls: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/databases"):
            return httpx.Response(
                200,
                json={"id": "db", "data_sources": [{"id": "ds"}]},
            )
        if request.url.path.endswith("/views"):
            if request.method == "GET":
                return httpx.Response(200, json={"results": [], "has_more": False})
            return httpx.Response(200, json={"id": "view"})
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        return httpx.Response(200, json={"id": "resource", "properties": {}})

    client = client_for(httpx.MockTransport(handle))
    client.create_page("root", "Data", icon="📦")
    client.update_page("root", {"icon": {"type": "emoji", "emoji": "🎧"}})
    client.create_database("root", "Podcast", {"Name": {"title": {}}}, icon="🎧")
    client.retrieve_database("db")
    client.retrieve_data_source("ds")
    client.update_data_source("ds", {"PID": {"rich_text": {}}})
    client.query_data_source("ds")
    client.list_views(database_id="db")
    client.retrieve_view("view")
    client.create_view({"database_id": "db", "data_source_id": "ds", "name": "All"})
    client.update_view("view", {"name": "Updated"})
    client.list_block_children("root")
    client.delete_block("managed")
    assert ("POST", "/v1/pages") in calls
    assert ("POST", "/v1/databases") in calls
    assert ("POST", "/v1/data_sources/ds/query") in calls
    assert ("DELETE", "/v1/blocks/managed") in calls
    with pytest.raises(ValueError, match="required"):
        client.list_views()
