"""Clean-room client for Tongyi Tingwu's user-authenticated web workflow.

The web endpoints are not a public API. All response parsing is deliberately
defensive, credentials are host-scoped, and callers must persist returned task
identifiers so a later GitHub Actions run can resume without resubmission.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from xyz2notion.models import (
    Chapter,
    ProviderError,
    ProviderErrorCategory,
    ProviderFailure,
    TranscriptResult,
    TranscriptSegment,
    TranscriptTimingQuality,
)
from xyz2notion.security import CredentialKind, validate_credential_destination

TINGWU_ORIGIN = "https://tingwu.aliyun.com"
TINGWU_API = f"{TINGWU_ORIGIN}/api"
DIRECTORY_LIST_URL = f"{TINGWU_API}/directory/request?getDirList&c=web"
DIRECTORY_ADD_URL = f"{TINGWU_API}/directory/request?addDir&c=web"
RECORD_LIST_URL = f"{TINGWU_API}/trans/request?getTransList&c=web"
RECORD_START_URL = f"{TINGWU_API}/trans/request?c=web"
PARSE_SOURCE_URL = f"{TINGWU_API}/trans/parseNetSourceUrl?c=web"
QUERY_SOURCE_URL = f"{TINGWU_API}/trans/queryNetSourceParse?c=web"
TRANSCRIPT_URL = f"{TINGWU_API}/trans/getTransResult?c=web"
LAB_URL = f"{TINGWU_API}/lab/getAllLabInfo?c=web"
NOTE_URL = f"{TINGWU_API}/doc/getTransDocEdit?c=web"


class TingwuTaskState(StrEnum):
    """Resumable states exposed by the asynchronous Tingwu web workflow."""

    SOURCE_PARSING = "source_parsing"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TingwuTask(BaseModel):
    """Task checkpoint that can be persisted between GitHub Actions runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_task_id: str = Field(min_length=1)
    state: TingwuTaskState
    directory_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_task_id: str | None = None
    record_status: int | None = None


class TingwuEnrichment(BaseModel):
    """Structured values available from Tingwu's lab cards."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str | None = None
    chapters: tuple[Chapter, ...] = ()
    questions: tuple[str, ...] = ()
    mindmap: dict[str, Any] | None = None


class TingwuNote(BaseModel):
    """Raw editable-note document retained for the later Notion renderer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: Any | None = None


def _failure(
    category: ProviderErrorCategory,
    message: str,
    *,
    code: str | None = None,
) -> ProviderError:
    return ProviderError(
        ProviderFailure(
            provider="tingwu_cookie",
            category=category,
            message=message,
            code=code,
        )
    )


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _failure(
            ProviderErrorCategory.SCHEMA_CHANGED,
            f"Tingwu response schema changed while reading {context}",
        )
    return value


def _sequence(value: object, context: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise _failure(
            ProviderErrorCategory.SCHEMA_CHANGED,
            f"Tingwu response schema changed while reading {context}",
        )
    return value


def _json_value(value: object, context: str) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise _failure(
            ProviderErrorCategory.SCHEMA_CHANGED,
            f"Tingwu returned invalid JSON while reading {context}",
        ) from exc


class TingwuClient:
    """Cookie-authenticated Tingwu client with retries and a local circuit breaker."""

    _retryable_statuses = frozenset({429, 500, 502, 503, 504})
    _risk_statuses = frozenset({412, 418, 451})

    def __init__(
        self,
        cookie: str | SecretStr,
        *,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 60,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        secret = cookie.get_secret_value() if isinstance(cookie, SecretStr) else cookie
        if not secret.strip():
            raise ValueError("Tingwu Cookie cannot be empty")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        for endpoint in (
            DIRECTORY_LIST_URL,
            DIRECTORY_ADD_URL,
            RECORD_LIST_URL,
            RECORD_START_URL,
            PARSE_SOURCE_URL,
            QUERY_SOURCE_URL,
            TRANSCRIPT_URL,
            LAB_URL,
            NOTE_URL,
        ):
            validate_credential_destination(endpoint, CredentialKind.TINGWU_COOKIE)
        self.max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self._headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": secret,
            "Origin": TINGWU_ORIGIN,
            "Referer": f"{TINGWU_ORIGIN}/",
            "User-Agent": "Mozilla/5.0 Xyz2Notion/0.2",
        }
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._circuit_failure: ProviderFailure | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TingwuClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def circuit_open(self) -> bool:
        """Whether a final session/schema failure disabled this client instance."""
        return self._circuit_failure is not None

    def _open_circuit(self, error: ProviderError) -> ProviderError:
        self._circuit_failure = error.failure
        return error

    def _delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return max(0.0, float(response.headers["Retry-After"]))
            except ValueError:
                pass
        return min(30.0, 2.0**attempt) + self._jitter()

    def _post(
        self,
        url: str,
        payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        if self._circuit_failure is not None:
            raise ProviderError(self._circuit_failure)
        validate_credential_destination(url, CredentialKind.TINGWU_COOKIE)
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.post(url, headers=self._headers, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    self._sleep(self._delay(None, attempt))
                    continue
                raise _failure(
                    ProviderErrorCategory.NETWORK,
                    f"Tingwu transport failed: {type(exc).__name__}",
                ) from exc

            if response.status_code in {401, 403}:
                raise self._open_circuit(
                    _failure(
                        ProviderErrorCategory.AUTHENTICATION,
                        "Tingwu Cookie expired; refresh TINGWU_COOKIE",
                        code=str(response.status_code),
                    )
                )
            if response.status_code in self._risk_statuses:
                raise self._open_circuit(
                    _failure(
                        ProviderErrorCategory.RISK_CONTROL,
                        "Tingwu web access was blocked by account risk control",
                        code=str(response.status_code),
                    )
                )
            if response.status_code in self._retryable_statuses:
                if attempt < self.max_retries:
                    self._sleep(self._delay(response, attempt))
                    continue
                category = (
                    ProviderErrorCategory.RATE_LIMITED
                    if response.status_code == 429
                    else ProviderErrorCategory.UNAVAILABLE
                )
                raise _failure(
                    category,
                    f"Tingwu is temporarily unavailable (HTTP {response.status_code})",
                    code=str(response.status_code),
                )
            if response.status_code == 404:
                raise self._open_circuit(
                    _failure(
                        ProviderErrorCategory.SCHEMA_CHANGED,
                        "Tingwu web endpoint changed",
                        code="404",
                    )
                )
            if response.is_error:
                raise _failure(
                    ProviderErrorCategory.UNKNOWN,
                    f"Tingwu rejected the request (HTTP {response.status_code})",
                    code=str(response.status_code),
                )
            try:
                decoded = response.json()
            except ValueError as exc:
                raise self._open_circuit(
                    _failure(
                        ProviderErrorCategory.SCHEMA_CHANGED,
                        "Tingwu returned a non-JSON response",
                    )
                ) from exc
            result = _mapping(decoded, "response root")
            raw_code = result.get("errorCode") or result.get("code")
            code = str(raw_code) if raw_code is not None else ""
            explicit_success = result.get("success")
            accepted = explicit_success is True or code == "0"
            rejected = explicit_success is False or (bool(code) and code != "0")
            if rejected and not accepted:
                hint = " ".join(
                    str(value)
                    for value in (
                        code,
                        result.get("errorMsg"),
                        result.get("message"),
                        result.get("msg"),
                    )
                    if value is not None
                ).lower()
                if any(
                    word in hint
                    for word in (
                        "login",
                        "cookie",
                        "expired",
                        "notlogin",
                        "not.login",
                        "登录",
                        "未登录",
                        "过期",
                    )
                ):
                    raise self._open_circuit(
                        _failure(
                            ProviderErrorCategory.AUTHENTICATION,
                            "Tingwu Cookie expired; refresh TINGWU_COOKIE",
                            code=code or "rejected",
                        )
                    )
                if any(
                    word in hint for word in ("risk", "captcha", "security", "风控", "验证", "安全")
                ):
                    raise self._open_circuit(
                        _failure(
                            ProviderErrorCategory.RISK_CONTROL,
                            "Tingwu web access was blocked by account risk control",
                            code=code or "rejected",
                        )
                    )
                raise _failure(
                    ProviderErrorCategory.UNKNOWN,
                    "Tingwu rejected the request",
                    code=code or "rejected",
                )
            return result
        raise AssertionError("unreachable retry loop")

    def _data(self, response: Mapping[str, Any], context: str) -> Any:
        if "data" not in response:
            error = _failure(
                ProviderErrorCategory.SCHEMA_CHANGED,
                f"Tingwu response schema changed while reading {context}",
            )
            raise self._open_circuit(error)
        return response["data"]

    def health_check(self) -> bool:
        """Validate the Cookie with a read-only directory-list request."""
        self.list_directories()
        return True

    def list_directories(self) -> dict[str, str]:
        response = self._post(
            DIRECTORY_LIST_URL,
            {"action": "getDirList", "version": "1.0", "returnDetails": True},
        )
        entries = _sequence(self._data(response, "directories"), "directories")
        directories: dict[str, str] = {}
        for entry in entries:
            wrapper = _mapping(entry, "directory entry")
            directory = _mapping(wrapper.get("dir"), "directory") if "dir" in wrapper else wrapper
            name = directory.get("dirName")
            identifier = directory.get("dirId") or directory.get("idStr") or directory.get("id")
            if isinstance(name, str) and name and identifier is not None:
                directories[name] = str(identifier)
        return directories

    def create_directory(self, name: str) -> str:
        if not name.strip():
            raise ValueError("Tingwu directory name cannot be empty")
        response = self._post(
            DIRECTORY_ADD_URL,
            {
                "action": "addDir",
                "version": "1.0",
                "dirName": name.strip(),
                "parentDirId": -1,
                "returnNewList": 1,
            },
        )
        data = _mapping(self._data(response, "created directory"), "created directory")
        focus = _mapping(data.get("focusDir"), "created directory focus")
        identifier = focus.get("dirId") or focus.get("idStr") or focus.get("id")
        if identifier is None:
            raise self._open_circuit(
                _failure(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "Tingwu response omitted the created directory ID",
                )
            )
        return str(identifier)

    def ensure_directory(self, name: str) -> str:
        return self.list_directories().get(name) or self.create_directory(name)

    def find_records(self, directory_id: str, title: str) -> tuple[Mapping[str, Any], ...]:
        """Return all exact-title matches so callers never guess between duplicates."""
        response = self._post(
            RECORD_LIST_URL,
            {
                "action": "getTransList",
                "version": "1.0",
                "userId": "",
                "filter": {
                    "status": [0, 1, 2, 3, 4, 11],
                    "showName": title,
                    "dirId": directory_id,
                },
                "preview": 0,
                "pageNo": 1,
                "pageSize": 12,
            },
        )
        raw_data = self._data(response, "record list")
        if isinstance(raw_data, list):
            records: Sequence[Any] = raw_data
        else:
            data = _mapping(raw_data, "record list")
            batches = _sequence(data.get("batchRecord", []), "record batches")
            flattened: list[Any] = []
            for batch in batches:
                flattened.extend(
                    _sequence(
                        _mapping(batch, "record batch").get("recordList", []),
                        "records",
                    )
                )
            records = flattened
        matches: list[Mapping[str, Any]] = []
        for record in records:
            item = _mapping(record, "record")
            tag = _mapping(item.get("tag", {}), "record tag")
            show_name = item.get("showName") or item.get("title") or tag.get("showName")
            if show_name == title:
                matches.append(item)
        return tuple(matches)

    def find_record(self, directory_id: str, title: str) -> Mapping[str, Any] | None:
        """Return one unique exact-title match, or stop safely when ambiguous."""
        matches = self.find_records(directory_id, title)
        if len(matches) > 1:
            raise _failure(
                ProviderErrorCategory.UNAVAILABLE,
                "Tingwu returned multiple matching records; manual review is required",
                code="ambiguous_record",
            )
        return matches[0] if matches else None

    def task_from_record(
        self,
        record: Mapping[str, Any],
        *,
        directory_id: str,
        title: str,
    ) -> TingwuTask:
        identifier = (
            record.get("genRecordId")
            or record.get("transId")
            or record.get("taskId")
            or record.get("id")
        )
        if identifier is None:
            raise self._open_circuit(
                _failure(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "Tingwu record omitted its task ID",
                )
            )
        raw_status = record.get("status")
        try:
            status = int(str(raw_status))
        except (TypeError, ValueError):
            status = None
        state = (
            TingwuTaskState.SUCCEEDED
            if status in {0, 30}
            else (
                TingwuTaskState.FAILED
                if status in {2, 11, 20, 21, 22, 40, 41}
                else TingwuTaskState.PROCESSING
            )
        )
        return TingwuTask(
            provider_task_id=str(identifier),
            state=state,
            directory_id=directory_id,
            title=title,
            record_status=status,
        )

    def get_task(self, directory_id: str, title: str) -> TingwuTask | None:
        record = self.find_record(directory_id, title)
        if record is None:
            return None
        return self.task_from_record(record, directory_id=directory_id, title=title)

    def resume_episode(
        self,
        directory_id: str,
        title: str,
        *,
        provider_task_id: str,
        source_task_id: str | None,
    ) -> TingwuTask:
        """Poll a persisted submission without ever creating another record."""
        matches = self.find_records(directory_id, title)
        if len(matches) > 1:
            exact = [
                record
                for record in matches
                if str(
                    record.get("genRecordId")
                    or record.get("transId")
                    or record.get("taskId")
                    or record.get("id")
                    or ""
                )
                == provider_task_id
            ]
            if len(exact) == 1:
                matches = tuple(exact)
            else:
                raise _failure(
                    ProviderErrorCategory.UNAVAILABLE,
                    "Tingwu returned multiple matching records; manual review is required",
                    code="ambiguous_record",
                )
        if len(matches) == 1:
            return self.task_from_record(matches[0], directory_id=directory_id, title=title)
        if provider_task_id == source_task_id:
            raise _failure(
                ProviderErrorCategory.UNAVAILABLE,
                "Tingwu submission record is not visible in the directory",
                code="record_not_visible",
            )
        return TingwuTask(
            provider_task_id=provider_task_id,
            source_task_id=source_task_id,
            state=TingwuTaskState.SUBMITTED,
            directory_id=directory_id,
            title=title,
        )

    def parse_source(self, audio_url: str) -> str:
        response = self._post(
            PARSE_SOURCE_URL,
            {"action": "parseNetSourceUrl", "version": "1.0", "url": audio_url},
        )
        data = _mapping(self._data(response, "source parser"), "source parser")
        identifier = data.get("taskId")
        if identifier is None:
            raise self._open_circuit(
                _failure(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "Tingwu source parser omitted its task ID",
                )
            )
        return str(identifier)

    def query_source(
        self,
        source_task_id: str,
        *,
        directory_id: str,
        title: str,
    ) -> tuple[TingwuTaskState, list[dict[str, object]]]:
        response = self._post(
            QUERY_SOURCE_URL,
            {
                "action": "queryNetSourceParse",
                "version": "1.0",
                "taskId": source_task_id,
            },
        )
        data = _mapping(self._data(response, "source parser status"), "source parser status")
        try:
            status = int(str(data.get("status")))
        except (TypeError, ValueError) as exc:
            raise self._open_circuit(
                _failure(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "Tingwu source parser returned an unknown status",
                )
            ) from exc
        if status == -1:
            return TingwuTaskState.SOURCE_PARSING, []
        if status != 0:
            return TingwuTaskState.FAILED, []
        urls = _sequence(data.get("urls"), "parsed source URLs")
        files: list[dict[str, object]] = []
        for item in urls:
            source = _mapping(item, "parsed source")
            file_id = source.get("fileId")
            size = source.get("size")
            if file_id is None or size is None:
                raise self._open_circuit(
                    _failure(
                        ProviderErrorCategory.SCHEMA_CHANGED,
                        "Tingwu parsed source omitted file metadata",
                    )
                )
            files.append(
                {
                    "fileId": file_id,
                    "dirId": directory_id,
                    "fileSize": size,
                    "tag": {
                        "fileType": "net_source",
                        "showName": title,
                        "lang": "cn",
                        "roleSplitNum": 0,
                        "translateSwitch": 0,
                        "transTargetValue": 0,
                        "client": "web",
                        "originalTag": "",
                    },
                }
            )
        return TingwuTaskState.SUBMITTED, files

    def start_record(
        self,
        directory_id: str,
        title: str,
        files: Sequence[Mapping[str, object]],
        *,
        source_task_id: str,
    ) -> TingwuTask:
        response = self._post(
            RECORD_START_URL,
            {
                "action": "putNetSourceUrl",
                "version": "1.0",
                "files": list(files),
            },
        )
        # A top-level success flag is not enough for automation: without a real
        # record identifier the web UI may never show a new transcription.
        data = response.get("data")
        identifiers: list[object] = []
        if isinstance(data, Mapping):
            for key in ("genRecordIdList", "transIds", "transIdList", "taskIds"):
                value = data.get(key)
                if isinstance(value, list):
                    identifiers.extend(value)
            for key in ("genRecordId", "transId", "taskId", "id"):
                value = data.get(key)
                if value is not None:
                    identifiers.append(value)
        elif isinstance(data, list):
            identifiers.extend(data)
        identifier = next((value for value in identifiers if value is not None), None)
        if identifier is None:
            raise _failure(
                ProviderErrorCategory.SCHEMA_CHANGED,
                "Tingwu did not return a confirmed transcription record ID",
                code="unconfirmed_record",
            )
        return TingwuTask(
            provider_task_id=str(identifier),
            state=TingwuTaskState.SUBMITTED,
            directory_id=directory_id,
            title=title,
            record_status=20,
            source_task_id=source_task_id,
        )

    def submit_episode(
        self,
        directory_name: str,
        title: str,
        audio_url: str,
        *,
        source_task_id: str | None = None,
        parse_poll_attempts: int = 1,
    ) -> TingwuTask:
        """Idempotently find, resume, or submit one public-audio transcription."""
        if parse_poll_attempts < 1:
            raise ValueError("parse_poll_attempts must be positive")
        directory_id = self.ensure_directory(directory_name)
        existing = self.get_task(directory_id, title)
        if existing is not None:
            return existing
        parser_id = source_task_id or self.parse_source(audio_url)
        for attempt in range(parse_poll_attempts):
            state, files = self.query_source(
                parser_id,
                directory_id=directory_id,
                title=title,
            )
            if state is TingwuTaskState.SUBMITTED:
                return self.start_record(
                    directory_id,
                    title,
                    files,
                    source_task_id=parser_id,
                )
            if state is TingwuTaskState.FAILED:
                raise _failure(
                    ProviderErrorCategory.INVALID_INPUT,
                    "Tingwu could not parse the episode audio URL",
                )
            if attempt + 1 < parse_poll_attempts:
                self._sleep(1)
        return TingwuTask(
            provider_task_id=parser_id,
            source_task_id=parser_id,
            state=TingwuTaskState.SOURCE_PARSING,
            directory_id=directory_id,
            title=title,
        )

    def get_transcript(self, task_id: str) -> TranscriptResult:
        response = self._post(
            TRANSCRIPT_URL,
            {"action": "getTransResult", "version": "1.0", "transId": task_id},
        )
        data = _mapping(self._data(response, "transcript"), "transcript")
        tag = _mapping(data.get("tag", {}), "transcript tag")
        identify = _json_value(tag.get("identify", {}), "speaker identities")
        identity = _mapping(identify, "speaker identities")
        users = _mapping(identity.get("user_info", {}), "speaker users")
        speaker_names: dict[str, str] = {}
        for key, value in users.items():
            person = _mapping(value, "speaker")
            name = person.get("name")
            if isinstance(name, str) and name:
                speaker_names[str(key)] = name

        result = _mapping(_json_value(data.get("result"), "transcript result"), "transcript")
        paragraphs = _sequence(result.get("pg", []), "transcript paragraphs")
        raw_segments: list[tuple[int, str, str]] = []
        for paragraph in paragraphs:
            page = _mapping(paragraph, "transcript paragraph")
            speaker_id = str(page.get("ui", ""))
            speaker = speaker_names.get(speaker_id) or f"发言人{speaker_id or '?'}"
            sentences = _sequence(page.get("sc", []), "transcript sentences")
            for sentence in sentences:
                item = _mapping(sentence, "transcript sentence")
                text = item.get("tc")
                begin = item.get("bt")
                if not isinstance(text, str) or not text.strip() or begin is None:
                    continue
                try:
                    start_ms = max(0, int(float(begin)))
                except (TypeError, ValueError):
                    continue
                raw_segments.append((start_ms, text.strip(), speaker))
        raw_segments.sort(key=lambda item: item[0])
        if not raw_segments:
            raise self._open_circuit(
                _failure(
                    ProviderErrorCategory.SCHEMA_CHANGED,
                    "Tingwu transcript contained no readable sentences",
                )
            )
        segments = tuple(
            TranscriptSegment(
                start_ms=start,
                end_ms=raw_segments[index + 1][0] if index + 1 < len(raw_segments) else start,
                text=text,
                speaker=speaker,
            )
            for index, (start, text, speaker) in enumerate(raw_segments)
        )
        return TranscriptResult(
            provider="tingwu_cookie",
            provider_task_id=task_id,
            model="tongyi-tingwu-web",
            duration_ms=segments[-1].end_ms,
            text="\n".join(segment.text for segment in segments),
            segments=segments,
            timing_quality=TranscriptTimingQuality.EXACT,
        )

    def get_enrichment(self, task_id: str) -> TingwuEnrichment:
        response = self._post(
            LAB_URL,
            {
                "action": "getAllLabInfo",
                "content": ["labInfo", "labSummaryInfo"],
                "transId": task_id,
            },
        )
        data = _mapping(self._data(response, "lab cards"), "lab cards")
        cards_map = _mapping(data.get("labCardsMap"), "lab card groups")
        cards: list[Any] = []
        for group_name in ("labInfo", "labSummaryInfo"):
            cards.extend(_sequence(cards_map.get(group_name, []), f"{group_name} cards"))

        summary: str | None = None
        chapters: list[Chapter] = []
        questions: list[str] = []
        mindmap: dict[str, Any] | None = None
        for raw_card in cards:
            card = _mapping(raw_card, "lab card")
            basic = _mapping(card.get("basicInfo", {}), "lab card info")
            name = basic.get("name")
            contents = _sequence(card.get("contents", []), "lab card contents")
            for raw_content in contents:
                content = _mapping(raw_content, "lab content")
                values = _sequence(content.get("contentValues", []), "lab values")
                for raw_value in values:
                    value = _mapping(raw_value, "lab value")
                    if name == "全文摘要" and isinstance(value.get("value"), str):
                        summary = value["value"].strip() or summary
                    elif name == "思维导图":
                        parsed = _json_value(value.get("json"), "mind map")
                        if isinstance(parsed, dict):
                            mindmap = parsed
                    elif name == "议程" and isinstance(value.get("value"), str):
                        try:
                            start_ms = max(0, int(float(value.get("time", 0))))
                        except (TypeError, ValueError):
                            start_ms = 0
                        chapters.append(
                            Chapter(
                                start_ms=start_ms,
                                title=value["value"],
                                summary=str(value.get("summary") or ""),
                            )
                        )
                    elif name == "qa问答":
                        title = str(value.get("title") or "").strip()
                        answer = str(value.get("value") or "").strip()
                        if title or answer:
                            questions.append("\n".join(part for part in (title, answer) if part))
        return TingwuEnrichment(
            summary=summary,
            chapters=tuple(chapters),
            questions=tuple(questions),
            mindmap=mindmap,
        )

    def get_note(self, task_id: str) -> TingwuNote:
        response = self._post(
            NOTE_URL,
            {"action": "getTransDocEdit", "version": "1.0", "transId": task_id},
        )
        data = _mapping(self._data(response, "note"), "note")
        content = data.get("content")
        return TingwuNote(content=_json_value(content, "note") if content else None)
