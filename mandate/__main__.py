"""Command-line entry point: ``python -m mandate``."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from typing import Any

from .application import ApplicationError
from .demo import run_demo, run_scenarios
from .web import serve

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_tests() -> int:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    suite = unittest.defaultTestLoader.discover(
        str(PROJECT_ROOT / "tests"),
        pattern="test_*.py",
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def _make_agent(mode: str) -> Any | None:
    if mode == "llm":
        from .agent import OpenAICompatibleAgent

        return OpenAICompatibleAgent()
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mandate",
        description=(
            "Mandate — intent-bound purchase agent sandbox prototype. "
            "Synthetic demo data only; not production banking software."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="run the full sandbox walkthrough")
    demo.add_argument(
        "--no-edge",
        action="store_true",
        help="skip the injection, tamper and retry probes",
    )
    demo.add_argument(
        "--agent",
        choices=("demo", "llm"),
        default="demo",
        help="demo-deterministic extractor (default) or a configured OpenAI-compatible LLM",
    )

    sub.add_parser("scenarios", help="print the nine documented engine scenarios")

    server = sub.add_parser("serve", help="start the local dashboard")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--no-browser", action="store_true")
    server.add_argument(
        "--agent",
        choices=("demo", "llm"),
        default="demo",
        help="demo-deterministic extractor (default) or a configured OpenAI-compatible LLM",
    )

    sub.add_parser("test", help="run the full test suite")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(parser, args)
    except ApplicationError as exc:
        print(f"\n  error: {exc}", file=sys.stderr)
        if getattr(args, "agent", "demo") == "llm":
            print(
                "  hint: --agent llm needs an OpenAI-compatible endpoint. Set "
                "MANDATE_LLM_BASE_URL, MANDATE_LLM_MODEL and "
                "MANDATE_LLM_API_KEY (see .env.example).",
                file=sys.stderr,
            )
        return 1


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command in (None, "demo"):
        include_edges = not getattr(args, "no_edge", False)
        agent = _make_agent(getattr(args, "agent", "demo"))
        print("")
        print("  MANDATE  |  sandbox prototype  |  synthetic demo data only")
        return run_demo(include_edges=include_edges, agent=agent)
    if args.command == "scenarios":
        return 0 if run_scenarios() else 1
    if args.command == "serve":
        agent = _make_agent(getattr(args, "agent", "demo"))
        serve(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            agent=agent,
        )
        return 0
    if args.command == "test":
        return _run_tests()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
