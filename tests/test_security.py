import pytest

from xyz2notion.security import (
    CredentialKind,
    UnsafeCredentialDestinationError,
    redact_mapping,
    redact_text,
    validate_credential_destination,
)


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        (CredentialKind.XIAOYUZHOU, "https://api.xiaoyuzhoufm.com/app/v1/test"),
        (CredentialKind.NOTION, "https://api.notion.com/v1/pages"),
        (CredentialKind.DASHSCOPE, "https://dashscope.aliyuncs.com/api/v1/tasks/task-1"),
        (CredentialKind.SILICONFLOW, "https://api.siliconflow.cn/v1/audio/transcriptions"),
        (CredentialKind.SILICONFLOW, "https://api.siliconflow.cn/v1/chat/completions"),
    ],
)
def test_allows_only_declared_service_hosts(kind: CredentialKind, url: str) -> None:
    assert validate_credential_destination(url, kind)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/collect",
        "http://api.xiaoyuzhoufm.com/app/v1/test",
        "https://api.xiaoyuzhoufm.com.evil.example/app/v1/test",
        "https://token@api.xiaoyuzhoufm.com/app/v1/test",
        "https://api.xiaoyuzhoufm.com@evil.example/app/v1/test",
        "https://malinkang.com/collect",
        "https://podcast.malinkang.com/collect",
        "https://api.notionhub.app/collect",
        "https://qianwen.biz.aliyun.com/api/test",
        "https://tw-efficiency.biz.aliyun.com/api/test",
    ],
)
def test_rejects_unsafe_xiaoyuzhou_credential_destinations(url: str) -> None:
    with pytest.raises(UnsafeCredentialDestinationError):
        validate_credential_destination(url, CredentialKind.XIAOYUZHOU)


def test_credentials_cannot_cross_service_boundaries() -> None:
    with pytest.raises(UnsafeCredentialDestinationError):
        validate_credential_destination(
            "https://api.notion.com/v1/pages",
            CredentialKind.XIAOYUZHOU,
        )
    with pytest.raises(UnsafeCredentialDestinationError):
        validate_credential_destination(
            "https://api.siliconflow.cn/v1/audio/transcriptions",
            CredentialKind.NOTION,
        )


def test_redact_text_removes_tokens_cookies_and_keys() -> None:
    secrets = {
        "refresh": "example-refresh-value",
        "cookie": "example-cookie-value",
        "api_key": "example-api-key-value",
        "bearer": "example-access-value",
    }
    raw = (
        f"X-Jike-Refresh-Token: {secrets['refresh']}\n"
        f"Cookie: {secrets['cookie']}\n"
        f'api_key="{secrets["api_key"]}"\n'
        f"Authorization: Bearer {secrets['bearer']}\n"
        f"NOTION_TOKEN={secrets['bearer']}"
    )
    redacted = redact_text(raw)
    assert redacted.count("<REDACTED>") == 5
    for secret in secrets.values():
        assert secret not in redacted


def test_redact_mapping_uses_sensitive_key_names() -> None:
    values = {
        "NOTION_TOKEN": "notion-secret-value",
        "DASHSCOPE_API_KEY": "dashscope-secret-value",
        "status": "ready",
    }
    assert redact_mapping(values) == {
        "NOTION_TOKEN": "<REDACTED>",
        "DASHSCOPE_API_KEY": "<REDACTED>",
        "status": "ready",
    }
