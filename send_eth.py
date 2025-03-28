import os
import logging
from web3 import Web3, exceptions
from dotenv import load_dotenv
from typing import Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("EtherTransfer")

# Load environment variables
load_dotenv()

# Retrieve environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE = os.getenv("DEFAULT_GAS_PRICE")

# Validate and initialize Web3 connection
if not INFURA_URL:
    logger.critical("INFURA_URL is missing from environment variables.")
    raise EnvironmentError("INFURA_URL is required.")

web3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not web3.is_connected():
    logger.critical("Failed to connect to the Ethereum network. Check INFURA_URL.")
    raise ConnectionError("Ethereum node connection failed.")


def validate_ethereum_address(address: Optional[str]) -> None:
    """Validate an Ethereum address."""
    if not address or not Web3.is_address(address):
        raise ValueError(f"Invalid Ethereum address: {address}")


def get_gas_price() -> int:
    """Retrieve the gas price, either from environment or network."""
    if DEFAULT_GAS_PRICE:
        try:
            return int(DEFAULT_GAS_PRICE)
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE. Using network gas price.")
    return web3.eth.gas_price


def validate_env_vars() -> None:
    """Ensure all required environment variables are present and valid."""
    required_vars = {
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY,
    }

    missing_vars = [key for key, value in required_vars.items() if not value]
    if missing_vars:
        error_message = f"Missing required environment variables: {', '.join(missing_vars)}"
        logger.critical(error_message)
        raise EnvironmentError(error_message)

    validate_ethereum_address(FROM_ADDRESS)
    validate_ethereum_address(TO_ADDRESS)


validate_env_vars()


def send_ether(from_address: str, to_address: str, private_key: str, amount_ether: float) -> str:
    """Send Ether from one address to another."""
    try:
        value = web3.to_wei(amount_ether, "ether")
        balance = web3.eth.get_balance(from_address)
        if balance < value:
            raise ValueError("Insufficient balance for transaction.")

        nonce = web3.eth.get_transaction_count(from_address)
        gas_price = get_gas_price()
        estimated_gas = web3.eth.estimate_gas({
            "from": from_address,
            "to": to_address,
            "value": value,
        })

        tx = {
            "nonce": nonce,
            "to": to_address,
            "value": value,
            "gas": estimated_gas,
            "gasPrice": gas_price,
            "chainId": web3.eth.chain_id,
        }

        signed_tx = web3.eth.account.sign_transaction(tx, private_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hash_hex = web3.to_hex(tx_hash)

        logger.info(f"Transaction successful. Hash: {tx_hash_hex}")
        return tx_hash_hex
    except exceptions.InsufficientFunds:
        logger.error("Insufficient funds for transaction.")
        raise
    except exceptions.TransactionNotFound:
        logger.error("Transaction not found. Check the transaction hash.")
        raise
    except ValueError as ve:
        logger.error(f"Value error: {ve}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while sending Ether: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        amount_to_send = 0.01
        logger.info(f"Initiating Ether transfer: {amount_to_send} ETH from {FROM_ADDRESS} to {TO_ADDRESS}.")
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount_to_send)
        logger.info(f"Transaction completed successfully. Hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"Script terminated due to an error: {e}", exc_info=True)
