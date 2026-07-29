from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from xyz2notion.notion.client import JsonObject
from xyz2notion.notion.cover_localizer import NotionCoverLocalizer


class FakeCoverAPI:
    def __init__(self, pages: list[JsonObject]) -> None:
        self.pages = pages
        self.uploads: list[tuple[str, str, bytes]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def query_data_source_page(
        self,
        _data_source_id: str,
        _payload: Mapping[str, Any] | None = None,
    ) -> list[JsonObject]:
        return self.pages

    def upload_file(self, filename: str, content_type: str, content: bytes) -> str:
        self.uploads.append((filename, content_type, content))
        return "upload-1"

    def update_page(self, page_id: str, payload: Mapping[str, Any]) -> JsonObject:
        self.updates.append((page_id, dict(payload)))
        return {"id": page_id}


def external_page(page_id: str = "page") -> JsonObject:
    return {
        "id": page_id,
        "properties": {
            "Cover": {
                "files": [
                    {
                        "type": "external",
                        "external": {"url": "https://cdn.example/cover.jpg"},
                    }
                ]
            }
        },
    }


def test_cover_localizer_uploads_and_repoints_cover_icon_and_property() -> None:
    api = FakeCoverAPI([external_page()])
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg"},
            content=b"jpeg",
        )
    )
    with (
        httpx.Client(transport=transport) as http,
        NotionCoverLocalizer(api, ("podcasts",), http_client=http) as localizer,
    ):
        report = localizer.repair(limit=1)
    assert report.repaired == 1
    assert report.failed == 0
    assert api.uploads[0][1:] == ("image/jpeg", b"jpeg")
    payload = api.updates[0][1]
    assert payload["icon"]["type"] == "file_upload"
    assert payload["cover"]["file_upload"]["id"] == "upload-1"
    assert (
        payload["properties"]["Cover"]["files"][0]["file_upload"]["id"]  # type: ignore[index]
        == "upload-1"
    )


def test_cover_localizer_is_capped_and_skips_non_external_rows() -> None:
    uploaded = {
        "id": "already-uploaded",
        "properties": {
            "Cover": {"files": [{"type": "file", "file": {"url": "https://notion.example/file"}}]}
        },
    }
    api = FakeCoverAPI([uploaded, external_page("one"), external_page("two")])
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "image/png"},
            content=b"png",
        )
    )
    with (
        httpx.Client(transport=transport) as http,
        NotionCoverLocalizer(api, ("podcasts",), http_client=http) as localizer,
    ):
        report = localizer.repair(limit=1)
    assert report.repaired == 1
    assert report.skipped == 1
    assert len(api.updates) == 1


def test_cover_localizer_follows_redirect_then_safely_counts_invalid_image() -> None:
    api = FakeCoverAPI([external_page()])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("cover.jpg"):
            return httpx.Response(302, headers={"Location": "/not-an-image"})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"no",
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        NotionCoverLocalizer(api, ("podcasts",), http_client=http) as localizer,
    ):
        report = localizer.repair(limit=1)
    assert report.failed == 1
    assert report.repaired == 0
    assert api.uploads == []


@pytest.mark.parametrize(
    ("response", "expected_failed"),
    [
        (httpx.Response(404), 1),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "image/png", "Content-Length": "99999999"},
                content=b"x",
            ),
            1,
        ),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                content=b"",
            ),
            1,
        ),
    ],
)
def test_cover_localizer_counts_bounded_download_failures(
    response: httpx.Response,
    expected_failed: int,
) -> None:
    api = FakeCoverAPI([external_page()])
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: response)) as http,
        NotionCoverLocalizer(api, ("podcasts",), http_client=http) as localizer,
    ):
        assert localizer.repair(limit=1).failed == expected_failed


def test_cover_localizer_rejects_unsafe_limit_and_skips_malformed_rows() -> None:
    api = FakeCoverAPI(
        [
            {},
            {"id": "missing-properties"},
            {"id": "missing-cover", "properties": {}},
            {"id": "wrong-cover", "properties": {"Cover": []}},
            {"id": "empty-files", "properties": {"Cover": {"files": []}}},
            {"id": "wrong-file", "properties": {"Cover": {"files": ["bad"]}}},
            {
                "id": "missing-url",
                "properties": {
                    "Cover": {
                        "files": [{"type": "external", "external": {}}],
                    }
                },
            },
        ]
    )
    with NotionCoverLocalizer(api, ("podcasts",)) as localizer:
        with pytest.raises(ValueError, match="between 1 and 10"):
            localizer.repair(limit=11)
        report = localizer.repair(limit=1)
    assert report.skipped == 7
