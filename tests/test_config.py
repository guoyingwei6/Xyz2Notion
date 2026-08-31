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
    SummaryConfig,
    config_schema_json,
    derive_device_id,
    load_config,
    load_runtime_credentials,
)


def test_example_config_is_valid_and_secret_free() -> None:
    config = load_config("config.example.yaml")
    assert config.schema_version == 1
    assert config.asr.provider_order == (
        AsrProvider.DASHSCOPE,
        AsrProvider.SILICONFLOW,
        AsrProvider.LOCAL_WHISPER,
    )
    assert config.asr.dashscope_model == "paraformer-v1"
    assert config.asr.dashscope_models == (
        "paraformer-v1",
        "paraformer-v2",
        "paraformer-mtl-v1",
    )
    assert config.summary.siliconflow_models == ("Qwen/Qwen3-8B",)
    assert config.summary.local_qwen_fallback is True
    assert config.summary.prompt_version == "summary-v1"
    assert config.summary.chunk_tokens == 12_000
    assert config.summary.chunk_minutes == 60
    assert config.limits.episodes_per_run == 4
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
        {"provider_order": ["siliconflow", "siliconflow"]},
        {"dashscope_fallback_models": ["paraformer-v2", "paraformer-v2"]},
        {"siliconflow_models": []},
        {"siliconflow_models": ["model", "model"]},
        {"siliconflow_models": ["paid-or-unknown/model"]},
    ],
)
def test_asr_policy_rejects_unsafe_configuration(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AsrConfig.model_validate(values)


def test_empty_provider_order_intentionally_pauses_asr() -> None:
    assert AsrConfig(provider_order=()).provider_order == ()


def test_daily_limit_cannot_exceed_monthly_limit() -> None:
    with pytest.raises(ValidationError, match="daily ASR"):
        LimitConfig(asr_minutes_per_day=31, asr_minutes_per_month=30)


def test_summary_limits_and_free_models_are_validated() -> None:
    with pytest.raises(ValidationError):
        SummaryConfig(chunk_tokens=999)
    with pytest.raises(ValidationError):
        SummaryConfig(siliconflow_models=())
    with pytest.raises(ValidationError):
        SummaryConfig(siliconflow_models=("same", "same"))
    for non_free_model in (
        "Pro/Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-14B-Instruct",
    ):
        with pytest.raises(ValidationError):
            SummaryConfig(siliconflow_models=(non_free_model,))


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
            "DASHSCOPE_API_KEY": "dashscope-example",
            "SILICONFLOW_API_KEY": "silicon-example",
        },
        fallback_identity="test",
    )
    rendered = credentials.model_dump_json() + repr(credentials)
    for secret in (
        "refresh-example",
        "notion-example",
        "dashscope-example",
        "silicon-example",
    ):
        assert secret not in rendered
    credentials.require("notion_token", "xiaoyuzhou_refresh_token")


def test_generated_schema_is_json() -> None:
    schema = json.loads(config_schema_json())
    assert schema["title"] == "AppConfig"
    assert "schema_version" in schema["properties"]
