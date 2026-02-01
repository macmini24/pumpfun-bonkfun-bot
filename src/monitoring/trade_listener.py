"""
Trade listener interfaces and signal structures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from interfaces.core import Platform, TokenInfo


class TradeSide(Enum):
    """Trade side for a copy trading signal."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class TradeSignal:
    """Represents a trade action detected for a watched trader."""

    side: TradeSide
    token_info: TokenInfo
    trader: str
    platform: Platform
    signature: str | None = None


class BaseTradeListener(ABC):
    """Base abstract class for trade listeners."""

    def __init__(self, trader_addresses: list[str], platforms: list[Platform] | None):
        """Initialize the listener.

        Args:
            trader_addresses: Wallet addresses to watch for trades
            platforms: Platforms to monitor (None = all supported)
        """
        self.trader_addresses = {addr for addr in trader_addresses if addr}
        self.platforms = platforms

    @abstractmethod
    async def listen_for_trades(
        self,
        trade_callback: Callable[[TradeSignal], Awaitable[None]],
    ) -> None:
        """Listen for trades and invoke callback per trade.

        Args:
            trade_callback: Callback for each detected trade signal
        """
        raise NotImplementedError

    def is_trader_watched(self, trader: str) -> bool:
        """Check if a trader address is in the watchlist.

        Args:
            trader: Trader wallet address (base58)

        Returns:
            True if the trader is being watched
        """
        return trader in self.trader_addresses
