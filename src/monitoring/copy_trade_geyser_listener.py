"""
Geyser-based trade listener for copy trading.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import base58
import grpc
from solders.pubkey import Pubkey

from geyser.generated import geyser_pb2, geyser_pb2_grpc
from interfaces.core import Platform
from monitoring.trade_listener import BaseTradeListener, TradeSignal
from monitoring.trade_parsers import TradeInstructionParser
from utils.logger import get_logger

logger = get_logger(__name__)


class CopyTradeGeyserListener(BaseTradeListener):
    """Listen for trade instructions via a Geyser stream."""

    def __init__(
        self,
        geyser_endpoint: str,
        geyser_api_token: str,
        geyser_auth_type: str,
        trader_addresses: list[str],
        platforms: list[Platform] | None = None,
    ) -> None:
        """Initialize the Geyser trade listener."""
        super().__init__(trader_addresses, platforms)
        self.geyser_endpoint = geyser_endpoint
        self.geyser_api_token = geyser_api_token

        valid_auth_types = {"x-token", "basic"}
        self.auth_type: str = (geyser_auth_type or "x-token").lower()
        if self.auth_type not in valid_auth_types:
            raise ValueError(
                f"Unsupported auth_type={self.auth_type!r}. "
                f"Expected one of {valid_auth_types}"
            )

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

        self.program_ids = sorted(self.program_id_to_parser.keys())

    async def listen_for_trades(
        self,
        trade_callback: Callable[[TradeSignal], Awaitable[None]],
    ) -> None:
        """Listen for trade instructions using Geyser subscription."""
        if not self.trader_addresses:
            logger.error("No trader addresses configured for copy trading.")
            return

        while True:
            try:
                stub, channel = await self._create_geyser_connection()
                request = self._create_subscription_request()

                logger.info(f"Connected to Geyser endpoint: {self.geyser_endpoint}")
                logger.info(
                    f"Monitoring traders: {sorted(self.trader_addresses)}"
                )

                try:
                    async for update in stub.Subscribe(iter([request])):
                        trade_signals = self._process_update(update)
                        for signal in trade_signals:
                            await trade_callback(signal)
                except Exception as exc:
                    if isinstance(exc, grpc.aio.AioRpcError):
                        logger.exception(f"gRPC error: {exc.details()}")
                    else:
                        logger.exception("Geyser error occurred")
                    await asyncio.sleep(5)
                finally:
                    await channel.close()

            except Exception:
                logger.exception("Geyser connection error")
                logger.info("Reconnecting in 10 seconds...")
                await asyncio.sleep(10)

    async def _create_geyser_connection(self):
        """Establish a secure connection to the Geyser endpoint."""
        if self.auth_type == "x-token":
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("x-token", self.geyser_api_token),), None
                )
            )
        else:
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("authorization", f"Basic {self.geyser_api_token}"),), None
                )
            )

        creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
        channel = grpc.aio.secure_channel(self.geyser_endpoint, creds)
        return geyser_pb2_grpc.GeyserStub(channel), channel

    def _create_subscription_request(self) -> geyser_pb2.SubscribeRequest:
        """Create a subscription request for watched traders."""
        request = geyser_pb2.SubscribeRequest()

        filter_index = 0
        for trader in self.trader_addresses:
            for program_id in self.program_ids:
                filter_name = f"copy_trader_{filter_index}"
                filter_index += 1
                request.transactions[filter_name].account_include.append(trader)
                request.transactions[filter_name].account_required.append(program_id)
                request.transactions[filter_name].failed = False

        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
        return request

    def _process_update(self, update) -> list[TradeSignal]:
        """Process Geyser updates and extract trade signals."""
        trade_signals: list[TradeSignal] = []
        try:
            if not update.HasField("transaction"):
                return trade_signals

            tx = update.transaction.transaction.transaction
            msg = getattr(tx, "message", None)
            if msg is None:
                return trade_signals

            account_keys = [bytes(key) for key in msg.account_keys]
            signature = self._extract_signature(update)

            for ix in msg.instructions:
                program_idx = ix.program_id_index
                if program_idx >= len(msg.account_keys):
                    continue

                program_id = Pubkey.from_bytes(msg.account_keys[program_idx])
                parser = self.program_id_to_parser.get(str(program_id))
                if not parser:
                    continue

                parsed = parser.parse_instruction(
                    ix.data, list(ix.accounts), account_keys
                )
                if not parsed:
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

            return trade_signals

        except Exception:
            logger.exception("Error processing Geyser update")
            return trade_signals

    def _extract_signature(self, update) -> str | None:
        """Extract transaction signature from a Geyser update."""
        signature_bytes = None
        if hasattr(update.transaction, "signature"):
            signature_bytes = update.transaction.signature
        elif hasattr(update.transaction.transaction, "signature"):
            signature_bytes = update.transaction.transaction.signature

        if isinstance(signature_bytes, (bytes, bytearray)):
            return base58.b58encode(signature_bytes).decode("utf-8")
        if isinstance(signature_bytes, str):
            return signature_bytes
        return None
