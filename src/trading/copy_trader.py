"""
Copy trading coordinator that mirrors trades from watched wallets.
"""

from __future__ import annotations

import asyncio
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
        self.trader_addresses = trader_addresses or []

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
        self.open_positions: set[Pubkey] = set()

    async def start(self) -> None:
        """Start copy trading."""
        logger.info(f"Starting copy trader on {self.platform.value}")
        logger.info(f"Watching traders: {sorted(self.trader_addresses)}")
        logger.info(f"Copy buys: {self.copy_buys} | Copy sells: {self.copy_sells}")

        processor_task = asyncio.create_task(self._process_trade_queue())
        try:
            await self.trade_listener.listen_for_trades(self._queue_trade_signal)
        except Exception:
            logger.exception("Copy trading stopped due to error")
        finally:
            processor_task.cancel()
            try:
                await processor_task
            except asyncio.CancelledError:
                pass
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

    async def _process_trade_queue(self) -> None:
        """Process queued trade signals sequentially."""
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
            self.open_positions.add(signal.token_info.mint)
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
            self.open_positions.discard(signal.token_info.mint)
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
