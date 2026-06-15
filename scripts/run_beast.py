"""
CLI entry. Usage:
    SERPER_API_KEY=... X402_PROXY_SECRET=... python -m scripts.run_beast
    python -m scripts.run_beast --only mastersheets
"""

import argparse
import sys

from sml_beast.orchestrator import main


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="sml-beast", description="SML autonomous SEO orchestrator")
    ap.add_argument("--only", nargs="*", choices=["mastersheets", "xrpl_x402"],
                    help="run only the named verticals (default: both)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    verticals = tuple(args.only) if args.only else None
    sys.exit(main(verticals))
