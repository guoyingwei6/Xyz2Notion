"""Public configuration and secret environment loading."""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class ConfigurationError(ValueError):
    """Raised when configuration or environment settings are invalid."""


class MissingCredentialError(ConfigurationError):
    """Raised when an operation is missing one or more required credentials."""


class AsrProvider(StrEnum):
    """Supported ASR providers in fallback order."""

    TINGWU_COOKIE = "tingwu_cookie"
    SILICONFLOW = "siliconflow"


FREE_SILICONFLOW_ASR_MODELS = frozenset(
    {
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
    }
)
FREE_SILICONFLOW_SUMMARY_MODELS = frozenset(
    {
        "Qwen/Qwen3-8B",
        "Qwen/Qwen2.5-7B-Instruct",
    }
)


class StrictConfigModel(BaseModel):
    """Shared policy for public configuration models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AsrConfig(StrictConfigModel):
    """Free speech recognition provider policy."""

    provider_order: tuple[AsrProvider, ...] = (
        AsrProvider.TINGWU_COOKIE,
        AsrProvider.SILICONFLOW,
    )
    siliconflow_models: tuple[str, ...] = (
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
    )

    @model_validator(mode="after")
    def validate_provider_policy(self) -> Self:
        if len(set(self.provider_order)) != len(self.provider_order):
            raise ValueError("asr.provider_order cannot contain duplicates")
        if not self.siliconflow_models or any(
            not model.strip() for model in self.siliconflow_models
        ):
            raise ValueError("asr.siliconflow_models must contain non-empty model names")
        if len(set(self.siliconflow_models)) != len(self.siliconflow_models):
            raise ValueError("asr.siliconflow_models cannot contain duplicates")
        if unknown := set(self.siliconflow_models) - FREE_SILICONFLOW_ASR_MODELS:
            raise ValueError(
                f"asr.siliconflow_models contains models outside the free allowlist: "
                f"{', '.join(sorted(unknown))}"
            )
        return self


class LimitConfig(StrictConfigModel):
    """Per-run and time-window safety limits."""

    episodes_per_run: int = Field(default=3, ge=1)
    asr_minutes_per_day: int = Field(default=240, ge=1)
    asr_minutes_per_month: int = Field(default=3000, ge=1)
    provider_poll_attempts: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def validate_time_windows(self) -> Self:
        if self.asr_minutes_per_day > self.asr_minutes_per_month:
            raise ValueError("daily ASR minute limit cannot exceed monthly limit")
        return self


class SummaryConfig(StrictConfigModel):
    """Free SiliconFlow summary generation settings."""

    enabled: bool = True
    siliconflow_models: tuple[str, ...] = (
        "Qwen/Qwen3-8B",
        "Qwen/Qwen2.5-7B-Instruct",
    )
    prompt_version: str = Field(default="summary-v1", min_length=1)
    chunk_tokens: int = Field(default=24_000, ge=1_000, le=100_000)
    chunk_minutes: int = Field(default=30, ge=5, le=120)
    max_output_tokens: int = Field(default=8_192, ge=512, le=32_768)

    @model_validator(mode="after")
    def validate_models(self) -> Self:
        if not self.siliconflow_models or any(
            not model.strip() for model in self.siliconflow_models
        ):
            raise ValueError("summary.siliconflow_models must contain non-empty model names")
        if len(set(self.siliconflow_models)) != len(self.siliconflow_models):
            raise ValueError("summary.siliconflow_models cannot contain duplicates")
        if unknown := set(self.siliconflow_models) - FREE_SILICONFLOW_SUMMARY_MODELS:
            raise ValueError(
                f"summary.siliconflow_models contains models outside the free allowlist: "
                f"{', '.join(sorted(unknown))}"
            )
        return self


class AppConfig(StrictConfigModel):
    """Complete secret-free application configuration."""

    schema_version: Literal[1] = 1
    asr: AsrConfig = Field(default_factory=AsrConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    limits: LimitConfig = Field(default_factory=LimitConfig)
    state_file: str = Field(default=".xyz2notion/state.json", min_length=1)


class RuntimeCredentials(BaseModel):
    """Secrets and derived identifiers loaded only from the runtime environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    xiaoyuzhou_refresh_token: SecretStr | None = None
    xiaoyuzhou_device_id: str
    notion_token: SecretStr | None = None
    notion_page_id: str | None = None
    tingwu_cookie: SecretStr | None = None
    siliconflow_api_key: SecretStr | None = None

    def require(self, *names: str) -> Self:
        """Require named credentials before a network operation."""
        missing = [name for name in names if not getattr(self, name, None)]
        if missing:
            rendered = ", ".join(sorted(missing))
            raise MissingCredentialError(f"Missing required credential(s): {rendered}")
        return self


_DEVICE_NAMESPACE = UUID("2c29d191-6d83-5b88-9e77-cad06c0d5a71")


def derive_device_id(identity: str) -> str:
    """Derive a stable UUID from a non-secret installation identity."""
    normalized = identity.strip()
    if not normalized:
        raise ConfigurationError("Device ID identity seed cannot be empty")
    return str(uuid5(_DEVICE_NAMESPACE, normalized))


def _normalize_explicit_device_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ConfigurationError("XIAOYUZHOU_DEVICE_ID must be a valid UUID") from exc


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a public YAML configuration file."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigurationError(f"Configuration file does not exist: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot read configuration: {config_path}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a YAML mapping")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc


def config_schema_json() -> str:
    """Return the generated Pydantic JSON Schema for integrations and editors."""
    return json.dumps(AppConfig.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True)


def load_runtime_credentials(
    environ: Mapping[str, str] | None = None,
    *,
    fallback_identity: str | None = None,
) -> RuntimeCredentials:
    """Load runtime credentials without persisting or logging their values."""
    env = dict(os.environ if environ is None else environ)
    refresh_token = env.get("XIAOYUZHOU_REFRESH_TOKEN", "").strip()
    legacy_refresh_token = env.get("REFRESH_TOKEN", "").strip()
    if not refresh_token and legacy_refresh_token:
        warnings.warn(
            "REFRESH_TOKEN is deprecated; rename it to XIAOYUZHOU_REFRESH_TOKEN",
            DeprecationWarning,
            stacklevel=2,
        )
        refresh_token = legacy_refresh_token

    explicit_device_id = env.get("XIAOYUZHOU_DEVICE_ID", "").strip()
    if explicit_device_id:
        device_id = _normalize_explicit_device_id(explicit_device_id)
    else:
        identity = (
            env.get("XYZ2NOTION_INSTALLATION_ID", "").strip()
            or env.get("GITHUB_REPOSITORY", "").strip()
            or env.get("NOTION_PAGE_ID", "").strip()
            or (fallback_identity or Path.cwd().resolve().as_uri())
        )
        device_id = derive_device_id(identity)

    def secret(name: str) -> SecretStr | None:
        value = env.get(name, "").strip()
        return SecretStr(value) if value else None

    return RuntimeCredentials(
        xiaoyuzhou_refresh_token=SecretStr(refresh_token) if refresh_token else None,
        xiaoyuzhou_device_id=device_id,
        notion_token=secret("NOTION_TOKEN"),
        notion_page_id=env.get("NOTION_PAGE_ID", "").strip() or None,
        tingwu_cookie=secret("TINGWU_COOKIE"),
        siliconflow_api_key=secret("SILICONFLOW_API_KEY"),
    )
