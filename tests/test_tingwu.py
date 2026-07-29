import json
from collections.abc import Callable

import httpx
import pytest

from xyz2notion.asr.router import (
    run_with_tingwu_fallback,
    tingwu_fallback_allowed,
)
from xyz2notion.asr.tingwu import (
    DIRECTORY_ADD_URL,
    DIRECTORY_LIST_URL,
    LAB_URL,
    NOTE_URL,
    PARSE_SOURCE_URL,
    QUERY_SOURCE_URL,
    RECORD_LIST_URL,
    RECORD_START_URL,
    TRANSCRIPT_URL,
    TingwuClient,
    TingwuTaskState,
)
from xyz2notion.models import (
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptTimingQuality,
)

COOKIE = "fixture_session=secret-cookie-value"


def ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 0,
    sleeps: list[float] | None = None,
) -> TingwuClient:
    return TingwuClient(
        COOKIE,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=max_retries,
        sleep=(sleeps if sleeps is not None else []).append,
        jitter=lambda: 0,
    )


def test_complete_submission_result_and_idempotency_contract() -> None:
    directory_created = False
    submitted = False
    seen_hosts: set[str] = set()

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal directory_created, submitted
        seen_hosts.add(request.url.host)
        assert request.headers["Cookie"] == COOKIE
        assert request.headers["X-Tw-From"] == "tongyi"
        url = str(request.url)
        payload = json.loads(request.content) if request.content else {}
        if url == DIRECTORY_LIST_URL:
            data = [{"dir": {"dirName": "播客", "idStr": "dir-1"}}] if directory_created else []
            return ok(data)
        if url == DIRECTORY_ADD_URL:
            assert payload == {"dirName": "播客", "parentIdStr": -1}
            directory_created = True
            return ok({"focusDir": {"idStr": "dir-1"}})
        if url == RECORD_LIST_URL:
            records = (
                [{"showName": "单集", "genRecordId": "record-1", "status": 30}] if submitted else []
            )
            return ok({"batchRecord": [{"recordList": records}]})
        if url == PARSE_SOURCE_URL:
            assert payload["url"] == "https://cdn.example/episode.mp3"
            return ok({"taskId": "source-1"})
        if url == QUERY_SOURCE_URL:
            return ok({"status": 0, "urls": [{"fileId": "file-1", "size": 123}]})
        if url == RECORD_START_URL:
            assert payload["files"][0]["tag"]["showName"] == "单集"
            submitted = True
            return ok({"genRecordIdList": ["record-1"]})
        if url == TRANSCRIPT_URL:
            return ok(
                {
                    "tag": {
                        "identify": json.dumps(
                            {"user_info": {"1": {"name": "主播"}}},
                            ensure_ascii=False,
                        )
                    },
                    "result": json.dumps(
                        {
                            "pg": [
                                {
                                    "ui": 1,
                                    "sc": [
                                        {"bt": 0, "tc": "第一句。"},
                                        {"bt": 1200, "tc": "第二句。"},
                                    ],
                                },
                                {"ui": 2, "sc": [{"bt": 2500, "tc": "第三句。"}]},
                            ]
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        if url == LAB_URL:
            return ok(
                {
                    "labCardsMap": {
                        "labInfo": [
                            {
                                "basicInfo": {"name": "议程"},
                                "contents": [
                                    {
                                        "contentValues": [
                                            {"time": 0, "value": "开场", "summary": "介绍"},
                                            {"time": "bad", "value": "讨论"},
                                        ]
                                    }
                                ],
                            },
                            {
                                "basicInfo": {"name": "qa问答"},
                                "contents": [
                                    {
                                        "contentValues": [
                                            {"title": "问题?", "value": "回答。"},
                                            {"title": "", "value": ""},
                                        ]
                                    }
                                ],
                            },
                        ],
                        "labSummaryInfo": [
                            {
                                "basicInfo": {"name": "全文摘要"},
                                "contents": [{"contentValues": [{"value": "完整摘要"}]}],
                            },
                            {
                                "basicInfo": {"name": "思维导图"},
                                "contents": [
                                    {
                                        "contentValues": [
                                            {
                                                "json": json.dumps(
                                                    {"content": "主题", "children": []},
                                                    ensure_ascii=False,
                                                )
                                            }
                                        ]
                                    }
                                ],
                            },
                        ],
                    }
                }
            )
        if url == NOTE_URL:
            return ok({"content": json.dumps([["span", {}, "笔记"]], ensure_ascii=False)})
        raise AssertionError(url)

    client = client_for(handle)
    task = client.submit_episode(
        "播客",
        "单集",
        "https://cdn.example/episode.mp3",
    )
    assert task.state is TingwuTaskState.SUBMITTED
    assert task.provider_task_id == "record-1"
    assert task.source_task_id == "source-1"

    existing = client.submit_episode(
        "播客",
        "单集",
        "https://cdn.example/episode.mp3",
    )
    assert existing.state is TingwuTaskState.SUCCEEDED
    assert client.health_check() is True

    transcript = client.get_transcript(existing.provider_task_id)
    assert transcript.text == "第一句。\n第二句。\n第三句。"
    assert transcript.timing_quality is TranscriptTimingQuality.EXACT
    assert transcript.segments[0].speaker == "主播"
    assert transcript.segments[-1].speaker == "发言人2"
    assert transcript.segments[0].end_ms == 1200

    enrichment = client.get_enrichment(existing.provider_task_id)
    assert enrichment.summary == "完整摘要"
    assert [chapter.title for chapter in enrichment.chapters] == ["开场", "讨论"]
    assert enrichment.questions == ("问题?\n回答。",)
    assert enrichment.mindmap == {"content": "主题", "children": []}
    assert client.get_note(existing.provider_task_id).content == [["span", {}, "笔记"]]
    assert seen_hosts == {"qianwen.biz.aliyun.com", "tw-efficiency.biz.aliyun.com"}


def test_source_parse_checkpoint_resumes_without_duplicate_parse() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == DIRECTORY_LIST_URL:
            return ok([{"dir": {"dirName": "播客", "id": 9}}])
        if str(request.url) == RECORD_LIST_URL:
            return ok({"batchRecord": []})
        if str(request.url) == QUERY_SOURCE_URL:
            return ok({"status": -1})
        raise AssertionError(str(request.url))

    client = client_for(handle)
    task = client.submit_episode(
        "播客",
        "单集",
        "https://cdn.example/audio",
        source_task_id="source-existing",
    )
    assert task.state is TingwuTaskState.SOURCE_PARSING
    assert task.source_task_id == "source-existing"
    assert PARSE_SOURCE_URL not in calls


def test_parse_poll_can_wait_then_submit() -> None:
    polls = 0
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        url = str(request.url)
        if url == DIRECTORY_LIST_URL:
            return ok([{"dir": {"dirName": "p", "idStr": "d"}}])
        if url == RECORD_LIST_URL:
            return ok({"batchRecord": []})
        if url == PARSE_SOURCE_URL:
            return ok({"taskId": "source"})
        if url == QUERY_SOURCE_URL:
            polls += 1
            if polls == 1:
                return ok({"status": -1})
            return ok({"status": 0, "urls": [{"fileId": "f", "size": 1}]})
        if url == RECORD_START_URL:
            return ok({"genRecordIdList": ["record"]})
        raise AssertionError(url)

    task = client_for(handle, sleeps=sleeps).submit_episode(
        "p",
        "e",
        "https://cdn.example/audio",
        parse_poll_attempts=2,
    )
    assert task.provider_task_id == "record"
    assert sleeps == [1]


@pytest.mark.parametrize("status", [40, 41])
def test_record_terminal_failure_is_returned_without_resubmission(status: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DIRECTORY_LIST_URL:
            return ok([{"dir": {"dirName": "p", "idStr": "d"}}])
        return ok(
            {
                "batchRecord": [
                    {"recordList": [{"genRecordId": "r", "status": status, "showName": "e"}]}
                ]
            }
        )

    task = client_for(handle).submit_episode("p", "e", "https://cdn.example/audio")
    assert task.state is TingwuTaskState.FAILED
    assert task.record_status == status


def test_retry_after_and_transient_exhaustion_are_safe() -> None:
    attempts = 0
    sleeps: list[float] = []

    def recover(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return ok([])

    client = client_for(recover, max_retries=1, sleeps=sleeps)
    assert client.list_directories() == {}
    assert sleeps == [2.0]

    broken = client_for(lambda _request: httpx.Response(503), max_retries=0)
    with pytest.raises(ProviderError) as caught:
        broken.health_check()
    assert caught.value.failure.category is ProviderErrorCategory.UNAVAILABLE
    assert COOKIE not in str(caught.value)


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (httpx.Response(401), ProviderErrorCategory.AUTHENTICATION),
        (httpx.Response(403), ProviderErrorCategory.AUTHENTICATION),
        (httpx.Response(418), ProviderErrorCategory.RISK_CONTROL),
        (httpx.Response(404), ProviderErrorCategory.SCHEMA_CHANGED),
        (httpx.Response(200, text="<html>login</html>"), ProviderErrorCategory.SCHEMA_CHANGED),
        (
            httpx.Response(
                200,
                json={"success": False, "errorMsg": "please login", "errorCode": "AUTH"},
            ),
            ProviderErrorCategory.AUTHENTICATION,
        ),
        (
            httpx.Response(
                200,
                json={"success": False, "errorMsg": "需要安全验证", "code": "RISK"},
            ),
            ProviderErrorCategory.RISK_CONTROL,
        ),
    ],
)
def test_final_session_failures_open_circuit(
    response: httpx.Response,
    category: ProviderErrorCategory,
) -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    client = client_for(handle)
    with pytest.raises(ProviderError) as first:
        client.health_check()
    assert first.value.failure.category is category
    assert client.circuit_open is True
    with pytest.raises(ProviderError) as second:
        client.health_check()
    assert second.value.failure == first.value.failure
    assert calls == 1
    assert "secret-cookie-value" not in str(first.value)


def test_success_false_unknown_and_http_unknown_do_not_leak_response() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                json={"success": False, "errorMsg": "private-response-value", "code": "X"},
            ),
            httpx.Response(400, text="private-response-value"),
        ]
    )
    client = client_for(lambda _request: next(responses))
    for expected_code in ("X", "400"):
        with pytest.raises(ProviderError) as caught:
            client.health_check()
        assert caught.value.failure.category is ProviderErrorCategory.UNKNOWN
        assert caught.value.failure.code == expected_code
        assert "private-response-value" not in str(caught.value)


def test_transport_retry_and_exhaustion() -> None:
    calls = 0
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("fixture", request=request)
        return ok([])

    assert client_for(handle, max_retries=1, sleeps=sleeps).health_check()
    assert sleeps == [1.0]

    def always_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture", request=request)

    with pytest.raises(ProviderError) as caught:
        client_for(always_timeout).health_check()
    assert caught.value.failure.category is ProviderErrorCategory.NETWORK
    assert caught.value.failure.retryable is True


def test_schema_contract_failures_are_classified() -> None:
    responses = iter(
        [
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"success": True}),
            ok("not-a-list"),
            ok([{"dir": None}]),
        ]
    )
    for _ in range(4):
        client = client_for(lambda _request: next(responses))
        with pytest.raises(ProviderError) as caught:
            client.list_directories()
        assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


def test_directory_and_record_validation_paths() -> None:
    client = client_for(lambda _request: ok({"focusDir": {}}))
    with pytest.raises(ValueError):
        client.create_directory(" ")
    with pytest.raises(ProviderError):
        client.create_directory("p")

    record_client = client_for(
        lambda _request: ok(
            {
                "batchRecord": [
                    {
                        "recordList": [
                            {"showName": "other", "genRecordId": "skip", "status": 30},
                            {"showName": "target", "id": 7, "status": "unknown"},
                        ]
                    }
                ]
            }
        )
    )
    record = record_client.find_record("d", "target")
    assert record is not None
    task = record_client.task_from_record(record, directory_id="d", title="target")
    assert task.provider_task_id == "7"
    assert task.state is TingwuTaskState.PROCESSING
    with pytest.raises(ProviderError):
        record_client.task_from_record({}, directory_id="d", title="target")


@pytest.mark.parametrize(
    "data",
    [
        {"status": "unknown"},
        {"status": 0, "urls": "wrong"},
        {"status": 0, "urls": [{"fileId": "f"}]},
    ],
)
def test_source_status_schema_failures(data: object) -> None:
    client = client_for(lambda _request: ok(data))
    with pytest.raises(ProviderError) as caught:
        client.query_source("source", directory_id="d", title="e")
    assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED


def test_source_rejected_and_submission_shape() -> None:
    client = client_for(lambda _request: ok({"status": 9}))
    state, files = client.query_source("source", directory_id="d", title="e")
    assert state is TingwuTaskState.FAILED
    assert files == []

    with pytest.raises(ProviderError):
        client_for(lambda _request: ok({"genRecordIdList": []})).start_record("d", "e", [])

    sequence = iter(
        [
            ok([{"dir": {"dirName": "p", "idStr": "d"}}]),
            ok({"batchRecord": []}),
            ok({"taskId": "source"}),
            ok({"status": 9}),
        ]
    )
    with pytest.raises(ProviderError) as caught:
        client_for(lambda _request: next(sequence)).submit_episode(
            "p", "e", "https://cdn.example/audio"
        )
    assert caught.value.failure.category is ProviderErrorCategory.INVALID_INPUT
    with pytest.raises(ValueError, match="positive"):
        client_for(lambda _request: ok([])).submit_episode(
            "p",
            "e",
            "https://cdn.example/audio",
            parse_poll_attempts=0,
        )


def test_result_schema_validation_and_empty_note() -> None:
    transcript_responses = iter(
        [
            ok({"tag": {"identify": "{"}, "result": "{}"}),
            ok({"tag": {}, "result": json.dumps({"pg": [{"ui": 1, "sc": []}]})}),
        ]
    )
    for _ in range(2):
        client = client_for(lambda _request: next(transcript_responses))
        with pytest.raises(ProviderError) as caught:
            client.get_transcript("task")
        assert caught.value.failure.category is ProviderErrorCategory.SCHEMA_CHANGED

    note = client_for(lambda _request: ok({"content": ""})).get_note("task")
    assert note.content is None


def provider_error(category: ProviderErrorCategory) -> ProviderError:
    return ProviderError(
        ProviderFailure(
            provider="tingwu_cookie",
            category=category,
            message="safe",
        )
    )


def test_fallback_policy_only_runs_for_final_tingwu_failures() -> None:
    fallback_calls = 0

    def fallback() -> str:
        nonlocal fallback_calls
        fallback_calls += 1
        return "siliconflow"

    for category in (
        ProviderErrorCategory.AUTHENTICATION,
        ProviderErrorCategory.RISK_CONTROL,
        ProviderErrorCategory.SCHEMA_CHANGED,
    ):
        error = provider_error(category)
        assert tingwu_fallback_allowed(error)

        def fail(error: ProviderError = error) -> str:
            raise error

        assert run_with_tingwu_fallback(fail, fallback) == "siliconflow"

    def processing() -> str:
        return "processing"

    assert run_with_tingwu_fallback(processing, fallback) == "processing"
    assert fallback_calls == 3

    transient = provider_error(ProviderErrorCategory.RATE_LIMITED)
    assert tingwu_fallback_allowed(transient) is False
    with pytest.raises(ProviderError):
        run_with_tingwu_fallback(lambda: (_ for _ in ()).throw(transient), fallback)


def test_client_configuration_and_context_manager() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        TingwuClient("")
    with pytest.raises(ValueError, match="negative"):
        TingwuClient("cookie", max_retries=-1)

    transport = httpx.MockTransport(lambda _request: ok([]))
    with TingwuClient("cookie", client=httpx.Client(transport=transport)) as client:
        assert client.health_check()
