"""
PumpSwap curve manager implementation for price calculations.
"""

import base64
from typing import Any

from solders.pubkey import Pubkey

from core.client import SolanaClient
from interfaces.core import CurveManager, Platform
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class PumpSwapCurveManager(CurveManager):
    """PumpSwap curve manager for AMM pool state and pricing."""

    def __init__(self, client: SolanaClient, idl_parser: IDLParser):
        self.client = client
        self.idl_parser = idl_parser

    @property
    def platform(self) -> Platform:
        return Platform.PUMP_SWAP

    async def get_pool_state(self, pool_address: Pubkey) -> dict[str, Any]:
        account_info = await self.client.get_account_info(pool_address)
        data = account_info.data

        if isinstance(data, list) and data:
            raw = base64.b64decode(data[0])
        elif isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
        else:
            raise ValueError("Unsupported pool account data format")

        decoded = self.idl_parser.decode_account_data(raw, "Pool")
        if not decoded:
            raise ValueError("Failed to decode PumpSwap pool state")
        return decoded

    async def calculate_price(self, pool_address: Pubkey) -> float:
        pool_state = await self.get_pool_state(pool_address)
        base_token_account = Pubkey.from_string(pool_state["pool_base_token_account"])
        quote_token_account = Pubkey.from_string(pool_state["pool_quote_token_account"])

        base_amount = await self._get_token_balance_ui(base_token_account)
        quote_amount = await self._get_token_balance_ui(quote_token_account)

        if base_amount <= 0:
            return 0.0
        return quote_amount / base_amount

    async def calculate_buy_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        price = await self.calculate_price(pool_address)
        if price <= 0:
            return 0
        return int(amount_in / price)

    async def calculate_sell_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        price = await self.calculate_price(pool_address)
        if price <= 0:
            return 0
        return int(amount_in * price)

    async def get_reserves(self, pool_address: Pubkey) -> tuple[int, int]:
        pool_state = await self.get_pool_state(pool_address)
        base_token_account = Pubkey.from_string(pool_state["pool_base_token_account"])
        quote_token_account = Pubkey.from_string(pool_state["pool_quote_token_account"])

        base_raw = await self._get_token_balance_raw(base_token_account)
        quote_raw = await self._get_token_balance_raw(quote_token_account)
        return base_raw, quote_raw

    async def _get_token_balance_raw(self, token_account: Pubkey) -> int:
        client = await self.client.get_client()
        response = await client.get_token_account_balance(token_account)
        if response.value:
            return int(response.value.amount)
        return 0

    async def _get_token_balance_ui(self, token_account: Pubkey) -> float:
        client = await self.client.get_client()
        response = await client.get_token_account_balance(token_account)
        if response.value:
            amount = int(response.value.amount)
            decimals = int(response.value.decimals)
            if decimals == 0:
                return float(amount)
            return amount / (10**decimals)
        return 0.0
