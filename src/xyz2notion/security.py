"""Credential destination validation and log redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlsplit


class UnsafeCredentialDestinationError(ValueError):
    """Raised when a credential could be sent to an unsafe destination."""


class CredentialKind(StrEnum):
    """Credential categories with independent destination policies."""

    XIAOYUZHOU = "xiaoyuzhou"
    NOTION = "notion"
    TINGWU_COOKIE = "tingwu_cookie"
    SILICONFLOW = "siliconflow"
    DASHSCOPE = "dashscope"


_ALLOWED_HOSTS: Mapping[CredentialKind, frozenset[str]] = MappingProxyType(
    {
        CredentialKind.XIAOYUZHOU: frozenset({"api.xiaoyuzhoufm.com"}),
        CredentialKind.NOTION: frozenset({"api.notion.com"}),
        CredentialKind.TINGWU_COOKIE: frozenset(
            {
                "qianwen.biz.aliyun.com",
                "tw-efficiency.biz.aliyun.com",
            }
        ),
        CredentialKind.SILICONFLOW: frozenset({"api.siliconflow.cn"}),
        CredentialKind.DASHSCOPE: frozenset(
            {
                "dashscope.aliyuncs.com",
                "dashscope-intl.aliyuncs.com",
            }
        ),
    }
)

_BLOCKED_HOST_SUFFIXES = ("malinkang.com", "notionhub.app")
_SENSITIVE_NAME = re.compile(
    r"(?i)(authorization|cookie|x-jike-(?:refresh|access)-token|"
    r"(?:api[-_]?key|token|secret|password))"
)
_HEADER_SECRET = re.compile(
    r"(?im)^(\s*(?:authorization|cookie|x-jike-(?:refresh|access)-token|"
    r"x-api-key)\s*:\s*)([^\r\n]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_VALUE_SECRET = re.compile(
    r"""(?ix)
    \b([a-z0-9_-]*(?:api[-_]?key|token|secret|password|cookie)[a-z0-9_-]*)
    (\s*["']?\s*[:=]\s*["']?)
    ([^"',\s}\]]+)
    """
)


def allowed_hosts(kind: CredentialKind) -> frozenset[str]:
    """Return the immutable host allowlist for one credential category."""
    return _ALLOWED_HOSTS[kind]


def validate_credential_destination(url: str, kind: CredentialKind) -> str:
    """Validate a URL before attaching a credential and return its normalized host."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()

    if parsed.scheme != "https":
        raise UnsafeCredentialDestinationError("Credentials require an HTTPS destination")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeCredentialDestinationError("Credentials are forbidden in URL userinfo")
    if not host:
        raise UnsafeCredentialDestinationError("Credential destination has no hostname")
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in _BLOCKED_HOST_SUFFIXES):
        raise UnsafeCredentialDestinationError(f"Blocked service domain: {host}")
    if host not in _ALLOWED_HOSTS[kind]:
        raise UnsafeCredentialDestinationError(
            f"{kind.value} credential cannot be sent to host: {host}"
        )
    return host


def redact_text(value: object) -> str:
    """Return a log-safe representation of arbitrary text."""
    text = str(value)
    text = _HEADER_SECRET.sub(r"\1<REDACTED>", text)
    text = _BEARER_SECRET.sub("Bearer <REDACTED>", text)
    return _KEY_VALUE_SECRET.sub(r"\1\2<REDACTED>", text)


def redact_mapping(values: Mapping[str, object]) -> dict[str, object]:
    """Redact sensitive values in a shallow mapping used for structured logs."""
    redacted: dict[str, object] = {}
    for key, value in values.items():
        redacted[key] = "<REDACTED>" if _SENSITIVE_NAME.search(key) else value
    return redacted
