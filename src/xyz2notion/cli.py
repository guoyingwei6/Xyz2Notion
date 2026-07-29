"""Command-line interface for Xyz2Notion."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from xyz2notion import __version__
from xyz2notion.config import ConfigurationError, config_schema_json, load_config
from xyz2notion.security import CredentialKind, allowed_hosts


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="xyz2notion",
        description="Sync Xiaoyuzhou podcasts and enrichments to Notion.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="check the local installation")
    config_check = subparsers.add_parser("config-check", help="validate a public YAML config")
    config_check.add_argument("--config", default="config.yaml", help="path to config.yaml")
    subparsers.add_parser("config-schema", help="print the generated JSON Schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        protected_services = len(CredentialKind)
        protected_hosts = sum(len(allowed_hosts(kind)) for kind in CredentialKind)
        print(
            f"Xyz2Notion {__version__}: OK "
            f"({protected_services} credential types, {protected_hosts} allowed hosts)"
        )
        return 0
    if args.command == "config-check":
        try:
            config = load_config(args.config)
        except ConfigurationError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        providers = ", ".join(provider.value for provider in config.asr.provider_order)
        print(f"Configuration OK (schema v{config.schema_version}, ASR: {providers})")
        return 0
    if args.command == "config-schema":
        print(config_schema_json())
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
