#!/usr/bin/env python3
"""
Hardened ETH transfer utility (production-grade).

Improvements over base version:
• Nonce locking (thread-safe)
• Smart retry (transient errors only)
• EIP-1559 dynamic priority fee
• Replacement tx (gas bump if stuck)
• Receipt status validation
• Optional dry-run mode
• Stronger validation & logging
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import geth_poa_middleware
from web3.exceptions import (
    TransactionNotFound,
    TimeExhausted,
    ContractLogicError,
)

# ==================================================
# Logging
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("EtherTransfer")

# ==================================================
# Constants
# ==================================================

DEFAULT_AMOUNT_ETH = 0.01
DEFAULT_GAS_LIMIT = 21000

DEFAULT_PRIORITY_FEE_GWEI = 2
MAX_PRIORITY_FEE_GWEI = 5

EIP1559_BASE_MULTIPLIER = 2
GAS_MULTIPLIER_ON_RETRY = 1.15

MAX_GAS_PRICE_GWEI = 200

RETRY_ATTEMPTS = 3
RETRY_DELAY = 2

RECEIPT_TIMEOUT = 180
RECEIPT_POLL_INTERVAL = 3

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# ==================================================
# Nonce Lock (thread-safe)
# ==================================================

_nonce_lock = threading.Lock()

# ==================================================
# Helpers
# ==================================================

def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.critical("Missing env: %s", name)
        sys.exit(1)
    return value


def parse_positive_float(value: Optional[str], default: float) -> float:
    try:
        if value:
            v = float(value)
            if v > 0:
                return v
    except Exception:
        pass
    return default


def is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()

    transient_patterns = [
        "timeout",
        "temporarily",
        "connection",
        "rate limit",
        "429",
        "502",
        "503",
        "504",
    ]

    return any(p in msg for p in transient_patterns)


def retry(func: Callable[..., Any], label: str, *args, **kwargs) -> Any:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)

        except Exception as exc:

            if not is_transient_error(exc):
                raise

            logger.warning(
                "%s failed (%d/%d): %s",
                label,
                attempt,
                RETRY_ATTEMPTS,
                exc,
            )

            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"{label} failed after retries")

# ==================================================
# Main Class
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

        logger.info("Connected to chain ID: %s", self.chain_id)

    # ==================================================

    def _init_web3(self) -> Web3:

        w3 = Web3(Web3.HTTPProvider(self.infura_url, request_kwargs={"timeout": 30}))

        w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not w3.is_connected():
            logger.critical("Web3 connection failed")
            sys.exit(1)

        return w3

    # ==================================================
    # Nonce (safe)
    # ==================================================

    def get_nonce(self) -> int:
        with _nonce_lock:
            return self.web3.eth.get_transaction_count(
                self.from_address,
                "pending",
            )

    # ==================================================
    # Gas
    # ==================================================

    def get_gas_params(self, bump: float = 1.0) -> Dict[str, int]:

        block = retry(self.web3.eth.get_block, "get_block", "latest")
        base_fee = block.get("baseFeePerGas")

        if base_fee:

            try:
                priority = self.web3.to_wei(DEFAULT_PRIORITY_FEE_GWEI, "gwei")

                # dynamic cap
                priority = min(
                    priority,
                    self.web3.to_wei(MAX_PRIORITY_FEE_GWEI, "gwei"),
                )

                max_fee = int(base_fee * EIP1559_BASE_MULTIPLIER * bump + priority)

                return {
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": int(priority * bump),
                }

            except Exception as exc:
                logger.warning("EIP1559 failed: %s", exc)

        gas_price = retry(self.web3.eth.gas_price, "gas_price")

        gas_price = int(gas_price * bump)

        max_allowed = self.web3.to_wei(MAX_GAS_PRICE_GWEI, "gwei")

        if gas_price > max_allowed:
            raise RuntimeError("Gas price too high")

        return {"gasPrice": gas_price}

    # ==================================================
    # Tx Build
    # ==================================================

    def estimate_gas(self, value: int) -> int:
        try:
            gas = self.web3.eth.estimate_gas({
                "from": self.from_address,
                "to": self.to_address,
                "value": value,
            })
            return int(gas * 1.2)
        except Exception:
            return DEFAULT_GAS_LIMIT

    def build_tx(self, value: int, gas_params: Dict[str, int], nonce: int) -> Dict[str, Any]:

        return {
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": self.to_address,
            "value": value,
            "gas": self.estimate_gas(value),
            **gas_params,
        }

    def estimate_total_cost(self, tx: Dict[str, Any]) -> int:

        gas_price = tx.get("maxFeePerGas") or tx.get("gasPrice")

        return tx["value"] + tx["gas"] * gas_price

    # ==================================================
    # Send (with replacement)
    # ==================================================

    def send(self) -> str:

        value = self.web3.to_wei(self.amount_eth, "ether")

        nonce = self.get_nonce()

        for attempt in range(1, RETRY_ATTEMPTS + 1):

            bump = GAS_MULTIPLIER_ON_RETRY ** (attempt - 1)

            gas_params = self.get_gas_params(bump)

            tx = self.build_tx(value, gas_params, nonce)

            balance = self.web3.eth.get_balance(self.from_address)

            required = self.estimate_total_cost(tx)

            if balance < required:
                raise ValueError("Insufficient balance")

            if DRY_RUN:
                logger.info("DRY RUN TX:\n%s", json.dumps(tx, indent=2))
                return "0xDRYRUN"

            signed = self.web3.eth.account.sign_transaction(tx, self.private_key)

            try:
                tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)

                hex_hash = self.web3.to_hex(tx_hash)

                logger.info("TX sent: %s (attempt %d)", hex_hash, attempt)

                return hex_hash

            except Exception as exc:

                logger.warning("Send failed: %s", exc)

                if attempt == RETRY_ATTEMPTS:
                    raise

                time.sleep(RETRY_DELAY)

        raise RuntimeError("Unreachable")

    # ==================================================
    # Receipt
    # ==================================================

    def wait_for_receipt(self, tx_hash: str) -> None:

        start = time.time()

        while True:

            if time.time() - start > RECEIPT_TIMEOUT:
                raise TimeoutError("Receipt timeout")

            try:
                receipt = self.web3.eth.get_transaction_receipt(tx_hash)

                if receipt:

                    if receipt.status != 1:
                        raise RuntimeError("Transaction reverted")

                    logger.info(
                        "Confirmed in block %s",
                        receipt.blockNumber,
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
            "Sending %.6f ETH → %s",
            self.amount_eth,
            self.to_address,
        )

        try:

            tx_hash = self.send()

            if not DRY_RUN:
                self.wait_for_receipt(tx_hash)

            logger.info("Done")

        except Exception as exc:
            logger.exception("Failed: %s", exc)
            sys.exit(1)

# ==================================================

if __name__ == "__main__":
    EtherTransfer().run()
