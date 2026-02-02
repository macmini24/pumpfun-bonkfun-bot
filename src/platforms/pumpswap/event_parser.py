"""
PumpSwap implementation of EventParser interface.

PumpSwap is an AMM and does not emit token creation events used by snipers.
This parser is primarily used to expose program metadata and satisfy interfaces.
"""

from typing import Any

from solders.pubkey import Pubkey

from interfaces.core import EventParser, Platform, TokenInfo
from utils.idl_parser import IDLParser


class PumpSwapEventParser(EventParser):
    """PumpSwap event parser (trade-only, no token creation parsing)."""

    def __init__(self, idl_parser: IDLParser):
        self._idl_parser = idl_parser
        discriminators = self._idl_parser.get_instruction_discriminators()
        self._buy_discriminator = discriminators.get("buy", b"")
        self._sell_discriminator = discriminators.get("sell", b"")

    @property
    def platform(self) -> Platform:
        return Platform.PUMP_SWAP

    def parse_token_creation_from_logs(
        self, logs: list[str], signature: str
    ) -> TokenInfo | None:
        return None

    def parse_token_creation_from_instruction(
        self, instruction_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> TokenInfo | None:
        return None

    def parse_token_creation_from_geyser(
        self, transaction_info: Any
    ) -> TokenInfo | None:
        return None

    def parse_token_creation_from_block(self, block_data: dict[str, Any]) -> TokenInfo | None:
        return None

    def get_program_id(self) -> Pubkey:
        from platforms.pumpswap.address_provider import PumpSwapAddresses

        return PumpSwapAddresses.PROGRAM

    def get_instruction_discriminators(self) -> list[bytes]:
        return [self._buy_discriminator, self._sell_discriminator]
