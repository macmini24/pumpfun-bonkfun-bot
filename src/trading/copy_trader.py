"""
Copy trading coordinator that mirrors trades from watched wallets.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import monotonic

import uvloop
from solders.pubkey import Pubkey

from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.wallet import Wallet
from interfaces.core import Platform
from monitoring.trade_listener import TradeSignal, TradeSide
from monitoring.trade_listener_factory import TradeListenerFactory
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from utils.logger import get_logger

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logger = get_logger(__name__)


class CopyTrader:
    """Copy trader that mirrors buy/sell actions from specified wallets."""

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        platform: Platform | str = Platform.PUMP_FUN,
        listener_type: str = "blocks",
        trader_addresses: list[str] | None = None,
        copy_buys: bool = True,
        copy_sells: bool = True,
        dedupe_window_seconds: int = 30,
        queue_size: int = 100,
        fast_buy: bool = True,
        fast_buy_min_amount_out: int = 1,
        fast_sell: bool = True,
        fast_sell_min_amount_out: int = 1,
        worker_count: int = 2,
        positions_cache_path: str | None = None,
        positions_flush_interval: float = 1.0,
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        max_retries: int = 3,
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
    ) -> None:
        """Initialize the copy trader."""
        self.solana_client = SolanaClient(rpc_endpoint)
        self.wallet = Wallet(private_key)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )

        if isinstance(platform, str):
            self.platform = Platform(platform)
        else:
            self.platform = platform

        self.copy_buys = copy_buys
        self.copy_sells = copy_sells
        self.dedupe_window_seconds = dedupe_window_seconds
        self.queue_size = queue_size
        self.fast_buy = fast_buy
        self.fast_buy_min_amount_out = max(1, fast_buy_min_amount_out)
        self.fast_sell = fast_sell
        self.fast_sell_min_amount_out = max(1, fast_sell_min_amount_out)
        self.worker_count = max(1, worker_count)
        self.trader_addresses = trader_addresses or []
        self.positions_flush_interval = positions_flush_interval
        self.positions_cache_path = self._resolve_positions_cache_path(
            positions_cache_path
        )

        self.buyer = PlatformAwareBuyer(
            self.solana_client,
            self.wallet,
            self.priority_fee_manager,
            buy_amount,
            buy_slippage,
            max_retries,
            extreme_fast_token_amount=0,
            extreme_fast_mode=False,
            fast_buy=self.fast_buy,
            fast_buy_min_amount_out=self.fast_buy_min_amount_out,
        )

        self.seller = PlatformAwareSeller(
            self.solana_client,
            self.wallet,
            self.priority_fee_manager,
            sell_slippage,
            max_retries,
            fast_sell=self.fast_sell,
            fast_sell_min_amount_out=self.fast_sell_min_amount_out,
        )

        self.trade_listener = TradeListenerFactory.create_listener(
            listener_type=listener_type,
            trader_addresses=self.trader_addresses,
            wss_endpoint=wss_endpoint,
            geyser_endpoint=geyser_endpoint,
            geyser_api_token=geyser_api_token,
            geyser_auth_type=geyser_auth_type,
            platforms=[self.platform],
        )

        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        self._signal_queue: asyncio.Queue[TradeSignal] = asyncio.Queue(
            maxsize=queue_size
        )
        self._processed_signals: dict[str, float] = {}
        self.traded_mints: set = set()
        self.open_positions: set[Pubkey] = self._load_positions_cache()
        self._positions_dirty = False
        self._positions_flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start copy trading."""
        logger.info(f"Starting copy trader on {self.platform.value}")
        logger.info(f"Watching traders: {sorted(self.trader_addresses)}")
        logger.info(f"Copy buys: {self.copy_buys} | Copy sells: {self.copy_sells}")
        if self.fast_buy:
            logger.info(
                f"Fast buy enabled (min out {self.fast_buy_min_amount_out} raw)"
            )
        if self.fast_sell:
            logger.info(
                f"Fast sell enabled (min out {self.fast_sell_min_amount_out} lamports)"
            )
        if self.open_positions:
            logger.info(
                f"Loaded {len(self.open_positions)} cached position(s) from disk"
            )

        processor_tasks = [
            asyncio.create_task(self._process_trade_queue(worker_id))
            for worker_id in range(self.worker_count)
        ]
        try:
            await self.trade_listener.listen_for_trades(self._queue_trade_signal)
        except Exception:
            logger.exception("Copy trading stopped due to error")
        finally:
            for task in processor_tasks:
                task.cancel()
            await asyncio.gather(*processor_tasks, return_exceptions=True)
            await self._cleanup_resources()
            logger.info("Copy trader shutdown complete")

    async def _queue_trade_signal(self, signal: TradeSignal) -> None:
        """Queue a trade signal if it hasn't been processed recently."""
        if signal.platform != self.platform:
            return

        if signal.trader == str(self.wallet.pubkey):
            logger.debug("Skipping self-generated trade signal")
            return

        now = monotonic()
        self._prune_processed(now)
        signal_key = self._build_signal_key(signal)
        if signal_key in self._processed_signals:
            return

        self._processed_signals[signal_key] = now

        try:
            self._signal_queue.put_nowait(signal)
        except asyncio.QueueFull:
            logger.warning("Copy trade queue full, dropping signal")

    async def _process_trade_queue(self, worker_id: int) -> None:
        """Process queued trade signals sequentially."""
        _ = worker_id
        while True:
            try:
                signal = await self._signal_queue.get()
                await self._handle_trade_signal(signal)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error processing copy trade signal")
            finally:
                self._signal_queue.task_done()

    async def _handle_trade_signal(self, signal: TradeSignal) -> None:
        """Handle a single trade signal."""
        if signal.side == TradeSide.BUY and self.copy_buys:
            await self._handle_copy_buy(signal)
            return

        if signal.side == TradeSide.SELL and self.copy_sells:
            await self._handle_copy_sell(signal)
            return

        logger.debug(
            f"Skipping trade signal {signal.side.value} (copy disabled)"
        )

    async def _handle_copy_buy(self, signal: TradeSignal) -> None:
        """Execute a copy buy based on a detected trade."""
        logger.info(
            f"Copying BUY from {signal.trader} for {signal.token_info.mint}"
        )
        if signal.signature:
            logger.info(f"Source signature: {signal.signature}")

        buy_result: TradeResult = await self.buyer.execute(signal.token_info)
        if buy_result.success:
            self.traded_mints.add(signal.token_info.mint)
            self._record_position_open(signal.token_info.mint)
            logger.info(
                f"Copied BUY succeeded for {signal.token_info.mint} "
                f"(tx: {buy_result.tx_signature})"
            )
        else:
            logger.error(
                f"Copied BUY failed for {signal.token_info.mint}: {buy_result.error_message}"
            )
            await handle_cleanup_after_failure(
                self.solana_client,
                self.wallet,
                signal.token_info.mint,
                self.priority_fee_manager,
                self.cleanup_mode,
                self.cleanup_with_priority_fee,
                self.cleanup_force_close_with_burn,
            )

    async def _handle_copy_sell(self, signal: TradeSignal) -> None:
        """Execute a copy sell based on a detected trade."""
        if signal.token_info.mint not in self.open_positions:
            logger.info(
                f"No position for {signal.token_info.mint}. Ignoring sell signal."
            )
            return

        logger.info(
            f"Copying SELL from {signal.trader} for {signal.token_info.mint}"
        )
        if signal.signature:
            logger.info(f"Source signature: {signal.signature}")

        sell_result: TradeResult = await self.seller.execute(signal.token_info)
        if sell_result.success:
            logger.info(
                f"Copied SELL succeeded for {signal.token_info.mint} "
                f"(tx: {sell_result.tx_signature})"
            )
            self._record_position_close(signal.token_info.mint)
            await handle_cleanup_after_sell(
                self.solana_client,
                self.wallet,
                signal.token_info.mint,
                self.priority_fee_manager,
                self.cleanup_mode,
                self.cleanup_with_priority_fee,
                self.cleanup_force_close_with_burn,
            )
        else:
            logger.error(
                f"Copied SELL failed for {signal.token_info.mint}: {sell_result.error_message}"
            )

    async def _cleanup_resources(self) -> None:
        """Perform post-session cleanup."""
        if self.traded_mints:
            try:
                await handle_cleanup_post_session(
                    self.solana_client,
                    self.wallet,
                    list(self.traded_mints),
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn,
                )
            except Exception:
                logger.exception("Error during copy trader cleanup")

        await self.solana_client.close()
        await self._flush_positions_cache(force=True)

    def _build_signal_key(self, signal: TradeSignal) -> str:
        """Build a unique key for deduping trade signals."""
        if signal.signature:
            return f"{signal.signature}:{signal.side.value}"
        return f"{signal.trader}:{signal.side.value}:{signal.token_info.mint}"

    def _prune_processed(self, now: float) -> None:
        """Remove old entries from the processed signal cache."""
        expired = [
            key
            for key, timestamp in self._processed_signals.items()
            if now - timestamp > self.dedupe_window_seconds
        ]
        for key in expired:
            self._processed_signals.pop(key, None)

    def _resolve_positions_cache_path(self, cache_path: str | None) -> Path:
        """Resolve the positions cache path.

        Args:
            cache_path: Optional explicit cache path

        Returns:
            Path to the cache file
        """
        if cache_path:
            return Path(cache_path)
        return Path("state") / "copy_trader_positions.json"

    def _load_positions_cache(self) -> set[Pubkey]:
        """Load open positions from disk cache."""
        if not self.positions_cache_path.exists():
            return set()

        try:
            raw = self.positions_cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                positions = data.get("positions", [])
            elif isinstance(data, list):
                positions = data
            else:
                positions = []
            result = set()
            for mint_str in positions:
                if isinstance(mint_str, str):
                    try:
                        result.add(Pubkey.from_string(mint_str))
                    except Exception:
                        continue
            return result
        except Exception:
            logger.exception("Failed to load positions cache")
            return set()

    def _record_position_open(self, mint: Pubkey) -> None:
        """Record an opened position and schedule a cache flush."""
        self.open_positions.add(mint)
        self._schedule_positions_flush()

    def _record_position_close(self, mint: Pubkey) -> None:
        """Record a closed position and schedule a cache flush."""
        self.open_positions.discard(mint)
        self._schedule_positions_flush()

    def _schedule_positions_flush(self) -> None:
        """Schedule a background flush for positions cache."""
        self._positions_dirty = True
        if self._positions_flush_task and not self._positions_flush_task.done():
            return
        self._positions_flush_task = asyncio.create_task(
            self._flush_positions_cache()
        )

    async def _flush_positions_cache(self, force: bool = False) -> None:
        """Flush positions cache to disk."""
        if not force:
            await asyncio.sleep(self.positions_flush_interval)

        if not self._positions_dirty and not force:
            return

        self._positions_dirty = False
        await asyncio.to_thread(self._persist_positions_sync)

    def _persist_positions_sync(self) -> None:
        """Persist positions cache to disk (sync)."""
        try:
            self.positions_cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = sorted(str(mint) for mint in self.open_positions)
            self.positions_cache_path.write_text(
                json.dumps(payload), encoding="utf-8"
            )
        except Exception:
            logger.exception("Failed to persist positions cache")
