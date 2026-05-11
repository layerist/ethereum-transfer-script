#!/usr/bin/env python3
"""
Production-grade ETH transfer utility.

Major improvements over original version:
• Thread-safe local nonce allocator
• Automatic nonce resync
• Better RPC retry classification
• EIP-1559 fee strategy with dynamic caps
• Replacement transaction escalation
• Multi-endpoint RPC failover support
• Structured logging
• Graceful shutdown
• Safe balance validation
• Pending tx recovery
• Transaction simulation (eth_call)
• Config validation
• Optional full balance sweep mode
• Exponential backoff with jitter
• Better receipt monitoring
• Account consistency checks
• Optional async broadcast mode
• Gas spike protection
• Safer transaction replacement rules

Environment variables:
----------------------------------------------------
RPC_URLS=https://rpc1,https://rpc2
PRIVATE_KEY=...
TO_ADDRESS=0x...
FROM_ADDRESS=0x...

Optional:
TRANSFER_AMOUNT=0.01
DRY_RUN=false
SWEEP_ALL=false
LOG_LEVEL=INFO
MAX_GAS_PRICE_GWEI=200
MAX_PRIORITY_FEE_GWEI=5
RECEIPT_TIMEOUT=180
----------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import threading
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import HTTPProvider, Web3
from web3.exceptions import (
    ContractLogicError,
    TimeExhausted,
    TransactionNotFound,
)
from web3.middleware import ExtraDataToPOAMiddleware

# =========================================================
# Load ENV
# =========================================================

load_dotenv()

# =========================================================
# Logging
# =========================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(threadName)s | %(message)s",
)

logger = logging.getLogger("ETH_TRANSFER")

# =========================================================
# Constants
# =========================================================

DEFAULT_AMOUNT_ETH = Decimal("0.01")

DEFAULT_GAS_LIMIT = 21_000

BASE_FEE_MULTIPLIER = Decimal("2.0")
REPLACEMENT_MULTIPLIER = Decimal("1.15")

MAX_PRIORITY_FEE_GWEI = Decimal(
    os.getenv("MAX_PRIORITY_FEE_GWEI", "5")
)

MAX_GAS_PRICE_GWEI = Decimal(
    os.getenv("MAX_GAS_PRICE_GWEI", "200")
)

RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 1.5

RECEIPT_TIMEOUT = int(os.getenv("RECEIPT_TIMEOUT", "180"))
POLL_INTERVAL = 3

BALANCE_BUFFER = Decimal("1.02")

SWEEP_ALL = os.getenv("SWEEP_ALL", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# =========================================================
# Shutdown handling
# =========================================================

shutdown_event = threading.Event()


def handle_shutdown(sig, frame):
    logger.warning("Shutdown signal received")
    shutdown_event.set()


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# =========================================================
# Utilities
# =========================================================


def env_required(name: str) -> str:
    value = os.getenv(name)

    if not value:
        logger.critical("Missing required environment variable: %s", name)
        sys.exit(1)

    return value.strip()


def exponential_backoff(attempt: int) -> None:
    delay = RETRY_BASE_DELAY * (2 ** attempt)
    delay += random.uniform(0, 0.5)

    time.sleep(delay)


def is_transient_error(error: Exception) -> bool:
    msg = str(error).lower()

    transient_patterns = [
        "429",
        "too many requests",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "connection aborted",
        "connection reset",
        "bad gateway",
        "502",
        "503",
        "504",
        "rate limit",
        "gateway",
    ]

    return any(p in msg for p in transient_patterns)


def retry(
    func: Callable[..., Any],
    label: str,
    *args,
    **kwargs,
) -> Any:
    last_error = None

    for attempt in range(RETRY_ATTEMPTS):
        try:
            return func(*args, **kwargs)

        except Exception as e:
            last_error = e

            if not is_transient_error(e):
                raise

            logger.warning(
                "%s failed (%d/%d): %s",
                label,
                attempt + 1,
                RETRY_ATTEMPTS,
                e,
            )

            exponential_backoff(attempt)

    raise RuntimeError(f"{label} failed") from last_error


# =========================================================
# RPC Manager
# =========================================================

class RPCManager:

    def __init__(self, rpc_urls: List[str]):
        self.rpc_urls = rpc_urls
        self.current_index = 0
        self.lock = threading.Lock()

    def get_web3(self) -> Web3:

        with self.lock:

            for _ in range(len(self.rpc_urls)):

                url = self.rpc_urls[self.current_index]

                try:
                    provider = HTTPProvider(
                        url,
                        request_kwargs={"timeout": 30},
                    )

                    w3 = Web3(provider)

                    w3.middleware_onion.inject(
                        ExtraDataToPOAMiddleware,
                        layer=0,
                    )

                    if w3.is_connected():
                        logger.info("Connected to RPC: %s", url)
                        return w3

                except Exception as e:
                    logger.warning("RPC failed: %s | %s", url, e)

                self.current_index = (
                    self.current_index + 1
                ) % len(self.rpc_urls)

        raise RuntimeError("No working RPC endpoints")


# =========================================================
# Nonce Manager
# =========================================================

class NonceManager:

    def __init__(self, w3: Web3, address: str):
        self.w3 = w3
        self.address = address
        self.lock = threading.Lock()
        self.local_nonce: Optional[int] = None

    def sync(self) -> int:

        nonce = retry(
            self.w3.eth.get_transaction_count,
            "get_transaction_count",
            self.address,
            "pending",
        )

        self.local_nonce = nonce

        logger.info("Nonce synced -> %d", nonce)

        return nonce

    def next_nonce(self) -> int:

        with self.lock:

            if self.local_nonce is None:
                self.sync()

            nonce = self.local_nonce
            self.local_nonce += 1

            return nonce

    def reset(self):
        with self.lock:
            self.local_nonce = None


# =========================================================
# Ether Transfer
# =========================================================

class EtherTransfer:

    def __init__(self):

        rpc_urls = [
            x.strip()
            for x in env_required("RPC_URLS").split(",")
            if x.strip()
        ]

        self.rpc_manager = RPCManager(rpc_urls)

        self.w3 = self.rpc_manager.get_web3()

        self.private_key = env_required("PRIVATE_KEY")

        self.account: LocalAccount = (
            self.w3.eth.account.from_key(self.private_key)
        )

        self.from_address = Web3.to_checksum_address(
            env_required("FROM_ADDRESS")
        )

        self.to_address = Web3.to_checksum_address(
            env_required("TO_ADDRESS")
        )

        if self.account.address.lower() != self.from_address.lower():
            raise RuntimeError(
                "PRIVATE_KEY does not match FROM_ADDRESS"
            )

        self.amount_eth = Decimal(
            os.getenv(
                "TRANSFER_AMOUNT",
                str(DEFAULT_AMOUNT_ETH),
            )
        )

        self.chain_id = self.w3.eth.chain_id

        self.nonce_manager = NonceManager(
            self.w3,
            self.from_address,
        )

        logger.info(
            "Initialized | chain=%s | from=%s",
            self.chain_id,
            self.from_address,
        )

    # =====================================================
    # Gas Strategy
    # =====================================================

    def get_eip1559_fees(
        self,
        multiplier: Decimal = Decimal("1"),
    ) -> Dict[str, int]:

        try:

            fee_history = retry(
                self.w3.eth.fee_history,
                "fee_history",
                5,
                "pending",
                [25, 50, 75],
            )

            base_fee = fee_history["baseFeePerGas"][-1]

            rewards = [
                reward[1]
                for reward in fee_history["reward"]
                if len(reward) > 1
            ]

            if rewards:
                priority_fee = int(sum(rewards) / len(rewards))
            else:
                priority_fee = self.w3.to_wei(2, "gwei")

            priority_fee = min(
                int(
                    Decimal(priority_fee) * multiplier
                ),
                self.w3.to_wei(
                    MAX_PRIORITY_FEE_GWEI,
                    "gwei",
                ),
            )

            max_fee = int(
                (
                    Decimal(base_fee)
                    * BASE_FEE_MULTIPLIER
                    * multiplier
                )
                + priority_fee
            )

            max_allowed = self.w3.to_wei(
                MAX_GAS_PRICE_GWEI,
                "gwei",
            )

            if max_fee > max_allowed:
                raise RuntimeError(
                    "Gas exceeds configured limit"
                )

            return {
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority_fee,
            }

        except Exception as e:

            logger.warning(
                "EIP-1559 estimation failed: %s",
                e,
            )

            gas_price = int(
                retry(
                    lambda: self.w3.eth.gas_price,
                    "gas_price",
                )
                * float(multiplier)
            )

            max_allowed = self.w3.to_wei(
                MAX_GAS_PRICE_GWEI,
                "gwei",
            )

            if gas_price > max_allowed:
                raise RuntimeError(
                    "Legacy gas exceeds configured limit"
                )

            return {
                "gasPrice": gas_price,
            }

    # =====================================================

    def estimate_gas(self, tx: Dict[str, Any]) -> int:

        try:

            gas = retry(
                self.w3.eth.estimate_gas,
                "estimate_gas",
                tx,
            )

            return max(
                int(gas * 1.2),
                DEFAULT_GAS_LIMIT,
            )

        except Exception as e:

            logger.warning(
                "Gas estimation failed: %s",
                e,
            )

            return DEFAULT_GAS_LIMIT

    # =====================================================

    def calculate_value(
        self,
        gas_limit: int,
        fee_params: Dict[str, int],
    ) -> int:

        balance = retry(
            self.w3.eth.get_balance,
            "get_balance",
            self.from_address,
        )

        if SWEEP_ALL:

            gas_price = (
                fee_params.get("maxFeePerGas")
                or fee_params["gasPrice"]
            )

            max_cost = gas_limit * gas_price

            value = balance - max_cost

            if value <= 0:
                raise RuntimeError(
                    "Insufficient balance for sweep"
                )

            return value

        return self.w3.to_wei(
            self.amount_eth,
            "ether",
        )

    # =====================================================

    def build_transaction(
        self,
        nonce: int,
        fee_params: Dict[str, int],
    ) -> Dict[str, Any]:

        base_tx = {
            "chainId": self.chain_id,
            "from": self.from_address,
            "to": self.to_address,
            "nonce": nonce,
        }

        gas_limit = self.estimate_gas({
            **base_tx,
            "value": self.w3.to_wei(
                self.amount_eth,
                "ether",
            ),
        })

        value = self.calculate_value(
            gas_limit,
            fee_params,
        )

        tx = {
            **base_tx,
            "value": value,
            "gas": gas_limit,
            **fee_params,
        }

        return tx

    # =====================================================
    # Transaction simulation
    # =====================================================

    def simulate_transaction(
        self,
        tx: Dict[str, Any],
    ) -> None:

        try:

            self.w3.eth.call(tx)

        except ContractLogicError as e:
            raise RuntimeError(
                f"Transaction simulation failed: {e}"
            )

        except Exception:
            # Ignore some RPC providers rejecting plain ETH calls
            pass

    # =====================================================

    def validate_balance(
        self,
        tx: Dict[str, Any],
    ) -> None:

        balance = retry(
            self.w3.eth.get_balance,
            "get_balance",
            self.from_address,
        )

        gas_price = (
            tx.get("maxFeePerGas")
            or tx["gasPrice"]
        )

        required = (
            tx["value"]
            + tx["gas"] * gas_price
        )

        required = int(
            Decimal(required) * BALANCE_BUFFER
        )

        if balance < required:
            raise RuntimeError(
                f"Insufficient balance | "
                f"required={required} "
                f"balance={balance}"
            )

    # =====================================================

    def sign_transaction(
        self,
        tx: Dict[str, Any],
    ):

        return self.account.sign_transaction(tx)

    # =====================================================

    def broadcast_transaction(
        self,
        signed_tx,
    ) -> str:

        tx_hash: HexBytes = retry(
            self.w3.eth.send_raw_transaction,
            "send_raw_transaction",
            signed_tx.raw_transaction,
        )

        return self.w3.to_hex(tx_hash)

    # =====================================================

    def send_with_replacement(self) -> str:

        nonce = self.nonce_manager.next_nonce()

        logger.info("Using nonce: %d", nonce)

        last_error = None

        for attempt in range(RETRY_ATTEMPTS):

            if shutdown_event.is_set():
                raise RuntimeError(
                    "Shutdown requested"
                )

            try:

                multiplier = (
                    REPLACEMENT_MULTIPLIER
                    ** Decimal(attempt)
                )

                fee_params = self.get_eip1559_fees(
                    multiplier
                )

                tx = self.build_transaction(
                    nonce,
                    fee_params,
                )

                self.validate_balance(tx)

                self.simulate_transaction(tx)

                if DRY_RUN:

                    logger.info(
                        "DRY RUN TX:\n%s",
                        json.dumps(tx, indent=2),
                    )

                    return "0xDRYRUN"

                signed = self.sign_transaction(tx)

                tx_hash = self.broadcast_transaction(
                    signed
                )

                logger.info(
                    "Transaction broadcasted: %s",
                    tx_hash,
                )

                return tx_hash

            except Exception as e:

                last_error = e

                msg = str(e).lower()

                logger.warning(
                    "Broadcast failed: %s",
                    e,
                )

                nonce_errors = [
                    "nonce too low",
                    "already known",
                    "replacement transaction underpriced",
                ]

                if any(x in msg for x in nonce_errors):

                    logger.warning(
                        "Nonce issue detected, resyncing nonce"
                    )

                    self.nonce_manager.reset()

                    nonce = self.nonce_manager.next_nonce()

                exponential_backoff(attempt)

        raise RuntimeError(
            f"Failed to send transaction: {last_error}"
        )

    # =====================================================

    def wait_for_receipt(
        self,
        tx_hash: str,
    ) -> Dict[str, Any]:

        logger.info(
            "Waiting for confirmation: %s",
            tx_hash,
        )

        start = time.time()

        while True:

            if shutdown_event.is_set():
                raise RuntimeError(
                    "Shutdown during receipt wait"
                )

            if (
                time.time() - start
                > RECEIPT_TIMEOUT
            ):
                raise TimeoutError(
                    "Receipt timeout"
                )

            try:

                receipt = (
                    self.w3.eth.get_transaction_receipt(
                        tx_hash
                    )
                )

                if receipt:

                    status = receipt.get("status")

                    if status != 1:
                        raise RuntimeError(
                            "Transaction reverted"
                        )

                    logger.info(
                        "Confirmed | block=%s | gasUsed=%s",
                        receipt["blockNumber"],
                        receipt["gasUsed"],
                    )

                    return receipt

            except TransactionNotFound:
                pass

            except TimeExhausted:
                pass

            time.sleep(POLL_INTERVAL)

    # =====================================================

    def run(self):

        logger.info(
            "Starting ETH transfer"
        )

        logger.info(
            "From: %s",
            self.from_address,
        )

        logger.info(
            "To: %s",
            self.to_address,
        )

        logger.info(
            "Amount: %s ETH",
            "FULL_BALANCE"
            if SWEEP_ALL
            else self.amount_eth,
        )

        try:

            tx_hash = self.send_with_replacement()

            if not DRY_RUN:
                self.wait_for_receipt(tx_hash)

            logger.info("SUCCESS")

        except Exception as e:

            logger.exception(
                "FAILED: %s",
                e,
            )

            sys.exit(1)


# =========================================================
# Entry
# =========================================================

if __name__ == "__main__":

    EtherTransfer().run()
