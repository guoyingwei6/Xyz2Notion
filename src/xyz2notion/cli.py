"""Command-line interface for Xyz2Notion."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from xyz2notion import __version__
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

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
