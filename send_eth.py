import os
import logging
import time
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from web3 import Web3, exceptions
from web3.middleware import geth_poa_middleware

# Load environment variables
load_dotenv()

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("EtherTransfer")

# Constants
DEFAULT_GAS_LIMIT: int = 21_000
DEFAULT_AMOUNT_ETH: float = 0.01
GAS_RETRY_ATTEMPTS: int = 3
GAS_RETRY_DELAY: int = 2  # seconds

# Environment variables
INFURA_URL: Optional[str] = os.getenv("INFURA_URL")
FROM_ADDRESS: Optional[str] = os.getenv("FROM_ADDRESS")
TO_ADDRESS: Optional[str] = os.getenv("TO_ADDRESS")
PRIVATE_KEY: Optional[str] = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE: Optional[str] = os.getenv("DEFAULT_GAS_PRICE")
TRANSFER_AMOUNT: Optional[str] = os.getenv("TRANSFER_AMOUNT")

# Validate connection URL
if not INFURA_URL:
    logger.critical("INFURA_URL is missing in environment variables.")
    raise EnvironmentError("INFURA_URL must be set in .env")

# Initialize Web3
web3 = Web3(Web3.HTTPProvider(INFURA_URL))
web3.middleware_onion.inject(geth_poa_middleware, layer=0)

if not web3.is_connected():
    logger.critical("Failed to connect to Ethereum network.")
    raise ConnectionError("Web3 provider connection failed.")


def validate_eth_address(address: Optional[str], label: str) -> None:
    """Validate Ethereum address format."""
    if not address or not Web3.is_address(address):
        raise ValueError(f"{label} is invalid or missing: {address}")


def check_required_env_vars() -> None:
    """Ensure required environment variables are present and valid."""
    required = {
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY,
    }
    missing = [name for name, val in required.items() if not val]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

    validate_eth_address(FROM_ADDRESS, "FROM_ADDRESS")
    validate_eth_address(TO_ADDRESS, "TO_ADDRESS")

    if PRIVATE_KEY and PRIVATE_KEY.startswith("0x"):
        logger.warning("PRIVATE_KEY loaded from environment. "
                       "Make sure this is not committed to version control.")


def get_gas_price() -> int:
    """Return gas price (custom or network with retries)."""
    if DEFAULT_GAS_PRICE:
        try:
            gas_price = int(DEFAULT_GAS_PRICE)
            logger.debug(f"Using custom gas price: {gas_price} wei")
            return gas_price
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE format. Falling back to network value.")

    for attempt in range(GAS_RETRY_ATTEMPTS):
        try:
            gas_price = web3.eth.gas_price
            logger.debug(f"Using network gas price: {gas_price} wei")
            return gas_price
        except Exception as e:
            logger.error(f"Failed to fetch gas price (attempt {attempt + 1}): {e}")
            if attempt < GAS_RETRY_ATTEMPTS - 1:
                time.sleep(GAS_RETRY_DELAY)
    raise RuntimeError("Failed to fetch gas price after multiple attempts.")


def estimate_gas_limit(from_addr: str, to_addr: str, value: int) -> int:
    """Estimate gas usage, fallback if it fails."""
    try:
        return web3.eth.estimate_gas({
            "from": from_addr,
            "to": to_addr,
            "value": value,
        })
    except Exception as e:
        logger.warning(f"Gas estimation failed: {e}. Using fallback {DEFAULT_GAS_LIMIT}.")
        return DEFAULT_GAS_LIMIT


def build_transaction(from_addr: str, to_addr: str, value_wei: int,
                      gas_price: int, gas_limit: int) -> Dict[str, Any]:
    """Build raw transaction dict."""
    return {
        "nonce": web3.eth.get_transaction_count(from_addr),
        "to": to_addr,
        "value": value_wei,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "chainId": web3.eth.chain_id,
    }


def send_eth(from_addr: str, to_addr: str, priv_key: str, amount_eth: float) -> str:
    """Send ETH and return transaction hash."""
    value_wei = web3.to_wei(amount_eth, "ether")
    balance = web3.eth.get_balance(from_addr)

    if balance < value_wei:
        raise ValueError(
            f"Insufficient funds. "
            f"Required: {amount_eth} ETH, Available: {web3.from_wei(balance, 'ether')} ETH"
        )

    gas_price = get_gas_price()
    gas_limit = estimate_gas_limit(from_addr, to_addr, value_wei)
    tx = build_transaction(from_addr, to_addr, value_wei, gas_price, gas_limit)

    try:
        signed_tx = web3.eth.account.sign_transaction(tx, priv_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hex = web3.to_hex(tx_hash)
        logger.info(f"Transaction submitted: {tx_hex}")
        return tx_hex
    except exceptions.InsufficientFunds:
        raise ValueError("Insufficient ETH to cover gas fees.")
    except Exception as e:
        logger.exception(f"Transaction failed: {e}")
        raise


def main() -> None:
    """Main entry point."""
    try:
        check_required_env_vars()
        amount = float(TRANSFER_AMOUNT) if TRANSFER_AMOUNT else DEFAULT_AMOUNT_ETH
        logger.info(f"Sending {amount:.6f} ETH from {FROM_ADDRESS} to {TO_ADDRESS}")
        tx_hash = send_eth(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount)  # type: ignore
        logger.info(f"Transaction successful: {tx_hash}")
    except Exception as e:
        logger.critical(f"Execution failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
