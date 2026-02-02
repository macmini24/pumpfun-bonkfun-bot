"""
Instruction-level trade parsing utilities for copy trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey

from interfaces.core import Platform, TokenInfo
from monitoring.trade_listener import TradeSide
from platforms.letsbonk.address_provider import LetsBonkAddressProvider
from platforms.pumpfun.address_provider import PumpFunAddressProvider
from utils.idl_manager import get_idl_parser
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ParsedTradeAction:
    """Parsed trade action from a single instruction."""

    side: TradeSide
    trader: str
    token_info: TokenInfo
    platform: Platform


@dataclass(frozen=True)
class TradeInstructionConfig:
    """Configuration for decoding trade instructions per platform."""

    buy_instruction: str
    sell_instruction: str
    trader_account: str
    mint_account: str


PLATFORM_TRADE_CONFIGS: dict[Platform, TradeInstructionConfig] = {
    Platform.PUMP_FUN: TradeInstructionConfig(
        buy_instruction="buy",
        sell_instruction="sell",
        trader_account="user",
        mint_account="mint",
    ),
    Platform.LETS_BONK: TradeInstructionConfig(
        buy_instruction="buy_exact_in",
        sell_instruction="sell_exact_in",
        trader_account="payer",
        mint_account="base_token_mint",
    ),
    Platform.PUMP_SWAP: TradeInstructionConfig(
        buy_instruction="buy",
        sell_instruction="sell",
        trader_account="user",
        mint_account="base_mint",
    ),
}


class TradeInstructionParser:
    """Decode platform trade instructions into structured trade actions."""

    def __init__(self, platform: Platform) -> None:
        """Initialize the parser for a specific platform.

        Args:
            platform: Platform to parse trades for
        """
        if platform not in PLATFORM_TRADE_CONFIGS:
            raise ValueError(f"Unsupported platform for trade parsing: {platform}")

        self.platform = platform
        self.config = PLATFORM_TRADE_CONFIGS[platform]
        self.idl_parser = get_idl_parser(platform)
        self.program_id = self._resolve_program_id(platform)

    def parse_instruction(
        self, ix_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> ParsedTradeAction | None:
        """Parse a single instruction into a trade action if applicable.

        Args:
            ix_data: Raw instruction data bytes
            accounts: Account indices referenced by the instruction
            account_keys: Transaction account keys as bytes

        Returns:
            ParsedTradeAction if trade instruction found, otherwise None
        """
        decoded = self.idl_parser.decode_instruction(ix_data, account_keys, accounts)
        if not decoded:
            return None

        instruction_name = decoded.get("instruction_name")
        if instruction_name not in (
            self.config.buy_instruction,
            self.config.sell_instruction,
        ):
            return None

        side = (
            TradeSide.BUY
            if instruction_name == self.config.buy_instruction
            else TradeSide.SELL
        )
        accounts_map = decoded.get("accounts", {})
        trader = accounts_map.get(self.config.trader_account)
        if not trader:
            return None

        token_info = self._build_token_info(accounts_map, trader)
        if not token_info:
            return None

        return ParsedTradeAction(
            side=side,
            trader=trader,
            token_info=token_info,
            platform=self.platform,
        )

    def _build_token_info(
        self, accounts_map: dict[str, Any], trader: str
    ) -> TokenInfo | None:
        """Build TokenInfo from decoded instruction accounts.

        Args:
            accounts_map: Decoded instruction account mapping
            trader: Trader address from the instruction

        Returns:
            TokenInfo if required fields are present, otherwise None
        """
        mint_str = accounts_map.get(self.config.mint_account) or accounts_map.get(
            "mint"
        )
        mint = self._to_pubkey(mint_str)
        trader_key = self._to_pubkey(trader)

        if not mint or not trader_key:
            return None

        label = self._format_label(mint_str)

        if self.platform == Platform.PUMP_FUN:
            bonding_curve = self._to_pubkey(accounts_map.get("bonding_curve"))
            associated_bonding_curve = self._to_pubkey(
                accounts_map.get("associated_bonding_curve")
            )
            creator_vault = self._to_pubkey(accounts_map.get("creator_vault"))

            if not bonding_curve:
                bonding_curve = PumpFunAddressProvider().derive_pool_address(mint)

            return TokenInfo(
                name=label,
                symbol=label,
                uri="",
                mint=mint,
                platform=self.platform,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                user=trader_key,
                creator=trader_key,
                creator_vault=creator_vault,
            )

        if self.platform == Platform.LETS_BONK:
            pool_state = self._to_pubkey(accounts_map.get("pool_state"))
            base_vault = self._to_pubkey(accounts_map.get("base_vault"))
            quote_vault = self._to_pubkey(accounts_map.get("quote_vault"))

            if not pool_state:
                pool_state = LetsBonkAddressProvider().derive_pool_address(mint)

            return TokenInfo(
                name=label,
                symbol=label,
                uri="",
                mint=mint,
                platform=self.platform,
                pool_state=pool_state,
                base_vault=base_vault,
                quote_vault=quote_vault,
                user=trader_key,
                creator=trader_key,
            )

        if self.platform == Platform.PUMP_SWAP:
            pool = self._to_pubkey(accounts_map.get("pool"))
            base_mint = self._to_pubkey(accounts_map.get("base_mint"))
            quote_mint = self._to_pubkey(accounts_map.get("quote_mint"))
            pool_base = self._to_pubkey(accounts_map.get("pool_base_token_account"))
            pool_quote = self._to_pubkey(accounts_map.get("pool_quote_token_account"))
            user_base = self._to_pubkey(accounts_map.get("user_base_token_account"))
            user_quote = self._to_pubkey(accounts_map.get("user_quote_token_account"))
            protocol_fee_recipient = self._to_pubkey(
                accounts_map.get("protocol_fee_recipient")
            )
            protocol_fee_recipient_token_account = self._to_pubkey(
                accounts_map.get("protocol_fee_recipient_token_account")
            )
            event_authority = self._to_pubkey(accounts_map.get("event_authority"))
            global_config = self._to_pubkey(accounts_map.get("global_config"))
            coin_creator_vault_ata = self._to_pubkey(
                accounts_map.get("coin_creator_vault_ata")
            )
            coin_creator_vault_authority = self._to_pubkey(
                accounts_map.get("coin_creator_vault_authority")
            )
            global_volume_accumulator = self._to_pubkey(
                accounts_map.get("global_volume_accumulator")
            )
            user_volume_accumulator = self._to_pubkey(
                accounts_map.get("user_volume_accumulator")
            )
            fee_config = self._to_pubkey(accounts_map.get("fee_config"))
            fee_program = self._to_pubkey(accounts_map.get("fee_program"))

            additional_data = {
                "pool": pool,
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "pool_base_token_account": pool_base,
                "pool_quote_token_account": pool_quote,
                "user_base_token_account": user_base,
                "user_quote_token_account": user_quote,
                "protocol_fee_recipient": protocol_fee_recipient,
                "protocol_fee_recipient_token_account": protocol_fee_recipient_token_account,
                "event_authority": event_authority,
                "global_config": global_config,
                "coin_creator_vault_ata": coin_creator_vault_ata,
                "coin_creator_vault_authority": coin_creator_vault_authority,
                "global_volume_accumulator": global_volume_accumulator,
                "user_volume_accumulator": user_volume_accumulator,
                "fee_config": fee_config,
                "fee_program": fee_program,
            }
            additional_data = {
                key: value for key, value in additional_data.items() if value is not None
            }

            return TokenInfo(
                name=label,
                symbol=label,
                uri="",
                mint=base_mint or mint,
                platform=self.platform,
                pool_state=pool,
                base_vault=pool_base,
                quote_vault=pool_quote,
                user=trader_key,
                creator=trader_key,
                additional_data=additional_data,
            )

        return None

    def _resolve_program_id(self, platform: Platform) -> Pubkey:
        """Resolve the program ID for a platform.

        Args:
            platform: Platform to resolve program ID for

        Returns:
            Program ID as Pubkey
        """
        if platform == Platform.PUMP_FUN:
            return PumpFunAddressProvider().program_id
        if platform == Platform.LETS_BONK:
            return LetsBonkAddressProvider().program_id
        if platform == Platform.PUMP_SWAP:
            from platforms.pumpswap.address_provider import PumpSwapAddressProvider

            return PumpSwapAddressProvider().program_id
        raise ValueError(f"Unsupported platform for program ID lookup: {platform}")

    def _to_pubkey(self, value: Any) -> Pubkey | None:
        """Convert a value to Pubkey if possible.

        Args:
            value: Base58 string or Pubkey-like object

        Returns:
            Pubkey instance or None if conversion fails
        """
        if value is None:
            return None
        if isinstance(value, Pubkey):
            return value
        if isinstance(value, str):
            try:
                return Pubkey.from_string(value)
            except Exception:
                logger.debug("Failed to parse pubkey from string")
                return None
        return None

    def _format_label(self, mint_str: str | None) -> str:
        """Build a short label from a mint string.

        Args:
            mint_str: Mint address string

        Returns:
            Short label for logging
        """
        if not mint_str:
            return "UNKNOWN"
        if len(mint_str) <= 10:
            return mint_str
        return f"{mint_str[:4]}...{mint_str[-4:]}"
