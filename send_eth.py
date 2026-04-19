#!/usr/bin/env python3
"""
Ultra-hardened ETH transfer utility.

New improvements:
• Local nonce manager (no duplicate nonce under concurrency)
• Proper replacement tx logic (same nonce gas bump)
• EIP-1559 via feeHistory (more accurate)
• Balance safety margin
• Structured logging
• Graceful shutdown
• Better retry classification (Web3 + RPC)
"""

from __future__ import annotations

import os
import sys
import time
import json
import signal
import logging
import threading
from typing import Any, Callable, Dict, Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import geth_poa_middleware
from web3.exceptions import TransactionNotFound

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

MAX_PRIORITY_FEE_GWEI = 5
BASE_FEE_MULTIPLIER = 2
REPLACEMENT_BUMP = 1.125

MAX_GAS_PRICE_GWEI = 200

RETRY_ATTEMPTS = 4
RETRY_DELAY = 2

RECEIPT_TIMEOUT = 180
POLL_INTERVAL = 3

BALANCE_BUFFER = 1.02  # +2% safety

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# ==================================================
# Global control
# ==================================================

_shutdown = False


def handle_shutdown(sig, frame):
    global _shutdown
    logger.warning("Shutdown signal received...")
    _shutdown = True


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# ==================================================
# Nonce Manager (critical improvement)
# ==================================================

class NonceManager:
    def __init__(self, web3: Web3, address: str):
        self.web3 = web3
        self.address = address
        self.lock = threading.Lock()
        self.current_nonce: Optional[int] = None

    def get_next_nonce(self) -> int:
        with self.lock:
            if self.current_nonce is None:
                self.current_nonce = self.web3.eth.get_transaction_count(
                    self.address,
                    "pending"
                )
            else:
                self.current_nonce += 1

            return self.current_nonce


# ==================================================
# Helpers
# ==================================================

def env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        logger.critical("Missing env: %s", name)
        sys.exit(1)
    return v


def retry(func: Callable[..., Any], label: str, *args, **kwargs) -> Any:
    for i in range(RETRY_ATTEMPTS):
        try:
            return func(*args, **kwargs)
        except Exception as e:

            msg = str(e).lower()

            transient = any(x in msg for x in [
                "timeout", "connection", "429", "502", "503", "504"
            ])

            if not transient:
                raise

            logger.warning("%s retry %d/%d: %s", label, i + 1, RETRY_ATTEMPTS, e)
            time.sleep(RETRY_DELAY)

    raise RuntimeError(f"{label} failed")


# ==================================================
# Main
# ==================================================

class EtherTransfer:

    def __init__(self):

        load_dotenv()

        self.w3 = self._init_web3()

        self.private_key = env_required("PRIVATE_KEY")
        self.from_addr = Web3.to_checksum_address(env_required("FROM_ADDRESS"))
        self.to_addr = Web3.to_checksum_address(env_required("TO_ADDRESS"))

        self.amount = float(os.getenv("TRANSFER_AMOUNT", DEFAULT_AMOUNT_ETH))

        self.chain_id = self.w3.eth.chain_id
        self.nonce_manager = NonceManager(self.w3, self.from_addr)

        logger.info("Connected | Chain ID: %s", self.chain_id)

    # ==================================================

    def _init_web3(self) -> Web3:

        w3 = Web3(Web3.HTTPProvider(
            env_required("INFURA_URL"),
            request_kwargs={"timeout": 30}
        ))

        w3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not w3.is_connected():
            raise RuntimeError("Web3 connection failed")

        return w3

    # ==================================================
    # Gas (better EIP-1559)
    # ==================================================

    def get_gas_params(self, bump: float = 1.0) -> Dict[str, int]:

        try:
            fee_history = self.w3.eth.fee_history(5, "latest", [50])

            base_fee = fee_history["baseFeePerGas"][-1]
            reward = fee_history["reward"][-1][0]

            priority = min(
                int(reward * bump),
                self.w3.to_wei(MAX_PRIORITY_FEE_GWEI, "gwei")
            )

            max_fee = int(base_fee * BASE_FEE_MULTIPLIER * bump + priority)

            return {
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority,
            }

        except Exception as e:
            logger.warning("feeHistory failed, fallback: %s", e)

        gas_price = int(self.w3.eth.gas_price * bump)

        if gas_price > self.w3.to_wei(MAX_GAS_PRICE_GWEI, "gwei"):
            raise RuntimeError("Gas too high")

        return {"gasPrice": gas_price}

    # ==================================================

    def estimate_gas(self, value: int) -> int:
        try:
            g = self.w3.eth.estimate_gas({
                "from": self.from_addr,
                "to": self.to_addr,
                "value": value,
            })
            return int(g * 1.2)
        except Exception:
            return DEFAULT_GAS_LIMIT

    # ==================================================

    def build_tx(self, value: int, gas: Dict[str, int], nonce: int):

        return {
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": self.to_addr,
            "value": value,
            "gas": self.estimate_gas(value),
            **gas,
        }

    # ==================================================

    def send_with_replacement(self) -> str:

        value = self.w3.to_wei(self.amount, "ether")
        nonce = self.nonce_manager.get_next_nonce()

        last_tx = None

        for attempt in range(RETRY_ATTEMPTS):

            if _shutdown:
                raise RuntimeError("Shutdown requested")

            bump = REPLACEMENT_BUMP ** attempt

            gas = self.get_gas_params(bump)
            tx = self.build_tx(value, gas, nonce)

            balance = self.w3.eth.get_balance(self.from_addr)
            required = (tx["value"] + tx["gas"] * (tx.get("maxFeePerGas") or tx.get("gasPrice")))

            if balance < required * BALANCE_BUFFER:
                raise RuntimeError("Insufficient balance (with buffer)")

            if DRY_RUN:
                logger.info("DRY RUN:\n%s", json.dumps(tx, indent=2))
                return "0xDRYRUN"

            signed = self.w3.eth.account.sign_transaction(tx, self.private_key)

            try:
                tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
                hex_hash = self.w3.to_hex(tx_hash)

                logger.info("Sent tx %s (attempt %d)", hex_hash, attempt + 1)

                last_tx = hex_hash
                return hex_hash

            except Exception as e:

                msg = str(e).lower()

                # replacement logic trigger
                if "already known" in msg or "nonce too low" in msg:
                    logger.warning("Replacement triggered (same nonce)")
                else:
                    logger.warning("Send error: %s", e)

                time.sleep(RETRY_DELAY)

        raise RuntimeError("Failed to send tx")

    # ==================================================

    def wait_receipt(self, tx_hash: str):

        start = time.time()

        while True:

            if _shutdown:
                raise RuntimeError("Shutdown during wait")

            if time.time() - start > RECEIPT_TIMEOUT:
                raise TimeoutError("Receipt timeout")

            try:
                r = self.w3.eth.get_transaction_receipt(tx_hash)

                if r:

                    if r.status != 1:
                        raise RuntimeError("Tx reverted")

                    logger.info("Confirmed in block %s", r.blockNumber)
                    return

            except TransactionNotFound:
                pass

            time.sleep(POLL_INTERVAL)

    # ==================================================

    def run(self):

        logger.info("Transfer %.6f ETH → %s", self.amount, self.to_addr)

        try:
            tx_hash = self.send_with_replacement()

            if not DRY_RUN:
                self.wait_receipt(tx_hash)

            logger.info("SUCCESS")

        except Exception as e:
            logger.exception("FAILED: %s", e)
            sys.exit(1)


# ==================================================

if __name__ == "__main__":
    EtherTransfer().run()
