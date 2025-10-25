import os
import sys
import time
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from web3 import Web3, exceptions
from web3.middleware import geth_poa_middleware

# === Environment Setup ===
load_dotenv()

# === Logging Configuration ===
logging.basicConfig(
    level=logging.INFO,
    format="\033[94m%(asctime)s\033[0m - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("EtherTransfer")

# === Constants ===
DEFAULT_GAS_LIMIT = 21_000
DEFAULT_AMOUNT_ETH = 0.01
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2  # seconds


class EtherTransfer:
    """Ethereum ETH transfer utility using Web3 and Infura."""

    def __init__(self):
        self.infura_url = os.getenv("INFURA_URL")
        self.from_address = os.getenv("FROM_ADDRESS")
        self.to_address = os.getenv("TO_ADDRESS")
        self.private_key = os.getenv("PRIVATE_KEY")
        self.custom_gas_price = os.getenv("DEFAULT_GAS_PRICE")
        self.transfer_amount = os.getenv("TRANSFER_AMOUNT")

        if not self.infura_url:
            logger.critical("Missing INFURA_URL in environment variables.")
            sys.exit(1)

        self.web3 = Web3(Web3.HTTPProvider(self.infura_url))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        if not self.web3.is_connected():
            logger.critical("Unable to connect to Ethereum network.")
            sys.exit(1)

        self._validate_env()

    def _validate_env(self) -> None:
        """Validate required environment variables and addresses."""
        required_vars = {
            "FROM_ADDRESS": self.from_address,
            "TO_ADDRESS": self.to_address,
            "PRIVATE_KEY": self.private_key,
        }
        missing = [k for k, v in required_vars.items() if not v]
        if missing:
            raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

        for label, addr in [("FROM_ADDRESS", self.from_address), ("TO_ADDRESS", self.to_address)]:
            if not Web3.is_address(addr):
                raise ValueError(f"{label} is invalid: {addr}")

        if self.private_key.startswith("0x"):
            logger.debug("Private key loaded. Ensure it's securely stored and never logged.")

    def _retry(self, func, *args, label: str = "", **kwargs):
        """Generic retry wrapper for network operations."""
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"{label} failed (attempt {attempt + 1}/{RETRY_ATTEMPTS}): {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_DELAY)
        raise RuntimeError(f"{label} failed after {RETRY_ATTEMPTS} attempts.")

    def get_gas_price(self) -> Dict[str, int]:
        """Return EIP-1559 gas settings or legacy gas price."""
        if self.custom_gas_price:
            try:
                gas_price = int(self.custom_gas_price)
                if gas_price <= 0:
                    raise ValueError
                logger.info(f"Using custom gas price: {gas_price} wei")
                return {"gasPrice": gas_price}
            except ValueError:
                logger.warning("Invalid DEFAULT_GAS_PRICE. Falling back to network price.")

        # Try EIP-1559 dynamic fee
        try:
            base_fee = self.web3.eth.fee_history(1, "latest")["baseFeePerGas"][-1]
            max_priority_fee = self.web3.to_wei("2", "gwei")
            max_fee = base_fee + max_priority_fee
            logger.info(f"Using dynamic fees: base={base_fee}, max={max_fee}")
            return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority_fee}
        except Exception:
            logger.debug("EIP-1559 not supported. Using legacy gas price.")

        # Fallback: legacy gas price
        gas_price = self._retry(self.web3.eth.gas_price, label="Fetch gas price")
        return {"gasPrice": gas_price}

    def estimate_gas_limit(self, from_addr: str, to_addr: str, value: int) -> int:
        """Estimate gas limit or return fallback."""
        try:
            return self.web3.eth.estimate_gas({"from": from_addr, "to": to_addr, "value": value})
        except Exception as e:
            logger.warning(f"Gas estimation failed: {e}. Using fallback {DEFAULT_GAS_LIMIT}.")
            return DEFAULT_GAS_LIMIT

    def get_nonce(self, address: str) -> int:
        """Get nonce with retry."""
        return self._retry(self.web3.eth.get_transaction_count, address, label="Get nonce")

    def build_transaction(self, value_wei: int, gas_params: Dict[str, int]) -> Dict[str, Any]:
        """Build transaction dictionary."""
        nonce = self.get_nonce(self.from_address)
        gas_limit = self.estimate_gas_limit(self.from_address, self.to_address, value_wei)

        tx = {
            "nonce": nonce,
            "to": self.to_address,
            "value": value_wei,
            "gas": gas_limit,
            "chainId": self.web3.eth.chain_id,
        }
        tx.update(gas_params)
        return tx

    def send_eth(self, amount_eth: float) -> str:
        """Send ETH and return tx hash."""
        value_wei = self.web3.to_wei(amount_eth, "ether")
        balance = self.web3.eth.get_balance(self.from_address)
        if balance < value_wei:
            raise ValueError(
                f"Insufficient funds. Required {amount_eth} ETH, "
                f"available {self.web3.from_wei(balance, 'ether')} ETH."
            )

        gas_params = self.get_gas_price()
        tx = self.build_transaction(value_wei, gas_params)

        try:
            signed_tx = self.web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
            tx_hex = self.web3.to_hex(tx_hash)
            logger.info(f"✅ Transaction submitted: {tx_hex}")
            return tx_hex
        except exceptions.InsufficientFunds:
            raise ValueError("Not enough ETH to cover gas fees.")
        except ValueError as e:
            if "nonce too low" in str(e).lower():
                logger.error("Nonce too low. Try resending with higher nonce.")
            raise
        except Exception as e:
            logger.exception(f"Transaction failed: {e}")
            raise

    def run(self) -> None:
        """Execute ETH transfer."""
        try:
            amount = float(self.transfer_amount) if self.transfer_amount else DEFAULT_AMOUNT_ETH
            if amount <= 0:
                raise ValueError("TRANSFER_AMOUNT must be positive.")

            logger.info(f"Initiating transfer of {amount:.6f} ETH...")
            tx_hash = self.send_eth(amount)
            logger.info(f"✅ Transfer complete: {tx_hash}")
        except Exception as e:
            logger.critical(f"❌ Execution failed: {e}", exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    EtherTransfer().run()
