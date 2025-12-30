#!/usr/bin/env python3
"""
Robust ETH transfer utility using Web3.py.

Improvements:
- Stricter input validation
- Clear separation of concerns
- Safer gas cost calculation
- Better EIP-1559 handling
- Type hints and docstrings everywhere
- Reduced hidden side effects
"""

import os
import sys
import time
import logging
from typing import Optional, Dict, Any, Callable

from dotenv import load_dotenv
from web3 import Web3, exceptions
from web3.middleware import geth_poa_middleware


# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("EtherTransfer")


# =========================
# Constants
# =========================
DEFAULT_AMOUNT_ETH = 0.01
DEFAULT_GAS_LIMIT = 21_000
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 2
DEFAULT_PRIORITY_FEE_GWEI = 2


# =========================
# Helpers
# =========================
def env_required(name: str) -> str:
    """Read required environment variable or exit."""
    value = os.getenv(name)
    if not value:
        logger.critical("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def parse_positive_float(value: Optional[str], default: float) -> float:
    """Parse a positive float or return default."""
    if not value:
        return default
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except ValueError:
        return default


def retry(
    func: Callable[..., Any],
    label: str,
    *args,
    **kwargs,
) -> Any:
    """Generic retry wrapper with logging."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "%s failed (%d/%d): %s",
                label,
                attempt,
                RETRY_ATTEMPTS,
                exc,
            )
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError(f"{label} failed after {RETRY_ATTEMPTS} attempts")


# =========================
# Main Class
# =========================
class EtherTransfer:
    """Utility class for sending ETH transactions."""

    def __init__(self) -> None:
        load_dotenv()

        # Required config
        self.infura_url: str = env_required("INFURA_URL")
        self.from_address: str = Web3.to_checksum_address(env_required("FROM_ADDRESS"))
        self.to_address: str = Web3.to_checksum_address(env_required("TO_ADDRESS"))
        self.private_key: str = env_required("PRIVATE_KEY")

        # Optional config
        self.transfer_amount_eth: float = parse_positive_float(
            os.getenv("TRANSFER_AMOUNT"),
            DEFAULT_AMOUNT_ETH,
        )
        self.custom_gas_price_wei: Optional[int] = self._parse_custom_gas_price(
            os.getenv("DEFAULT_GAS_PRICE")
        )

        # Web3 setup
        self.web3 = Web3(Web3.HTTPProvider(self.infura_url, request_kwargs={"timeout": 30}))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not self.web3.is_connected():
            logger.critical("Failed to connect to Ethereum node.")
            sys.exit(1)

        self._validate_inputs()

    # ---------------------------------------------------------
    def _parse_custom_gas_price(self, value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            gas_price = int(value)
            return gas_price if gas_price > 0 else None
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE value, ignoring.")
            return None

    # ---------------------------------------------------------
    def _validate_inputs(self) -> None:
        if not self.private_key:
            raise ValueError("PRIVATE_KEY must not be empty")
        if self.transfer_amount_eth <= 0:
            raise ValueError("TRANSFER_AMOUNT must be positive")

        logger.debug("Input validation passed.")

    # ---------------------------------------------------------
    # Gas logic
    # ---------------------------------------------------------
    def get_gas_params(self) -> Dict[str, int]:
        """Return gas parameters (EIP-1559 preferred, legacy fallback)."""

        # Custom legacy gas price
        if self.custom_gas_price_wei:
            logger.info("Using custom gas price: %d wei", self.custom_gas_price_wei)
            return {"gasPrice": self.custom_gas_price_wei}

        # Try EIP-1559
        try:
            latest_block = self.web3.eth.get_block("latest")
            base_fee = latest_block.get("baseFeePerGas")
            if base_fee is not None:
                priority_fee = self.web3.to_wei(DEFAULT_PRIORITY_FEE_GWEI, "gwei")
                return {
                    "maxFeePerGas": base_fee + priority_fee,
                    "maxPriorityFeePerGas": priority_fee,
                }
        except Exception as exc:
            logger.debug("EIP-1559 gas fetch failed: %s", exc)

        # Legacy fallback
        gas_price = retry(self.web3.eth.gas_price, "Fetch gas price")
        return {"gasPrice": gas_price}

    # ---------------------------------------------------------
    # Blockchain helpers
    # ---------------------------------------------------------
    def get_nonce(self) -> int:
        return retry(
            self.web3.eth.get_transaction_count,
            "Get nonce",
            self.from_address,
            "pending",
        )

    def estimate_gas(self, value_wei: int) -> int:
        try:
            return self.web3.eth.estimate_gas(
                {
                    "from": self.from_address,
                    "to": self.to_address,
                    "value": value_wei,
                }
            )
        except Exception as exc:
            logger.warning(
                "Gas estimation failed (%s), using fallback %d",
                exc,
                DEFAULT_GAS_LIMIT,
            )
            return DEFAULT_GAS_LIMIT

    # ---------------------------------------------------------
    def build_transaction(
        self,
        value_wei: int,
        gas_params: Dict[str, int],
    ) -> Dict[str, Any]:
        return {
            "chainId": self.web3.eth.chain_id,
            "nonce": self.get_nonce(),
            "to": self.to_address,
            "value": value_wei,
            "gas": self.estimate_gas(value_wei),
            **gas_params,
        }

    # ---------------------------------------------------------
    def _estimate_total_cost(
        self,
        tx: Dict[str, Any],
    ) -> int:
        gas_limit = tx["gas"]
        gas_price = (
            tx.get("maxFeePerGas")
            or tx.get("gasPrice")
            or 0
        )
        return gas_limit * gas_price + tx["value"]

    # ---------------------------------------------------------
    def send_eth(self) -> str:
        value_wei = self.web3.to_wei(self.transfer_amount_eth, "ether")

        gas_params = self.get_gas_params()
        tx = self.build_transaction(value_wei, gas_params)

        balance = self.web3.eth.get_balance(self.from_address)
        required = self._estimate_total_cost(tx)

        if balance < required:
            raise ValueError(
                f"Insufficient balance. Required: {required}, balance: {balance}"
            )

        try:
            signed_tx = self.web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
            hex_hash = self.web3.to_hex(tx_hash)
            logger.info("Transaction sent: %s", hex_hash)
            return hex_hash

        except exceptions.InsufficientFunds:
            raise ValueError("Insufficient funds to cover gas.")
        except Exception:
            logger.exception("Transaction submission failed.")
            raise

    # ---------------------------------------------------------
    def run(self) -> None:
        logger.info("Sending %.6f ETH from %s to %s",
                    self.transfer_amount_eth,
                    self.from_address,
                    self.to_address)

        try:
            tx_hash = self.send_eth()
            logger.info("Transfer successful: %s", tx_hash)
        except Exception as exc:
            logger.critical("Execution failed: %s", exc)
            sys.exit(1)


if __name__ == "__main__":
    EtherTransfer().run()
