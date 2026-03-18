"""Polymarket Dutch Book Bot — Textual terminal dashboard.

Layout:
  ┌────────────────────────────────────────────────────────────────────┐
  │  HEADER: bot name | status | PnL                                   │
  ├─────────────────┬──────────────────────┬───────────────────────────┤
  │  MARKET INFO    │  UP ORDERBOOK        │  DOWN ORDERBOOK           │
  ├─────────────────┴──────────────────────┴───────────────────────────┤
  │  LOG (scrolling)                                                    │
  └────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Log,
    Static,
)
from textual.widget import Widget
from textual import work

from config import ORDERBOOK_DEPTH, UI_REFRESH_HZ, STOP_BEFORE_END_MS
from bot.bot_engine import BotEngine
from bot.orderbook import Orderbook, Level


# ── Helper widgets ─────────────────────────────────────────────────────────────

class MarketInfoPanel(Static):
    """Left panel showing market metadata and current positions."""

    DEFAULT_CSS = """
    MarketInfoPanel {
        width: 28;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
    }
    MarketInfoPanel .section-title {
        color: $accent;
        text-style: bold;
        padding: 0 0 1 0;
    }
    MarketInfoPanel .label {
        color: $text-muted;
    }
    MarketInfoPanel .value {
        color: $text;
        text-style: bold;
    }
    MarketInfoPanel .value-good {
        color: $success;
        text-style: bold;
    }
    MarketInfoPanel .value-bad {
        color: $error;
        text-style: bold;
    }
    MarketInfoPanel .value-warn {
        color: $warning;
        text-style: bold;
    }
    """


class OrderbookPanel(Widget):
    """Displays a single-side orderbook as a DataTable."""

    DEFAULT_CSS = """
    OrderbookPanel {
        border: solid $primary;
        background: $surface;
    }
    OrderbookPanel DataTable {
        height: 1fr;
    }
    """

    def __init__(self, title: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title

    def compose(self) -> ComposeResult:
        yield Label(f" {self._title} ", classes="ob-title")
        yield DataTable(id=f"ob-{self._title.lower().replace(' ', '-')}", show_cursor=False)

    def on_mount(self) -> None:
        table: DataTable = self.query_one(DataTable)
        table.add_columns("Side", "Price", "Size")

    def update(self, ob: Optional[Orderbook], depth: int = ORDERBOOK_DEPTH) -> None:
        table: DataTable = self.query_one(DataTable)
        table.clear()
        if ob is None:
            return

        asks = ob.top_asks(depth)
        bids = ob.top_bids(depth)

        # Asks (sell pressure) — display in reverse so best ask is at bottom
        for lvl in reversed(asks):
            marker = "◄" if lvl == asks[0] else " "
            table.add_row("ASK", f"{lvl.price:.4f}{marker}", f"{lvl.size:.1f}", key=None)

        # Separator
        table.add_row("─", "────────", "────────", key=None)

        # Bids
        for lvl in bids:
            marker = "◄" if lvl == bids[0] else " "
            table.add_row("BID", f"{lvl.price:.4f}{marker}", f"{lvl.size:.1f}", key=None)


# ── Main app ────────────────────────────────────────────────────────────────────

class PolymarketDashboard(App):
    """Polymarket Dutch Book Arbitrage Terminal."""

    TITLE = "Polymarket Dutch Book Bot"
    CSS = """
    Screen {
        background: $background;
    }

    #header-bar {
        height: 3;
        background: $primary-darken-2;
        color: $text;
        padding: 1 2;
        dock: top;
    }

    #status-label {
        width: 1fr;
        text-align: center;
        text-style: bold;
    }

    #pnl-label {
        width: 20;
        text-align: right;
        text-style: bold;
    }

    #main-row {
        height: 1fr;
        margin-top: 3;
    }

    #info-panel {
        width: 30;
        border: solid $primary;
        padding: 1 1;
        background: $surface;
    }

    #ob-up {
        width: 1fr;
        border: solid $accent;
        background: $surface;
    }

    #ob-down {
        width: 1fr;
        border: solid $warning;
        background: $surface;
    }

    #log-panel {
        height: 12;
        border: solid $primary;
        background: $surface-darken-1;
        dock: bottom;
        padding: 0 1;
    }

    .ob-title {
        text-style: bold;
        padding: 0 1;
    }

    #ob-up .ob-title {
        color: $accent;
    }

    #ob-down .ob-title {
        color: $warning;
    }

    .info-section {
        color: $accent;
        text-style: bold;
        padding: 1 0 0 0;
    }

    .info-key {
        color: $text-muted;
    }

    .info-val {
        color: $text;
        text-style: bold;
    }

    .good { color: $success; text-style: bold; }
    .bad  { color: $error;   text-style: bold; }
    .warn { color: $warning; text-style: bold; }

    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Force refresh"),
        Binding("h", "toggle_halt", "Halt/Resume"),
    ]

    def __init__(self, underlying: str = "BTC", paper: bool = False) -> None:
        super().__init__()
        self._engine = BotEngine(underlying=underlying, paper=paper)
        self._engine_task: Optional[asyncio.Task] = None

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # Custom header bar
        with Horizontal(id="header-bar"):
            yield Label("▶  POLYMARKET DUTCH BOOK BOT", id="bot-name")
            yield Label("Status: STARTING", id="status-label")
            yield Label("PnL: $0.00", id="pnl-label")

        with Horizontal(id="main-row"):
            yield Static("", id="info-panel")
            with Vertical(id="ob-up"):
                yield Label(" UP ORDERBOOK ", classes="ob-title")
                yield DataTable(id="table-up", show_cursor=False)
            with Vertical(id="ob-down"):
                yield Label(" DOWN ORDERBOOK ", classes="ob-title")
                yield DataTable(id="table-down", show_cursor=False)

        yield Log(id="log-panel", auto_scroll=True, max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        # Initialise orderbook tables
        for tid in ("table-up", "table-down"):
            t: DataTable = self.query_one(f"#{tid}", DataTable)
            t.add_columns("Side", "Price", "Size")

        # Pipe engine logs into the Textual Log widget
        self._engine.log_lines  # access to prime the deque
        self._engine_task = asyncio.create_task(self._engine.run())

        # Redirect engine log function to also write to UI
        original_log = self._engine.log
        def patched_log(msg: str) -> None:
            original_log(msg)
            try:
                log_widget: Log = self.query_one("#log-panel", Log)
                log_widget.write_line(self._engine.log_lines[-1] if self._engine.log_lines else msg)
            except Exception:
                pass
        self._engine.log = patched_log

        # Start periodic UI refresh
        self.set_interval(1 / UI_REFRESH_HZ, self._refresh_ui)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_quit(self) -> None:
        self._engine.stop()
        self.exit()

    def action_refresh(self) -> None:
        self._refresh_ui()

    def action_toggle_halt(self) -> None:
        risk = self._engine.risk
        if risk.is_halted:
            risk.resume()
            self._engine.log("Trading RESUMED by user")
        else:
            risk.halt("Manually halted by user")

    # ── Periodic refresh ──────────────────────────────────────────────────────

    def _refresh_ui(self) -> None:
        engine = self._engine
        pair = engine._current_pair
        pos = engine.tracker.position

        # ── Header ──────────────────────────────────────────────────────────
        status = engine.status
        if engine.risk.is_halted:
            status = f"[bold red]HALTED[/]"
        self.query_one("#status-label", Label).update(f"Status: {status}")

        total_pnl = engine.tracker.total_pnl
        pnl_class = "good" if total_pnl >= 0 else "bad"
        self.query_one("#pnl-label", Label).update(
            f"PnL: [bold {'green' if total_pnl >= 0 else 'red'}]"
            f"{'+'if total_pnl>=0 else ''}${total_pnl:.2f}[/]"
        )

        # ── Info panel ───────────────────────────────────────────────────────
        info_lines: list[str] = []

        info_lines.append("[bold cyan]── MARKET ──────────────[/]")
        if pair is not None:
            info_lines.append(f"[dim]Underlying:[/]  [bold]{engine.underlying}[/]")
            slug = pair.short_label()
            info_lines.append(f"[dim]Slug:[/]  [bold]{slug[:22]}[/]")
            up_short = pair.up_token_id[:10] + "…"
            down_short = pair.down_token_id[:10] + "…"
            info_lines.append(f"[dim]UP token:[/]  [bold]{up_short}[/]")
            info_lines.append(f"[dim]DN token:[/]  [bold]{down_short}[/]")

            ms_left = pair.ms_until_end
            secs_left = max(0, ms_left / 1000)
            mins = int(secs_left // 60)
            secs = int(secs_left % 60)
            if ms_left <= STOP_BEFORE_END_MS:
                time_col = "bold red"
            elif secs_left < 60:
                time_col = "bold yellow"
            else:
                time_col = "bold green"
            info_lines.append(f"[dim]Remaining:[/]  [{time_col}]{mins:02d}:{secs:02d}[/]")

            # Dutch book indicator
            ob_up = engine.ob_up
            ob_down = engine.ob_down
            sum_ask = None
            if ob_up and ob_down:
                a_up = ob_up.best_ask
                a_dn = ob_down.best_ask
                if a_up and a_dn:
                    sum_ask = a_up + a_dn
                    from config import SPREAD_THRESHOLD
                    db_active = sum_ask < (1.0 - SPREAD_THRESHOLD)
                    db_col = "bold green" if db_active else "dim white"
                    db_str = f"{sum_ask:.4f}" + (" ✓ ARBIT" if db_active else "")
                    info_lines.append(f"[dim]Sum ask:[/]  [{db_col}]{db_str}[/]")
        else:
            info_lines.append("[dim]No active market pair[/]")

        info_lines.append("")
        info_lines.append("[bold cyan]── POSITIONS ────────────[/]")
        info_lines.append(f"[dim]UP shares:[/]   [bold]{pos.up_shares}[/]")
        info_lines.append(f"[dim]DOWN shares:[/] [bold]{pos.down_shares}[/]")
        delta_pct = pos.share_delta_pct * 100
        delta_col = "bold red" if delta_pct > 3 else "bold green"
        info_lines.append(f"[dim]Δ shares:[/]   [{delta_col}]{delta_pct:.1f}%[/]")
        if pos.avg_sum is not None:
            avg_col = "bold green" if pos.avg_sum < 1.0 else "bold red"
            info_lines.append(f"[dim]Avg sum:[/]    [{avg_col}]{pos.avg_sum:.4f}[/]")

        info_lines.append("")
        info_lines.append("[bold cyan]── PnL ──────────────────[/]")
        unrealised = engine.tracker.unrealised_pnl
        realised = engine.tracker.realised_pnl
        u_col = "bold green" if unrealised >= 0 else "bold red"
        r_col = "bold green" if realised >= 0 else "bold red"
        t_col = "bold green" if total_pnl >= 0 else "bold red"
        info_lines.append(f"[dim]Unrealised:[/] [{u_col}]${unrealised:+.2f}[/]")
        info_lines.append(f"[dim]Realised:[/]   [{r_col}]${realised:+.2f}[/]")
        info_lines.append(f"[dim]Total:[/]      [{t_col}]${total_pnl:+.2f}[/]")
        info_lines.append(f"[dim]Trades:[/]     [bold]{engine.tracker.trade_count}[/]")

        if engine.risk.is_halted:
            info_lines.append("")
            info_lines.append(f"[bold red]⚠ HALTED[/]")
            info_lines.append(f"[dim]{engine.risk.halt_reason[:24]}[/]")

        self.query_one("#info-panel", Static).update("\n".join(info_lines))

        # ── Orderbooks ───────────────────────────────────────────────────────
        self._update_ob_table("table-up", engine.ob_up)
        self._update_ob_table("table-down", engine.ob_down)

    def _update_ob_table(self, table_id: str, ob: Optional[Orderbook]) -> None:
        table: DataTable = self.query_one(f"#{table_id}", DataTable)
        table.clear()
        if ob is None:
            table.add_row("—", "loading…", "—")
            return

        asks = ob.top_asks(ORDERBOOK_DEPTH)
        bids = ob.top_bids(ORDERBOOK_DEPTH)

        # Asks in reverse order so best ask sits nearest the mid
        for i, lvl in enumerate(reversed(asks)):
            is_best = (i == len(asks) - 1)
            marker = "◄" if is_best else " "
            price_str = f"[red]{lvl.price:.4f}[/]{marker}"
            table.add_row("ASK", price_str, f"{lvl.size:.1f}")

        table.add_row("", "[dim]────────[/]", "[dim]────────[/]")

        for i, lvl in enumerate(bids):
            is_best = (i == 0)
            marker = "◄" if is_best else " "
            price_str = f"[green]{lvl.price:.4f}[/]{marker}"
            table.add_row("BID", price_str, f"{lvl.size:.1f}")
