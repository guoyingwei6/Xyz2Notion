import json
import warnings
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from xyz2notion.config import (
    AppConfig,
    AsrConfig,
    AsrProvider,
    ConfigurationError,
    LimitConfig,
    MissingCredentialError,
    config_schema_json,
    derive_device_id,
    load_config,
    load_runtime_credentials,
)


def test_example_config_is_valid_and_secret_free() -> None:
    config = load_config("config.example.yaml")
    assert config.schema_version == 1
    assert config.asr.provider_order == (
        AsrProvider.TINGWU_COOKIE,
        AsrProvider.SILICONFLOW,
    )
    assert config.asr.paid_enabled is False
    assert config.asr.paid_budget_cny == 0
    assert config.limits.episodes_per_run == 3
    raw = Path("config.example.yaml").read_text(encoding="utf-8")
    assert "TOKEN" not in raw
    assert "COOKIE" not in raw


def test_empty_config_uses_safe_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert load_config(path) == AppConfig()


@pytest.mark.parametrize(
    "content",
    [
        "- not-a-mapping\n",
        "schema_version: 2\n",
        "schema_version: 1\nunknown: true\n",
        "schema_version: [broken\n",
    ],
)
def test_invalid_config_has_clear_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Configuration|configuration"):
        load_config(path)


def test_missing_config_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "values",
    [
        {"provider_order": []},
        {"provider_order": ["siliconflow", "siliconflow"]},
        {"siliconflow_models": []},
        {"siliconflow_models": ["model", "model"]},
        {"paid_enabled": True, "paid_budget_cny": 0},
        {"paid_enabled": False, "paid_budget_cny": 1},
        {"provider_order": ["dashscope_paid"]},
    ],
)
def test_asr_policy_rejects_unsafe_configuration(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AsrConfig.model_validate(values)


def test_paid_provider_requires_explicit_budget() -> None:
    config = AsrConfig(
        provider_order=(AsrProvider.DASHSCOPE_PAID,),
        paid_enabled=True,
        paid_budget_cny=5,
    )
    assert config.paid_budget_cny == 5


def test_daily_limit_cannot_exceed_monthly_limit() -> None:
    with pytest.raises(ValidationError, match="daily ASR"):
        LimitConfig(asr_minutes_per_day=31, asr_minutes_per_month=30)


def test_derived_device_id_is_stable_and_valid() -> None:
    first = derive_device_id("guoyingwei6/Xyz2Notion")
    second = derive_device_id("guoyingwei6/Xyz2Notion")
    assert first == second
    assert str(UUID(first)) == first
    with pytest.raises(ConfigurationError, match="cannot be empty"):
        derive_device_id(" ")


def test_explicit_device_id_is_preserved() -> None:
    explicit = "D9428888-122B-11E1-B85C-61CD3CBB3210"
    credentials = load_runtime_credentials({"XIAOYUZHOU_DEVICE_ID": explicit})
    assert credentials.xiaoyuzhou_device_id == explicit.lower()


def test_invalid_explicit_device_id_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="valid UUID"):
        load_runtime_credentials({"XIAOYUZHOU_DEVICE_ID": "not-a-uuid"})


def test_repository_identity_generates_stable_device_id() -> None:
    env = {"GITHUB_REPOSITORY": "guoyingwei6/Xyz2Notion"}
    assert (
        load_runtime_credentials(env).xiaoyuzhou_device_id
        == load_runtime_credentials(env).xiaoyuzhou_device_id
    )


def test_fallback_identity_generates_stable_device_id() -> None:
    first = load_runtime_credentials({}, fallback_identity="local-install")
    second = load_runtime_credentials({}, fallback_identity="local-install")
    assert first.xiaoyuzhou_device_id == second.xiaoyuzhou_device_id


def test_legacy_refresh_token_migrates_with_warning() -> None:
    with pytest.warns(DeprecationWarning, match="deprecated"):
        credentials = load_runtime_credentials(
            {"REFRESH_TOKEN": "legacy-example"},
            fallback_identity="test",
        )
    assert credentials.xiaoyuzhou_refresh_token is not None
    assert credentials.xiaoyuzhou_refresh_token.get_secret_value() == "legacy-example"


def test_new_refresh_token_wins_without_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        credentials = load_runtime_credentials(
            {
                "REFRESH_TOKEN": "legacy-example",
                "XIAOYUZHOU_REFRESH_TOKEN": "new-example",
            },
            fallback_identity="test",
        )
    assert credentials.xiaoyuzhou_refresh_token is not None
    assert credentials.xiaoyuzhou_refresh_token.get_secret_value() == "new-example"


def test_required_credentials_report_all_missing_names() -> None:
    credentials = load_runtime_credentials({}, fallback_identity="test")
    with pytest.raises(MissingCredentialError) as caught:
        credentials.require("notion_token", "xiaoyuzhou_refresh_token")
    assert "notion_token" in str(caught.value)
    assert "xiaoyuzhou_refresh_token" in str(caught.value)


def test_secret_values_are_not_serialized_or_represented() -> None:
    credentials = load_runtime_credentials(
        {
            "XIAOYUZHOU_REFRESH_TOKEN": "refresh-example",
            "NOTION_TOKEN": "notion-example",
            "NOTION_PAGE_ID": "page-example",
            "TINGWU_COOKIE": "cookie-example",
            "SILICONFLOW_API_KEY": "silicon-example",
            "DASHSCOPE_API_KEY": "dashscope-example",
        },
        fallback_identity="test",
    )
    rendered = credentials.model_dump_json() + repr(credentials)
    for secret in (
        "refresh-example",
        "notion-example",
        "cookie-example",
        "silicon-example",
        "dashscope-example",
    ):
        assert secret not in rendered
    credentials.require("notion_token", "xiaoyuzhou_refresh_token")


def test_generated_schema_is_json() -> None:
    schema = json.loads(config_schema_json())
    assert schema["title"] == "AppConfig"
    assert "schema_version" in schema["properties"]
