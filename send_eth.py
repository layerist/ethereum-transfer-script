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
    format="\033[94m%(asctime)s\033[0m - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("EtherTransfer")


# =========================
# Constants
# =========================
DEFAULT_AMOUNT_ETH = 0.01
DEFAULT_GAS_LIMIT = 21_000
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2


# =========================
# Helper
# =========================
def env_or_fail(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.critical(f"Missing env var: {name}")
        sys.exit(1)
    return value


# =========================
# Main Class
# =========================
class EtherTransfer:
    """Utility for sending ETH using Web3."""

    def __init__(self):
        load_dotenv()

        # Required
        self.infura_url = env_or_fail("INFURA_URL")
        self.from_address = env_or_fail("FROM_ADDRESS")
        self.to_address = env_or_fail("TO_ADDRESS")
        self.private_key = env_or_fail("PRIVATE_KEY")

        # Optional
        self.transfer_amount = float(os.getenv("TRANSFER_AMOUNT", DEFAULT_AMOUNT_ETH))
        self.custom_gas_price = os.getenv("DEFAULT_GAS_PRICE")

        # Web3
        self.web3 = Web3(Web3.HTTPProvider(self.infura_url))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not self.web3.is_connected():
            logger.critical("Unable to connect to Ethereum network.")
            sys.exit(1)

        self._validate_inputs()

    # -------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------
    def _validate_inputs(self) -> None:
        """Validate addresses, private key, and transfer amount."""
        for label, address in [("FROM_ADDRESS", self.from_address), ("TO_ADDRESS", self.to_address)]:
            if not Web3.is_address(address):
                raise ValueError(f"Invalid {label}: {address}")

        if self.private_key.startswith("0x"):
            logger.debug("Private key loaded securely.")

        if self.transfer_amount <= 0:
            raise ValueError("TRANSFER_AMOUNT must be positive")

    # -------------------------------------------------------------
    # Retry Wrapper
    # -------------------------------------------------------------
    def retry(self, func: Callable, label: str = "", *args, **kwargs):
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"{label} failed (attempt {attempt}/{RETRY_ATTEMPTS}): {e}")
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(RETRY_DELAY)
        raise RuntimeError(f"{label} failed after {RETRY_ATTEMPTS} attempts")

    # -------------------------------------------------------------
    # Gas Logic
    # -------------------------------------------------------------
    def get_gas_price(self) -> Dict[str, int]:
        """Return appropriate gas settings."""
        if self.custom_gas_price:
            try:
                gp = int(self.custom_gas_price)
                if gp > 0:
                    logger.info(f"Using custom gasPrice: {gp}")
                    return {"gasPrice": gp}
            except:
                logger.warning("Invalid DEFAULT_GAS_PRICE — ignoring it.")

        # Try EIP-1559
        try:
            fee_data = self.web3.eth.fee_history(1, "latest")
            base_fee = fee_data["baseFeePerGas"][-1]
            priority = self.web3.to_wei(2, "gwei")
            max_fee = base_fee + priority
            return {
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority
            }
        except Exception:
            logger.debug("EIP-1559 not available. Using legacy gas price.")

        # Legacy
        gas_price = self.retry(self.web3.eth.gas_price, "Fetching gas price")
        return {"gasPrice": gas_price}

    # -------------------------------------------------------------
    # Core Blockchain Actions
    # -------------------------------------------------------------
    def get_nonce(self, address: str) -> int:
        return self.retry(self.web3.eth.get_transaction_count, "Get nonce", address)

    def estimate_gas(self, value: int) -> int:
        try:
            return self.web3.eth.estimate_gas({
                "from": self.from_address,
                "to": self.to_address,
                "value": value
            })
        except Exception as e:
            logger.warning(f"Gas estimation failed: {e}. Using fallback {DEFAULT_GAS_LIMIT}.")
            return DEFAULT_GAS_LIMIT

    # -------------------------------------------------------------
    def build_tx(self, value_wei: int, gas_params: Dict[str, int]) -> Dict[str, Any]:
        return {
            "nonce": self.get_nonce(self.from_address),
            "to": self.to_address,
            "value": value_wei,
            "gas": self.estimate_gas(value_wei),
            "chainId": self.web3.eth.chain_id,
            **gas_params
        }

    # -------------------------------------------------------------
    def send_eth(self) -> str:
        value_wei = self.web3.to_wei(self.transfer_amount, "ether")

        # Check balance
        balance = self.web3.eth.get_balance(self.from_address)
        if balance < value_wei:
            raise ValueError(f"Insufficient balance: need {self.transfer_amount} ETH")

        gas_params = self.get_gas_price()
        tx = self.build_tx(value_wei, gas_params)

        try:
            signed = self.web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
            hex_hash = self.web3.to_hex(tx_hash)
            logger.info(f"Transaction sent: {hex_hash}")
            return hex_hash
        except exceptions.InsufficientFunds:
            raise ValueError("Not enough ETH to pay gas fees.")
        except Exception as e:
            logger.exception(f"Transaction failed: {e}")
            raise

    # -------------------------------------------------------------
    def run(self):
        logger.info(f"Sending {self.transfer_amount} ETH...")
        try:
            tx_hash = self.send_eth()
            logger.info(f"Transfer completed: {tx_hash}")
        except Exception as e:
            logger.critical(f"Execution failed: {e}")
            sys.exit(1)


if __name__ == "__main__":
    EtherTransfer().run()
