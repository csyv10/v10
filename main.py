"""Entry point for the Polymarket Dutch Book Arbitrage Bot."""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8")],
)

# Suppress noisy libraries
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    args = sys.argv[1:]
    web_mode   = "--web"   in args
    # --paper flag OR PAPER_MODE=true in .env (set by the web settings UI)
    paper_mode = "--paper" in args or os.environ.get("PAPER_MODE", "").lower() == "true"
    underlying_args = [a for a in args if not a.startswith("-")]

    if underlying_args:
        underlyings = [a.upper() for a in underlying_args]
    else:
        underlyings = [os.environ.get("UNDERLYING", "BTC").upper()]

    if not paper_mode and not os.environ.get("PRIVATE_KEY"):
        print(
            "ERROR: PRIVATE_KEY environment variable not set.\n"
            "Copy .env.example → .env and fill in your private key.\n"
            "To run without a key, use --paper for paper-trading mode.",
            file=sys.stderr,
        )
        sys.exit(1)

    if paper_mode:
        print("  *** PAPER MODE - no real orders will be placed ***")

    if web_mode:
        import asyncio
        from ui.web_server import run_web_bot
        port = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8090")))
        asyncio.run(run_web_bot(underlyings=underlyings, paper=paper_mode, port=port))
    else:
        from ui.dashboard import PolymarketDashboard
        app = PolymarketDashboard(underlying=underlyings[0], paper=paper_mode)
        app.run()


if __name__ == "__main__":
    main()
