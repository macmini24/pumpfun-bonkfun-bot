"""
PumpSwap implementation of InstructionBuilder interface.
"""

import struct

from solders.compute_budget import set_compute_unit_limit
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from spl.token.instructions import (
    SyncNativeParams,
    create_idempotent_associated_token_account,
    sync_native,
)

from core.pubkeys import SystemAddresses
from interfaces.core import AddressProvider, InstructionBuilder, Platform, TokenInfo
from utils.idl_parser import IDLParser


class PumpSwapInstructionBuilder(InstructionBuilder):
    """PumpSwap implementation of InstructionBuilder interface."""

    BUY_COMPUTE_LIMIT = 200_000
    SELL_COMPUTE_LIMIT = 100_000

    def __init__(self, idl_parser: IDLParser):
        """Initialize PumpSwap instruction builder with injected IDL parser."""
        self._idl_parser = idl_parser
        discriminators = self._idl_parser.get_instruction_discriminators()
        self._buy_discriminator = discriminators["buy"]
        self._sell_discriminator = discriminators["sell"]

    @property
    def platform(self) -> Platform:
        """Get the platform this builder serves."""
        return Platform.PUMP_SWAP

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build buy instruction(s) for PumpSwap."""
        instructions: list[Instruction] = []
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)

        instructions.append(set_compute_unit_limit(self.BUY_COMPUTE_LIMIT))

        base_ata_ix = create_idempotent_associated_token_account(
            user,
            user,
            accounts_info["base_mint"],
            SystemAddresses.TOKEN_PROGRAM,
        )
        instructions.append(base_ata_ix)

        quote_mint = accounts_info["quote_mint"]
        quote_ata_ix = create_idempotent_associated_token_account(
            user,
            user,
            quote_mint,
            SystemAddresses.TOKEN_PROGRAM,
        )
        instructions.append(quote_ata_ix)

        if quote_mint == SystemAddresses.SOL_MINT:
            transfer_ix = transfer(
                TransferParams(
                    from_pubkey=user,
                    to_pubkey=accounts_info["user_quote_token_account"],
                    lamports=amount_in,
                )
            )
            instructions.append(transfer_ix)
            sync_ix = sync_native(
                SyncNativeParams(
                    SystemAddresses.TOKEN_PROGRAM,
                    accounts_info["user_quote_token_account"],
                )
            )
            instructions.append(sync_ix)

        buy_accounts = [
            AccountMeta(pubkey=accounts_info["pool"], is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts_info["user"], is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=accounts_info["global_config"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["base_mint"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["quote_mint"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["user_base_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["user_quote_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["pool_base_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["pool_quote_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["protocol_fee_recipient"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["protocol_fee_recipient_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["base_token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["quote_token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["system_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["associated_token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["program"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["coin_creator_vault_ata"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["coin_creator_vault_authority"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["global_volume_accumulator"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["user_volume_accumulator"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["fee_config"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["fee_program"],
                is_signer=False,
                is_writable=False,
            ),
        ]

        track_volume = struct.pack("<?", True)
        instruction_data = (
            self._buy_discriminator
            + struct.pack("<Q", minimum_amount_out)
            + struct.pack("<Q", amount_in)
            + track_volume
        )

        buy_instruction = Instruction(
            program_id=accounts_info["program"],
            data=instruction_data,
            accounts=buy_accounts,
        )
        instructions.append(buy_instruction)

        return instructions

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build sell instruction(s) for PumpSwap."""
        instructions: list[Instruction] = []
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)

        instructions.append(set_compute_unit_limit(self.SELL_COMPUTE_LIMIT))

        quote_mint = accounts_info["quote_mint"]
        quote_ata_ix = create_idempotent_associated_token_account(
            user,
            user,
            quote_mint,
            SystemAddresses.TOKEN_PROGRAM,
        )
        instructions.append(quote_ata_ix)

        sell_accounts = [
            AccountMeta(pubkey=accounts_info["pool"], is_signer=False, is_writable=False),
            AccountMeta(pubkey=accounts_info["user"], is_signer=True, is_writable=True),
            AccountMeta(
                pubkey=accounts_info["global_config"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["base_mint"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["quote_mint"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["user_base_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["user_quote_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["pool_base_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["pool_quote_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["protocol_fee_recipient"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["protocol_fee_recipient_token_account"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["base_token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["quote_token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["system_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["associated_token_program"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["program"], is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_info["coin_creator_vault_ata"],
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=accounts_info["coin_creator_vault_authority"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["fee_config"],
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=accounts_info["fee_program"],
                is_signer=False,
                is_writable=False,
            ),
        ]

        instruction_data = (
            self._sell_discriminator
            + struct.pack("<Q", amount_in)
            + struct.pack("<Q", minimum_amount_out)
        )

        sell_instruction = Instruction(
            program_id=accounts_info["program"],
            data=instruction_data,
            accounts=sell_accounts,
        )
        instructions.append(sell_instruction)

        return instructions

    def get_required_accounts_for_buy(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)
        return [
            accounts_info["pool"],
            accounts_info["user_base_token_account"],
            accounts_info["user_quote_token_account"],
            accounts_info["pool_base_token_account"],
            accounts_info["pool_quote_token_account"],
            accounts_info["protocol_fee_recipient_token_account"],
            accounts_info["coin_creator_vault_ata"],
            accounts_info["global_volume_accumulator"],
            accounts_info["user_volume_accumulator"],
            accounts_info["fee_config"],
            accounts_info["fee_program"],
        ]

    def get_required_accounts_for_sell(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)
        return [
            accounts_info["pool"],
            accounts_info["user_base_token_account"],
            accounts_info["user_quote_token_account"],
            accounts_info["pool_base_token_account"],
            accounts_info["pool_quote_token_account"],
            accounts_info["protocol_fee_recipient_token_account"],
            accounts_info["coin_creator_vault_ata"],
            accounts_info["fee_config"],
            accounts_info["fee_program"],
        ]
