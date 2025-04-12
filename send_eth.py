import os
import logging
from web3 import Web3, exceptions
from dotenv import load_dotenv
from typing import Optional

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("EtherTransfer")

# Environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE = os.getenv("DEFAULT_GAS_PRICE")

# Validate Infura URL and initialize Web3
if not INFURA_URL:
    logger.critical("INFURA_URL is missing in environment variables.")
    raise EnvironmentError("INFURA_URL is required.")

web3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not web3.is_connected():
    logger.critical("Unable to connect to Ethereum network. Check your INFURA_URL.")
    raise ConnectionError("Failed to connect to Ethereum node.")


def validate_ethereum_address(address: Optional[str]) -> None:
    """Validate the given Ethereum address."""
    if not address or not Web3.is_address(address):
        raise ValueError(f"Invalid Ethereum address: {address}")


def get_gas_price() -> int:
    """Return gas price from env or fetch current network price."""
    if DEFAULT_GAS_PRICE:
        try:
            gas_price = int(DEFAULT_GAS_PRICE)
            logger.debug(f"Using default gas price from .env: {gas_price}")
            return gas_price
        except ValueError:
            logger.warning("DEFAULT_GAS_PRICE is invalid. Falling back to network gas price.")
    return web3.eth.gas_price


def validate_env_vars() -> None:
    """Ensure all critical environment variables are present and valid."""
    required = {
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY,
    }

    missing = [key for key, val in required.items() if not val]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

    validate_ethereum_address(FROM_ADDRESS)
    validate_ethereum_address(TO_ADDRESS)


def send_ether(from_addr: str, to_addr: str, priv_key: str, amount_eth: float) -> str:
    """Transfer Ether between accounts."""
    value = web3.to_wei(amount_eth, "ether")
    balance = web3.eth.get_balance(from_addr)

    if balance < value:
        raise ValueError("Insufficient balance for transaction.")

    nonce = web3.eth.get_transaction_count(from_addr)
    gas_price = get_gas_price()

    try:
        estimated_gas = web3.eth.estimate_gas({
            "from": from_addr,
            "to": to_addr,
            "value": value,
        })
    except Exception as e:
        logger.warning(f"Gas estimation failed: {e}. Using fallback gas limit of 21000.")
        estimated_gas = 21000  # Standard for ETH transfers

    tx = {
        "nonce": nonce,
        "to": to_addr,
        "value": value,
        "gas": estimated_gas,
        "gasPrice": gas_price,
        "chainId": web3.eth.chain_id,
    }

    try:
        signed_tx = web3.eth.account.sign_transaction(tx, priv_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hex = web3.to_hex(tx_hash)
        logger.info(f"Transaction sent. Hash: {tx_hex}")
        return tx_hex
    except exceptions.InsufficientFunds:
        logger.error("Insufficient funds for gas.")
        raise
    except exceptions.TransactionNotFound:
        logger.error("Transaction not found after broadcasting.")
        raise
    except ValueError as ve:
        logger.error(f"Value error during transaction: {ve}")
        raise
    except Exception as e:
        logger.error("Unexpected error during transaction.", exc_info=True)
        raise


def main():
    """Entry point for ETH transfer."""
    try:
        validate_env_vars()
        amount = 0.01
        logger.info(f"Sending {amount} ETH from {FROM_ADDRESS} to {TO_ADDRESS}...")
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount)
        logger.info(f"Transfer successful. Transaction hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"Transaction failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
