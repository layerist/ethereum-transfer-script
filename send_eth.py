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
TRANSFER_AMOUNT = os.getenv("TRANSFER_AMOUNT")  # Optional: can override hardcoded value

# Validate Infura URL and initialize Web3
if not INFURA_URL:
    logger.critical("INFURA_URL is missing in environment variables.")
    raise EnvironmentError("INFURA_URL is required.")

web3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not web3.is_connected():
    logger.critical("Unable to connect to Ethereum network. Check your INFURA_URL.")
    raise ConnectionError("Failed to connect to Ethereum node.")


def validate_ethereum_address(address: Optional[str]) -> None:
    if not address or not Web3.is_address(address):
        raise ValueError(f"Invalid Ethereum address: {address}")


def get_gas_price() -> int:
    """Get the gas price, either from environment or network."""
    if DEFAULT_GAS_PRICE:
        try:
            gas_price = int(DEFAULT_GAS_PRICE)
            logger.debug(f"Using default gas price: {gas_price} wei")
            return gas_price
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE in .env. Using network gas price.")
    gas_price = web3.eth.gas_price
    logger.debug(f"Using network gas price: {gas_price} wei")
    return gas_price


def validate_env_vars() -> None:
    """Ensure all required environment variables are set and valid."""
    required_vars = {
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY,
    }
    missing = [key for key, val in required_vars.items() if not val]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

    validate_ethereum_address(FROM_ADDRESS)
    validate_ethereum_address(TO_ADDRESS)


def send_ether(from_addr: str, to_addr: str, priv_key: str, amount_eth: float) -> str:
    """Transfer ETH from one address to another."""
    value = web3.to_wei(amount_eth, "ether")
    balance = web3.eth.get_balance(from_addr)

    if balance < value:
        raise ValueError(f"Insufficient balance: {web3.from_wei(balance, 'ether')} ETH available, {amount_eth} ETH needed.")

    nonce = web3.eth.get_transaction_count(from_addr)
    gas_price = get_gas_price()

    try:
        estimated_gas = web3.eth.estimate_gas({
            "from": from_addr,
            "to": to_addr,
            "value": value,
        })
    except Exception as e:
        logger.warning(f"Gas estimation failed: {e}. Falling back to 21000 gas.")
        estimated_gas = 21000  # Standard for ETH transfer

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
        logger.info(f"Transaction successful. Hash: {tx_hex}")
        return tx_hex
    except exceptions.InsufficientFunds:
        logger.error("Not enough ETH to cover gas fees.")
        raise
    except exceptions.TransactionNotFound:
        logger.error("Transaction not found after sending.")
        raise
    except ValueError as ve:
        logger.error(f"Transaction error: {ve}")
        raise
    except Exception as e:
        logger.exception("Unexpected error during ETH transfer.")
        raise


def main():
    """Main entry point for ETH transfer script."""
    try:
        validate_env_vars()
        amount = float(TRANSFER_AMOUNT) if TRANSFER_AMOUNT else 0.01
        logger.info(f"Transferring {amount} ETH from {FROM_ADDRESS} to {TO_ADDRESS}...")
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount)
        logger.info(f"Transfer complete. Transaction hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"ETH transfer failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
