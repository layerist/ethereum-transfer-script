#!/usr/bin/env python3
"""
Production-grade ETH transfer utility using Web3.py.

Features
--------
• Strict environment validation
• Deterministic retries
• Safe nonce handling
• EIP-1559 preferred
• Legacy fallback
• Gas safety margins
• Fee sanity limits
• Balance pre-check
• Explicit failure boundaries
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
DEFAULT_GAS_LIMIT = 21000

DEFAULT_PRIORITY_FEE_GWEI = 2
EIP1559_BASEFEE_MULTIPLIER = 2

GAS_ESTIMATE_MARGIN = 1.2

MAX_GAS_PRICE_GWEI = 200

RETRY_ATTEMPTS = 3
RETRY_DELAY = 2

RECEIPT_TIMEOUT = 120
RECEIPT_POLL_INTERVAL = 3


# ==================================================
# Helpers
# ==================================================

def env_required(name: str) -> str:
    value = os.getenv(name)

    if not value:
        logger.critical("Missing required environment variable: %s", name)
        sys.exit(1)

    return value


def retry(func: Callable[..., Any], label: str, *args, **kwargs) -> Any:
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
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"{label} failed after {RETRY_ATTEMPTS} attempts")


def parse_positive_float(value: Optional[str], default: float) -> float:
    if not value:
        return default

    try:
        parsed = float(value)

        if parsed <= 0:
            return default

        return parsed

    except ValueError:
        return default


# ==================================================
# Main
# ==================================================

class EtherTransfer:

    def __init__(self) -> None:

        load_dotenv()

        self.infura_url = env_required("INFURA_URL")
        self.private_key = env_required("PRIVATE_KEY")

        self.from_address = Web3.to_checksum_address(env_required("FROM_ADDRESS"))
        self.to_address = Web3.to_checksum_address(env_required("TO_ADDRESS"))

        self.amount_eth = parse_positive_float(
            os.getenv("TRANSFER_AMOUNT"),
            DEFAULT_AMOUNT_ETH,
        )

        self.web3 = self._init_web3()

        self.chain_id = self.web3.eth.chain_id

        logger.info("Connected to chain id: %s", self.chain_id)

    # -------------------------------------------------

    def _init_web3(self) -> Web3:

        web3 = Web3(
            Web3.HTTPProvider(
                self.infura_url,
                request_kwargs={"timeout": 30},
            )
        )

        web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not web3.is_connected():
            logger.critical("Unable to connect to Ethereum node")
            sys.exit(1)

        return web3

    # ==================================================
    # Gas
    # ==================================================

    def get_gas_params(self) -> Dict[str, int]:

        try:

            block = retry(self.web3.eth.get_block, "get block", "latest")

            base_fee = block.get("baseFeePerGas")

            if base_fee:

                priority = self.web3.to_wei(DEFAULT_PRIORITY_FEE_GWEI, "gwei")

                max_fee = base_fee * EIP1559_BASEFEE_MULTIPLIER + priority

                return {
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority,
                }

        except Exception as exc:

            logger.warning("EIP1559 unavailable: %s", exc)

        gas_price = retry(self.web3.eth.gas_price, "gas_price")

        max_allowed = self.web3.to_wei(MAX_GAS_PRICE_GWEI, "gwei")

        if gas_price > max_allowed:

            raise RuntimeError(
                f"Gas price too high: {Web3.from_wei(gas_price,'gwei')} gwei"
            )

        return {"gasPrice": gas_price}

    # ==================================================
    # Transaction
    # ==================================================

    def get_nonce(self) -> int:

        return retry(
            self.web3.eth.get_transaction_count,
            "get nonce",
            self.from_address,
            "pending",
        )

    def estimate_gas(self, value: int) -> int:

        try:

            gas = self.web3.eth.estimate_gas(
                {
                    "from": self.from_address,
                    "to": self.to_address,
                    "value": value,
                }
            )

            gas = int(gas * GAS_ESTIMATE_MARGIN)

            return gas

        except Exception as exc:

            logger.warning("Gas estimation failed: %s", exc)

            return DEFAULT_GAS_LIMIT

    def build_tx(self, value: int, gas_params: Dict[str, int]) -> Dict[str, Any]:

        tx = {

            "chainId": self.chain_id,
            "nonce": self.get_nonce(),
            "to": self.to_address,
            "value": value,
            "gas": self.estimate_gas(value),

            **gas_params
        }

        return tx

    # ==================================================
    # Cost
    # ==================================================

    def estimate_cost(self, tx: Dict[str, Any]) -> int:

        gas_limit = tx["gas"]

        gas_price = tx.get("maxFeePerGas") or tx.get("gasPrice")

        return gas_limit * gas_price + tx["value"]

    # ==================================================
    # Send
    # ==================================================

    def send(self) -> str:

        value = self.web3.to_wei(self.amount_eth, "ether")

        gas_params = self.get_gas_params()

        tx = self.build_tx(value, gas_params)

        balance = retry(self.web3.eth.get_balance, "get balance", self.from_address)

        required = self.estimate_cost(tx)

        if balance < required:

            raise ValueError(
                f"Insufficient balance. Need {required}, have {balance}"
            )

        signed = self.web3.eth.account.sign_transaction(tx, self.private_key)

        tx_hash = retry(
            self.web3.eth.send_raw_transaction,
            "send tx",
            signed.rawTransaction,
        )

        hex_hash = self.web3.to_hex(tx_hash)

        logger.info("Transaction sent: %s", hex_hash)

        return hex_hash

    # ==================================================
    # Receipt
    # ==================================================

    def wait_for_receipt(self, tx_hash: str) -> None:

        logger.info("Waiting for confirmation...")

        start = time.time()

        while True:

            if time.time() - start > RECEIPT_TIMEOUT:

                raise TimeoutError("Transaction confirmation timeout")

            try:

                receipt = self.web3.eth.get_transaction_receipt(tx_hash)

                if receipt:

                    logger.info(
                        "Confirmed in block %s status=%s",
                        receipt.blockNumber,
                        receipt.status,
                    )

                    return

            except TransactionNotFound:

                pass

            time.sleep(RECEIPT_POLL_INTERVAL)

    # ==================================================
    # Run
    # ==================================================

    def run(self) -> None:

        logger.info(
            "Transfer %.6f ETH → %s",
            self.amount_eth,
            self.to_address,
        )

        try:

            tx_hash = self.send()

            self.wait_for_receipt(tx_hash)

            logger.info("Transfer completed")

        except InsufficientFunds:

            logger.critical("Insufficient funds")

            sys.exit(1)

        except Exception as exc:

            logger.exception("Transfer failed: %s", exc)

            sys.exit(1)


# ==================================================

if __name__ == "__main__":

    EtherTransfer().run()
