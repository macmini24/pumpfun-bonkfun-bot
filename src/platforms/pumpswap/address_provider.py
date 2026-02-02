"""
PumpSwap (pump.fun AMM) implementation of AddressProvider interface.
"""

from dataclasses import dataclass
from typing import Final

from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import SystemAddresses
from interfaces.core import AddressProvider, Platform, TokenInfo


@dataclass
class PumpSwapAddresses:
    """PumpSwap program addresses."""

    PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
    )
    GLOBAL_CONFIG: Final[Pubkey] = Pubkey.from_string(
        "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
    )
    PROTOCOL_FEE_RECIPIENT: Final[Pubkey] = Pubkey.from_string(
        "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ"
    )
    PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT: Final[Pubkey] = Pubkey.from_string(
        "7GFUN3bWzJMKMRZ34JLsvcqdssDbXnp589SiE33KVwcC"
    )
    EVENT_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
        "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
    )
    FEE_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
    )


class PumpSwapAddressProvider(AddressProvider):
    """PumpSwap implementation of AddressProvider interface."""

    @property
    def platform(self) -> Platform:
        """Get the platform this provider serves."""
        return Platform.PUMP_SWAP

    @property
    def program_id(self) -> Pubkey:
        """Get the main program ID for this platform."""
        return PumpSwapAddresses.PROGRAM

    def get_system_addresses(self) -> dict[str, Pubkey]:
        """Get all system addresses required for PumpSwap."""
        system_addresses = SystemAddresses.get_all_system_addresses()
        pumpswap_addresses = {
            "program": PumpSwapAddresses.PROGRAM,
            "global_config": PumpSwapAddresses.GLOBAL_CONFIG,
            "protocol_fee_recipient": PumpSwapAddresses.PROTOCOL_FEE_RECIPIENT,
            "protocol_fee_recipient_token_account": PumpSwapAddresses.PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT,
            "event_authority": PumpSwapAddresses.EVENT_AUTHORITY,
            "fee_program": PumpSwapAddresses.FEE_PROGRAM,
        }
        return {**system_addresses, **pumpswap_addresses}

    def derive_pool_address(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """PumpSwap pools are not PDA-derivable by mint alone."""
        raise ValueError(
            "PumpSwap pool address must be provided from trade data or pool lookup"
        )

    def derive_user_token_account(self, user: Pubkey, mint: Pubkey) -> Pubkey:
        """Derive user's associated token account address."""
        return get_associated_token_address(user, mint)

    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get PumpSwap-specific additional accounts needed for trading."""
        return token_info.additional_data or {}

    def derive_creator_vault_authority(self, coin_creator: Pubkey) -> Pubkey:
        """Derive the coin creator vault authority PDA."""
        derived_address, _ = Pubkey.find_program_address(
            [b"creator_vault", bytes(coin_creator)],
            PumpSwapAddresses.PROGRAM,
        )
        return derived_address

    def derive_creator_vault_ata(
        self, vault_authority: Pubkey, quote_mint: Pubkey
    ) -> Pubkey:
        """Derive the coin creator vault ATA for the quote mint."""
        return get_associated_token_address(vault_authority, quote_mint)

    def derive_global_volume_accumulator(self) -> Pubkey:
        """Derive the global volume accumulator PDA."""
        derived_address, _ = Pubkey.find_program_address(
            [b"global_volume_accumulator"],
            PumpSwapAddresses.PROGRAM,
        )
        return derived_address

    def derive_user_volume_accumulator(self, user: Pubkey) -> Pubkey:
        """Derive the user volume accumulator PDA."""
        derived_address, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user)],
            PumpSwapAddresses.PROGRAM,
        )
        return derived_address

    def derive_fee_config(self) -> Pubkey:
        """Derive the fee config PDA."""
        derived_address, _ = Pubkey.find_program_address(
            [b"fee_config", bytes(PumpSwapAddresses.PROGRAM)],
            PumpSwapAddresses.FEE_PROGRAM,
        )
        return derived_address

    def _get_additional_pubkey(self, token_info: TokenInfo, key: str) -> Pubkey | None:
        additional = token_info.additional_data or {}
        value = additional.get(key)
        return value if isinstance(value, Pubkey) else None

    def _require_pubkey(self, token_info: TokenInfo, key: str) -> Pubkey:
        value = self._get_additional_pubkey(token_info, key)
        if not value:
            raise ValueError(f"Missing required PumpSwap account '{key}'")
        return value

    def get_buy_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a PumpSwap buy instruction."""
        quote_mint = self._get_additional_pubkey(token_info, "quote_mint") or (
            SystemAddresses.SOL_MINT
        )
        return {
            "pool": self._require_pubkey(token_info, "pool"),
            "user": user,
            "global_config": PumpSwapAddresses.GLOBAL_CONFIG,
            "base_mint": token_info.mint,
            "quote_mint": quote_mint,
            "user_base_token_account": self.derive_user_token_account(
                user, token_info.mint
            ),
            "user_quote_token_account": self.derive_user_token_account(
                user, quote_mint
            ),
            "pool_base_token_account": self._require_pubkey(
                token_info, "pool_base_token_account"
            ),
            "pool_quote_token_account": self._require_pubkey(
                token_info, "pool_quote_token_account"
            ),
            "protocol_fee_recipient": PumpSwapAddresses.PROTOCOL_FEE_RECIPIENT,
            "protocol_fee_recipient_token_account": PumpSwapAddresses.PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT,
            "base_token_program": SystemAddresses.TOKEN_PROGRAM,
            "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "associated_token_program": SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
            "event_authority": PumpSwapAddresses.EVENT_AUTHORITY,
            "program": PumpSwapAddresses.PROGRAM,
            "coin_creator_vault_ata": self._require_pubkey(
                token_info, "coin_creator_vault_ata"
            ),
            "coin_creator_vault_authority": self._require_pubkey(
                token_info, "coin_creator_vault_authority"
            ),
            "global_volume_accumulator": self._get_additional_pubkey(
                token_info, "global_volume_accumulator"
            )
            or self.derive_global_volume_accumulator(),
            "user_volume_accumulator": self._get_additional_pubkey(
                token_info, "user_volume_accumulator"
            )
            or self.derive_user_volume_accumulator(user),
            "fee_config": self._get_additional_pubkey(token_info, "fee_config")
            or self.derive_fee_config(),
            "fee_program": PumpSwapAddresses.FEE_PROGRAM,
        }

    def get_sell_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a PumpSwap sell instruction."""
        quote_mint = self._get_additional_pubkey(token_info, "quote_mint") or (
            SystemAddresses.SOL_MINT
        )
        return {
            "pool": self._require_pubkey(token_info, "pool"),
            "user": user,
            "global_config": PumpSwapAddresses.GLOBAL_CONFIG,
            "base_mint": token_info.mint,
            "quote_mint": quote_mint,
            "user_base_token_account": self.derive_user_token_account(
                user, token_info.mint
            ),
            "user_quote_token_account": self.derive_user_token_account(
                user, quote_mint
            ),
            "pool_base_token_account": self._require_pubkey(
                token_info, "pool_base_token_account"
            ),
            "pool_quote_token_account": self._require_pubkey(
                token_info, "pool_quote_token_account"
            ),
            "protocol_fee_recipient": PumpSwapAddresses.PROTOCOL_FEE_RECIPIENT,
            "protocol_fee_recipient_token_account": PumpSwapAddresses.PROTOCOL_FEE_RECIPIENT_TOKEN_ACCOUNT,
            "base_token_program": SystemAddresses.TOKEN_PROGRAM,
            "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "associated_token_program": SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
            "event_authority": PumpSwapAddresses.EVENT_AUTHORITY,
            "program": PumpSwapAddresses.PROGRAM,
            "coin_creator_vault_ata": self._require_pubkey(
                token_info, "coin_creator_vault_ata"
            ),
            "coin_creator_vault_authority": self._require_pubkey(
                token_info, "coin_creator_vault_authority"
            ),
            "fee_config": self._get_additional_pubkey(token_info, "fee_config")
            or self.derive_fee_config(),
            "fee_program": PumpSwapAddresses.FEE_PROGRAM,
        }
