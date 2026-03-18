"""Entry point for the Spread Capture Market Making Bot."""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("spread_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    args = sys.argv[1:]
    paper_mode = "--paper" in args or os.environ.get("PAPER_MODE", "").lower() == "true"
    port = int(os.environ.get("SPREAD_PORT", "8091"))

    # Always paper unless explicitly live
    if not paper_mode:
        paper_mode = True  # default to paper for safety

    print("  *** SPREAD CAPTURE BOT ***")
    if paper_mode:
        print("  *** PAPER MODE – no real orders will be placed ***")

    from spreadcapture.web_server import run_spread_web
    asyncio.run(run_spread_web(underlying="BTC", paper=paper_mode, port=port))


if __name__ == "__main__":
    main()
