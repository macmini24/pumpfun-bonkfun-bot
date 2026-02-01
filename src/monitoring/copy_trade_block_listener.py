"""
BlockSubscribe-based trade listener for copy trading.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable

import websockets
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from interfaces.core import Platform
from monitoring.trade_listener import BaseTradeListener, TradeSignal
from monitoring.trade_parsers import TradeInstructionParser
from utils.logger import get_logger

logger = get_logger(__name__)


class CopyTradeBlockListener(BaseTradeListener):
    """Listen for trade instructions via blockSubscribe."""

    def __init__(
        self,
        wss_endpoint: str,
        trader_addresses: list[str],
        platforms: list[Platform] | None = None,
    ) -> None:
        """Initialize the copy trade block listener.

        Args:
            wss_endpoint: WebSocket endpoint URL
            trader_addresses: Trader wallet addresses to watch
            platforms: Platforms to monitor (if None, monitor all supported platforms)
        """
        super().__init__(trader_addresses, platforms)
        self.wss_endpoint = wss_endpoint
        self.ping_interval = 20

        from platforms import platform_factory

        if platforms is None:
            self.platforms = platform_factory.get_supported_platforms()
        else:
            self.platforms = platforms

        self.program_id_to_parser: dict[str, TradeInstructionParser] = {}
        for platform in self.platforms:
            try:
                parser = TradeInstructionParser(platform)
                self.program_id_to_parser[str(parser.program_id)] = parser
                logger.info(
                    f"Registered trade parser for {platform.value} "
                    f"(program {parser.program_id})"
                )
            except Exception as exc:
                logger.warning(
                    f"Could not register trade parser for {platform.value}: {exc}"
                )

        if not self.program_id_to_parser:
            raise ValueError("No trade parsers available for copy trading")

    async def listen_for_trades(
        self,
        trade_callback: Callable[[TradeSignal], Awaitable[None]],
    ) -> None:
        """Listen for trade instructions involving watched traders.

        Args:
            trade_callback: Callback for each trade signal
        """
        if not self.trader_addresses:
            logger.error("No trader addresses configured for copy trading.")
            return

        while True:
            try:
                async with websockets.connect(self.wss_endpoint) as websocket:
                    subscriptions = await self._subscribe_to_traders(websocket)
                    ping_task = asyncio.create_task(self._ping_loop(websocket))

                    try:
                        while True:
                            trade_signals = await self._wait_for_trade_signals(
                                websocket, subscriptions
                            )
                            for signal in trade_signals:
                                await trade_callback(signal)
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning(
                            "Copy trade WebSocket connection closed. Reconnecting..."
                        )
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except Exception:
                logger.exception("Copy trade WebSocket error")
                logger.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    async def _subscribe_to_traders(
        self, websocket: websockets.WebSocketServerProtocol
    ) -> dict[int, str]:
        """Subscribe to block notifications per watched trader.

        Args:
            websocket: Active WebSocket connection

        Returns:
            Mapping of subscription IDs to trader addresses
        """
        subscriptions: dict[int, str] = {}
        for i, trader_address in enumerate(sorted(self.trader_addresses)):
            subscription_message = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "blockSubscribe",
                    "params": [
                        {"mentionsAccountOrProgram": trader_address},
                        {
                            "commitment": "confirmed",
                            "encoding": "base64",
                            "showRewards": False,
                            "transactionDetails": "full",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                }
            )

            await websocket.send(subscription_message)
            response = await websocket.recv()
            response_data = json.loads(response)

            if "result" in response_data:
                subscription_id = response_data["result"]
                subscriptions[subscription_id] = trader_address
                logger.info(
                    f"Subscribed to trades for {trader_address} (subscription {subscription_id})"
                )
            else:
                logger.warning(
                    f"Unexpected subscription response for {trader_address}: {response}"
                )

        return subscriptions

    async def _ping_loop(self, websocket: websockets.WebSocketServerProtocol) -> None:
        """Keep connection alive with pings."""
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                try:
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=10)
                except TimeoutError:
                    logger.warning("Ping timeout - server not responding")
                    await websocket.close()
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ping error")

    async def _wait_for_trade_signals(
        self,
        websocket: websockets.WebSocketServerProtocol,
        subscriptions: dict[int, str],
    ) -> list[TradeSignal]:
        """Wait for trade-related block notifications.

        Args:
            websocket: Active WebSocket connection
            subscriptions: Mapping of subscription IDs to trader addresses

        Returns:
            List of trade signals detected in the block
        """
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = json.loads(response)

            if "method" not in data or data["method"] != "blockNotification":
                return []

            params = data.get("params", {})
            subscription_id = params.get("subscription")
            trader_address = (
                subscriptions.get(subscription_id) if subscription_id else None
            )

            result = params.get("result", {})
            block_value = result.get("value", {})
            block = block_value.get("block", {})
            transactions = block.get("transactions", [])

            return self._process_block_transactions(transactions, trader_address)

        except TimeoutError:
            logger.debug("No block data received for 30 seconds")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            raise
        except Exception:
            logger.exception("Error processing block notification")

        return []

    def _process_block_transactions(
        self, transactions: list, trader_address: str | None
    ) -> list[TradeSignal]:
        """Process block transactions and extract trade signals.

        Args:
            transactions: List of transaction objects from the block
            trader_address: Trader address associated with the subscription

        Returns:
            List of trade signals
        """
        trade_signals: list[TradeSignal] = []
        for tx in transactions:
            if not isinstance(tx, dict) or "transaction" not in tx:
                continue

            tx_data = tx["transaction"]
            if isinstance(tx_data, list) and tx_data:
                trade_signals.extend(
                    self._parse_encoded_transaction(tx_data[0], trader_address)
                )
            elif isinstance(tx_data, dict) and "message" in tx_data:
                trade_signals.extend(
                    self._parse_decoded_transaction(tx_data, trader_address)
                )

        return trade_signals

    def _parse_encoded_transaction(
        self, encoded_data: str, trader_address: str | None
    ) -> list[TradeSignal]:
        """Parse a base64-encoded transaction for trade instructions."""
        trade_signals: list[TradeSignal] = []
        try:
            tx_bytes = base64.b64decode(encoded_data)
            transaction = VersionedTransaction.from_bytes(tx_bytes)
            message = transaction.message
            account_keys = [bytes(key) for key in message.account_keys]
            signature = (
                str(transaction.signatures[0]) if transaction.signatures else None
            )

            for ix in message.instructions:
                program_id = str(message.account_keys[ix.program_id_index])
                parser = self.program_id_to_parser.get(program_id)
                if not parser:
                    continue

                parsed = parser.parse_instruction(
                    bytes(ix.data), list(ix.accounts), account_keys
                )
                if not parsed:
                    continue

                if trader_address and parsed.trader != trader_address:
                    continue

                if not self.is_trader_watched(parsed.trader):
                    continue

                trade_signals.append(
                    TradeSignal(
                        side=parsed.side,
                        token_info=parsed.token_info,
                        trader=parsed.trader,
                        platform=parsed.platform,
                        signature=signature,
                    )
                )

        except Exception:
            logger.exception("Failed to parse encoded transaction")

        return trade_signals

    def _parse_decoded_transaction(
        self, tx_data: dict, trader_address: str | None
    ) -> list[TradeSignal]:
        """Parse a decoded transaction for trade instructions."""
        trade_signals: list[TradeSignal] = []
        try:
            message = tx_data["message"]
            account_keys_raw = message.get("accountKeys", [])
            account_keys = [
                Pubkey.from_string(key["pubkey"]).to_bytes()
                if isinstance(key, dict)
                else Pubkey.from_string(key).to_bytes()
                for key in account_keys_raw
            ]
            signature_list = tx_data.get("signatures", [])
            signature = signature_list[0] if signature_list else None

            for ix in message.get("instructions", []):
                if "programIdIndex" not in ix or "data" not in ix:
                    continue

                program_idx = ix["programIdIndex"]
                if program_idx >= len(account_keys_raw):
                    continue

                program_key = account_keys_raw[program_idx]
                program_id = (
                    program_key.get("pubkey") if isinstance(program_key, dict) else None
                ) or program_key

                parser = self.program_id_to_parser.get(program_id)
                if not parser:
                    continue

                ix_data = base64.b64decode(ix["data"])
                accounts = list(ix.get("accounts", []))

                parsed = parser.parse_instruction(ix_data, accounts, account_keys)
                if not parsed:
                    continue

                if trader_address and parsed.trader != trader_address:
                    continue

                if not self.is_trader_watched(parsed.trader):
                    continue

                trade_signals.append(
                    TradeSignal(
                        side=parsed.side,
                        token_info=parsed.token_info,
                        trader=parsed.trader,
                        platform=parsed.platform,
                        signature=signature,
                    )
                )

        except Exception:
            logger.exception("Failed to parse decoded transaction")

        return trade_signals
