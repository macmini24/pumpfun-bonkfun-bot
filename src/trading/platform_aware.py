"""
Platform-aware trader implementations that use the interface system.
Final cleanup removing all platform-specific hardcoding.
"""

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import LAMPORTS_PER_SOL, TOKEN_DECIMALS
from core.wallet import Wallet
from interfaces.core import AddressProvider, Platform, TokenInfo
from platforms import get_platform_implementations
from trading.base import Trader, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)


class PlatformAwareBuyer(Trader):
    """Platform-aware token buyer that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        amount: float,
        slippage: float = 0.01,
        max_retries: int = 5,
        extreme_fast_token_amount: int = 0,
        extreme_fast_mode: bool = False,
        fast_buy: bool = False,
        fast_buy_min_amount_out: int = 1,
    ):
        """Initialize platform-aware token buyer.

        Args:
            client: Solana RPC client
            wallet: Wallet instance
            priority_fee_manager: Priority fee manager
            amount: Amount of SOL to spend
            slippage: Allowed slippage percentage
            max_retries: Maximum retries for transaction submission
            extreme_fast_token_amount: Token amount to assume in extreme fast mode
            extreme_fast_mode: Skip pool stabilization + price RPC
            fast_buy: Skip price RPC and use minimal token out
            fast_buy_min_amount_out: Minimum tokens out in raw units
        """
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        self.fast_buy = fast_buy
        self.fast_buy_min_amount_out = max(1, fast_buy_min_amount_out)

    async def execute(self, token_info: TokenInfo) -> TradeResult:
        """Execute buy operation using platform-specific implementations."""
        try:
            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager

            # Convert amount to lamports
            amount_lamports = int(self.amount * LAMPORTS_PER_SOL)

            if self.extreme_fast_mode:
                # Skip the wait and directly calculate the amount
                token_amount = self.extreme_fast_token_amount
                token_price_sol = self.amount / token_amount if token_amount > 0 else 0
            elif self.fast_buy:
                token_amount = None
                token_price_sol = None
            else:
                # Get pool address based on platform using platform-agnostic method
                pool_address = self._get_pool_address(token_info, address_provider)

                # Regular behavior with RPC call
                token_price_sol = await curve_manager.calculate_price(pool_address)
                token_amount = (
                    self.amount / token_price_sol if token_price_sol > 0 else 0
                )

            # Calculate minimum token amount with slippage
            if self.extreme_fast_mode:
                minimum_token_amount = token_amount * (1 - self.slippage)
                minimum_token_amount_raw = int(
                    minimum_token_amount * 10**TOKEN_DECIMALS
                )
            elif self.fast_buy:
                minimum_token_amount_raw = self.fast_buy_min_amount_out
            else:
                minimum_token_amount = token_amount * (1 - self.slippage)
                minimum_token_amount_raw = int(
                    minimum_token_amount * 10**TOKEN_DECIMALS
                )

            # Calculate maximum SOL to spend with slippage
            max_amount_lamports = int(amount_lamports * (1 + self.slippage))

            # Build buy instructions using platform-specific builder
            instructions = await instruction_builder.build_buy_instruction(
                token_info,
                self.wallet.pubkey,
                max_amount_lamports,  # amount_in (SOL)
                minimum_token_amount_raw,  # minimum_amount_out (tokens)
                address_provider,
            )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_buy(
                token_info, self.wallet.pubkey, address_provider
            )

            if self.fast_buy:
                logger.info(
                    "Fast buy enabled: skipping price RPC, "
                    f"min out {minimum_token_amount_raw} (raw units)"
                )
            else:
                logger.info(
                    f"Buying {token_amount:.6f} tokens at {token_price_sol:.8f} SOL per "
                    f"token on {token_info.platform.value}"
                )
            logger.info(
                f"Total cost: {self.amount:.6f} SOL (max: {max_amount_lamports / LAMPORTS_PER_SOL:.6f} SOL)"
            )

            # Send transaction
            tx_signature = await self.client.build_and_send_transaction(
                instructions,
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                    priority_accounts
                ),
            )

            success = await self.client.confirm_transaction(tx_signature)

            if success:
                logger.info(f"Buy transaction confirmed: {tx_signature}")
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_amount,
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.exception("Buy operation failed")
            return TradeResult(
                success=False, platform=token_info.platform, error_message=str(e)
            )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)


class PlatformAwareSeller(Trader):
    """Platform-aware token seller that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        slippage: float = 0.25,
        max_retries: int = 5,
        fast_sell: bool = False,
        fast_sell_min_amount_out: int = 1,
    ):
        """Initialize platform-aware token seller."""
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.slippage = slippage
        self.max_retries = max_retries
        self.fast_sell = fast_sell
        self.fast_sell_min_amount_out = max(1, fast_sell_min_amount_out)

    async def execute(self, token_info: TokenInfo) -> TradeResult:
        """Execute sell operation using platform-specific implementations."""
        try:
            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager

            # Get user's token account and balance
            user_token_account = address_provider.derive_user_token_account(
                self.wallet.pubkey, token_info.mint
            )

            token_balance = await self.client.get_token_account_balance(
                user_token_account
            )
            token_balance_decimal = token_balance / 10**TOKEN_DECIMALS

            logger.info(f"Token balance: {token_balance_decimal}")

            if token_balance == 0:
                logger.info("No tokens to sell.")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message="No tokens to sell",
                )

            if self.fast_sell:
                token_price_sol = None
                min_sol_output = self.fast_sell_min_amount_out
                logger.info(
                    "Fast sell enabled: skipping price RPC, "
                    f"min out {min_sol_output} lamports"
                )
            else:
                # Get pool address and current price using platform-agnostic method
                pool_address = self._get_pool_address(token_info, address_provider)
                token_price_sol = await curve_manager.calculate_price(pool_address)

                logger.info(f"Price per Token: {token_price_sol:.8f} SOL")

                # Calculate minimum SOL output with slippage
                expected_sol_output = float(token_balance_decimal) * float(
                    token_price_sol
                )
                min_sol_output = int(
                    (expected_sol_output * (1 - self.slippage)) * LAMPORTS_PER_SOL
                )

                logger.info(
                    f"Selling {token_balance_decimal} tokens on {token_info.platform.value}"
                )
                logger.info(f"Expected SOL output: {expected_sol_output:.8f} SOL")
                logger.info(
                    "Minimum SOL output (with "
                    f"{self.slippage * 100}% slippage): "
                    f"{min_sol_output / LAMPORTS_PER_SOL:.8f} SOL"
                )

            # Build sell instructions using platform-specific builder
            instructions = await instruction_builder.build_sell_instruction(
                token_info,
                self.wallet.pubkey,
                token_balance,  # amount_in (tokens)
                min_sol_output,  # minimum_amount_out (SOL)
                address_provider,
            )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_sell(
                token_info, self.wallet.pubkey, address_provider
            )

            # Send transaction
            tx_signature = await self.client.build_and_send_transaction(
                instructions,
                self.wallet.keypair,
                skip_preflight=True,
                max_retries=self.max_retries,
                priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                    priority_accounts
                ),
            )

            success = await self.client.confirm_transaction(tx_signature)

            if success:
                logger.info(f"Sell transaction confirmed: {tx_signature}")
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_balance_decimal,
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.exception("Sell operation failed")
            return TradeResult(
                success=False, platform=token_info.platform, error_message=str(e)
            )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)
