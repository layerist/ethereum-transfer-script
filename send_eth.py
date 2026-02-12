#!/usr/bin/env python3
"""
Robust ETH transfer utility using Web3.py.

Features:
- Strict environment validation
- EIP-1559 first, legacy fallback
- Deterministic fee bounds
- Safe balance pre-check
- Deterministic retry logic
- Explicit error boundaries
"""

from __future__ import annotations

import os
import sys
import time
import logging
from typing import Any, Callable, Dict, Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import geth_poa_middleware
from web3.exceptions import InsufficientFunds, TransactionNotFound


# ==================================================
# Logging
# ==================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("EtherTransfer")


# ==================================================
# Constants
# ==================================================
DEFAULT_AMOUNT_ETH = 0.01
DEFAULT_GAS_LIMIT = 21_000

DEFAULT_PRIORITY_FEE_GWEI = 2
EIP1559_BASEFEE_MULTIPLIER = 2  # conservative maxFee bound

RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 2

RECEIPT_TIMEOUT_SEC = 120


# ==================================================
# Utilities
# ==================================================
def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.critical("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def parse_positive_float(value: Optional[str], default: float) -> float:
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
    *args: Any,
    **kwargs: Any,
) -> Any:
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


# ==================================================
# Main class
# ==================================================
class EtherTransfer:
    """Utility class for sending ETH safely."""

    def __init__(self) -> None:
        load_dotenv()

        self.infura_url = env_required("INFURA_URL")
        self.from_address = Web3.to_checksum_address(env_required("FROM_ADDRESS"))
        self.to_address = Web3.to_checksum_address(env_required("TO_ADDRESS"))
        self.private_key = env_required("PRIVATE_KEY")

        self.amount_eth = parse_positive_float(
            os.getenv("TRANSFER_AMOUNT"),
            DEFAULT_AMOUNT_ETH,
        )

        self.custom_gas_price_wei = self._parse_custom_gas_price(
            os.getenv("DEFAULT_GAS_PRICE")
        )

        self.web3 = self._init_web3()
        self._validate_inputs()

    # --------------------------------------------------
    def _init_web3(self) -> Web3:
        web3 = Web3(
            Web3.HTTPProvider(
                self.infura_url,
                request_kwargs={"timeout": 30},
            )
        )
        web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not web3.is_connected():
            logger.critical("Failed to connect to Ethereum node.")
            sys.exit(1)

        return web3

    # --------------------------------------------------
    @staticmethod
    def _parse_custom_gas_price(value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            gas_price = int(value)
            return gas_price if gas_price > 0 else None
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE, ignoring.")
            return None

    # --------------------------------------------------
    def _validate_inputs(self) -> None:
        if self.amount_eth <= 0:
            raise ValueError("TRANSFER_AMOUNT must be positive")

    # ==================================================
    # Gas handling
    # ==================================================
    def get_gas_params(self) -> Dict[str, int]:
        """
        Returns gas parameters.
        Prefers EIP-1559, falls back to legacy gasPrice.
        """

        if self.custom_gas_price_wei:
            logger.info(
                "Using custom legacy gas price: %d wei",
                self.custom_gas_price_wei,
            )
            return {"gasPrice": self.custom_gas_price_wei}

        try:
            block = self.web3.eth.get_block("latest")
            base_fee = block.get("baseFeePerGas")

            if base_fee is not None:
                priority_fee = self.web3.to_wei(
                    DEFAULT_PRIORITY_FEE_GWEI,
                    "gwei",
                )
                max_fee = base_fee * EIP1559_BASEFEE_MULTIPLIER + priority_fee

                return {
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee,
                }

        except Exception as exc:
            logger.debug("EIP-1559 fee calculation failed: %s", exc)

        gas_price = retry(self.web3.eth.gas_price, "Fetch legacy gas price")
        return {"gasPrice": gas_price}

    # ==================================================
    # Transaction helpers
    # ==================================================
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

    # --------------------------------------------------
    @staticmethod
    def estimate_max_cost(tx: Dict[str, Any]) -> int:
        """
        Upper-bound cost estimation.
        Uses maxFeePerGas when available.
        """
        gas_limit = tx["gas"]
        gas_price = (
            tx.get("maxFeePerGas")
            or tx.get("gasPrice")
            or 0
        )
        return gas_limit * gas_price + tx["value"]

    # ==================================================
    # Execution
    # ==================================================
    def send(self) -> str:
        value_wei = self.web3.to_wei(self.amount_eth, "ether")

        gas_params = self.get_gas_params()
        tx = self.build_transaction(value_wei, gas_params)

        balance = self.web3.eth.get_balance(self.from_address)
        required = self.estimate_max_cost(tx)

        if balance < required:
            raise ValueError(
                f"Insufficient balance. Required={required}, balance={balance}"
            )

        try:
            signed = self.web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
            hex_hash = self.web3.to_hex(tx_hash)

            logger.info("Transaction broadcasted: %s", hex_hash)
            return hex_hash

        except InsufficientFunds:
            raise ValueError("Insufficient funds to cover gas.")
        except Exception:
            logger.exception("Transaction submission failed.")
            raise

    # --------------------------------------------------
    def wait_for_receipt(self, tx_hash: str) -> None:
        logger.info("Waiting for confirmation...")
        try:
            receipt = self.web3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=RECEIPT_TIMEOUT_SEC,
            )
            logger.info(
                "Confirmed in block %d | status=%s",
                receipt.blockNumber,
                receipt.status,
            )
        except TransactionNotFound:
            logger.warning("Transaction not found yet.")
        except Exception as exc:
            logger.warning("Receipt wait failed: %s", exc)

    # --------------------------------------------------
    def run(self) -> None:
        logger.info(
            "Sending %.6f ETH from %s to %s",
            self.amount_eth,
            self.from_address,
            self.to_address,
        )

        try:
            tx_hash = self.send()
            self.wait_for_receipt(tx_hash)
            logger.info("Transfer completed: %s", tx_hash)
        except Exception as exc:
            logger.critical("Execution failed: %s", exc)
            sys.exit(1)


# ==================================================
if __name__ == "__main__":
    EtherTransfer().run()
