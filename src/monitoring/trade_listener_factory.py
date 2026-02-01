"""
Factory for creating trade listeners used by copy trading.
"""

from interfaces.core import Platform
from monitoring.trade_listener import BaseTradeListener
from utils.logger import get_logger

logger = get_logger(__name__)


class TradeListenerFactory:
    """Factory for creating trade listeners based on configuration."""

    @staticmethod
    def create_listener(
        listener_type: str,
        trader_addresses: list[str],
        wss_endpoint: str | None = None,
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        platforms: list[Platform] | None = None,
    ) -> BaseTradeListener:
        """Create a trade listener based on the specified type.

        Args:
            listener_type: Listener type ("blocks" or "geyser")
            trader_addresses: Trader wallet addresses to watch
            wss_endpoint: WebSocket endpoint URL (blocks listener)
            geyser_endpoint: Geyser gRPC endpoint URL
            geyser_api_token: Geyser API token
            geyser_auth_type: Geyser authentication type
            platforms: List of platforms to monitor

        Returns:
            Configured trade listener
        """
        listener_type = listener_type.lower()

        if listener_type == "blocks":
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint is required for blocks listener")

            from monitoring.copy_trade_block_listener import CopyTradeBlockListener

            listener = CopyTradeBlockListener(
                wss_endpoint=wss_endpoint,
                trader_addresses=trader_addresses,
                platforms=platforms,
            )
            logger.info("Created copy trade block listener")
            return listener

        if listener_type == "geyser":
            if not geyser_endpoint or not geyser_api_token:
                raise ValueError(
                    "Geyser endpoint and API token are required for geyser listener"
                )

            from monitoring.copy_trade_geyser_listener import CopyTradeGeyserListener

            listener = CopyTradeGeyserListener(
                geyser_endpoint=geyser_endpoint,
                geyser_api_token=geyser_api_token,
                geyser_auth_type=geyser_auth_type,
                trader_addresses=trader_addresses,
                platforms=platforms,
            )
            logger.info("Created copy trade geyser listener")
            return listener

        raise ValueError(
            f"Invalid copy trade listener type '{listener_type}'. "
            "Must be one of: 'blocks', 'geyser'"
        )

    @staticmethod
    def get_supported_listener_types() -> list[str]:
        """Get list of supported copy trade listener types."""
        return ["blocks", "geyser"]
