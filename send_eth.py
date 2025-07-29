import os
import logging
from typing import Optional
from dotenv import load_dotenv
from web3 import Web3, exceptions
from web3.middleware import geth_poa_middleware

# Load environment variables from .env file
load_dotenv()

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("EtherTransfer")

# Constants
DEFAULT_GAS_LIMIT = 21_000

# Environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE = os.getenv("DEFAULT_GAS_PRICE")
TRANSFER_AMOUNT = os.getenv("TRANSFER_AMOUNT")

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
    """Check if the given Ethereum address is valid."""
    if not address or not Web3.is_address(address):
        raise ValueError(f"{label} is invalid: {address}")


def check_required_env_vars() -> None:
    """Ensure all required environment variables are present and valid."""
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


def get_gas_price() -> int:
    """Return gas price from environment or fetch from network."""
    if DEFAULT_GAS_PRICE:
        try:
            gas_price = int(DEFAULT_GAS_PRICE)
            logger.debug(f"Using custom gas price: {gas_price} wei")
            return gas_price
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE format. Falling back to network value.")

    try:
        gas_price = web3.eth.gas_price
        logger.debug(f"Using network gas price: {gas_price} wei")
        return gas_price
    except Exception as e:
        logger.error(f"Failed to fetch gas price from network: {e}")
        raise


def estimate_gas_limit(from_addr: str, to_addr: str, value: int) -> int:
    """Estimate gas usage or return fallback."""
    try:
        estimated = web3.eth.estimate_gas({
            'from': from_addr,
            'to': to_addr,
            'value': value,
        })
        logger.debug(f"Estimated gas: {estimated}")
        return estimated
    except Exception as e:
        logger.warning(f"Gas estimation failed: {e}. Using fallback {DEFAULT_GAS_LIMIT}.")
        return DEFAULT_GAS_LIMIT


def send_eth(from_addr: str, to_addr: str, priv_key: str, amount_eth: float) -> str:
    """Send ETH and return transaction hash."""
    value_wei = web3.to_wei(amount_eth, 'ether')
    balance = web3.eth.get_balance(from_addr)

    if balance < value_wei:
        raise ValueError(f"Insufficient funds. Required: {amount_eth} ETH, Available: {web3.from_wei(balance, 'ether')} ETH")

    nonce = web3.eth.get_transaction_count(from_addr)
    gas_price = get_gas_price()
    gas_limit = estimate_gas_limit(from_addr, to_addr, value_wei)

    tx = {
        'nonce': nonce,
        'to': to_addr,
        'value': value_wei,
        'gas': gas_limit,
        'gasPrice': gas_price,
        'chainId': web3.eth.chain_id
    }

    try:
        signed_tx = web3.eth.account.sign_transaction(tx, priv_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hex = web3.to_hex(tx_hash)
        logger.info(f"Transaction submitted successfully: {tx_hex}")
        return tx_hex
    except exceptions.InsufficientFunds:
        logger.error("Insufficient ETH to cover gas cost.")
        raise
    except Exception as e:
        logger.exception(f"Transaction submission failed: {e}")
        raise


def main() -> None:
    """Main program entrypoint."""
    try:
        check_required_env_vars()
        amount = float(TRANSFER_AMOUNT) if TRANSFER_AMOUNT else 0.01
        logger.info(f"Transferring {amount:.6f} ETH from {FROM_ADDRESS} to {TO_ADDRESS}")
        tx_hash = send_eth(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount)
        logger.info(f"Transaction completed: {tx_hash}")
    except Exception as e:
        logger.critical(f"Execution failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
