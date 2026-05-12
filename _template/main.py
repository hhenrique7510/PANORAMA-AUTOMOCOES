"""Entry point — substitua pela lógica da sua automação."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Descrição da automação.")
    p.add_argument("--debug", action="store_true", help="Logs verbosos")
    p.add_argument("--limit", type=int, default=None, help="Limita N items (debug)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("main")
    log.info("automation start (limit=%s, debug=%s)", args.limit, args.debug)

    # TODO: chame seu worker aqui
    # from worker import run
    # run(limit=args.limit)


if __name__ == "__main__":
    main()
