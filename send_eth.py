import os
import logging
from web3 import Web3
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
INFURA_URL: Optional[str] = os.getenv("INFURA_URL")
FROM_ADDRESS: Optional[str] = os.getenv("FROM_ADDRESS")
TO_ADDRESS: Optional[str] = os.getenv("TO_ADDRESS")
PRIVATE_KEY: Optional[str] = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE: Optional[int] = int(os.getenv("DEFAULT_GAS_PRICE", 0))


def validate_ethereum_address(address: str) -> None:
    """Validate an Ethereum address."""
    if not Web3.isAddress(address):
        raise ValueError(f"Invalid Ethereum address format: {address}")


def validate_env_vars() -> None:
    """Ensure all required environment variables are present and valid."""
    required_vars = {
        "INFURA_URL": INFURA_URL,
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY,
    }

    missing_vars = [key for key, value in required_vars.items() if not value]
    if missing_vars:
        error_message = f"Missing required environment variables: {', '.join(missing_vars)}"
        logger.critical(error_message)
        raise EnvironmentError(error_message)

    # Validate Ethereum addresses
    validate_ethereum_address(FROM_ADDRESS)
    validate_ethereum_address(TO_ADDRESS)


# Validate environment variables
validate_env_vars()

# Connect to the Ethereum node
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

if not web3.isConnected():
    logger.critical("Failed to connect to the Ethereum node. Check INFURA_URL.")
    raise ConnectionError("Failed to connect to the Ethereum node.")


def send_ether(
    from_address: str,
    to_address: str,
    private_key: str,
    amount_ether: float
) -> str:
    """Send Ether from one address to another.

    Args:
        from_address (str): The sender's Ethereum address.
        to_address (str): The receiver's Ethereum address.
        private_key (str): The private key for the sender's address.
        amount_ether (float): Amount of Ether to send.

    Returns:
        str: Transaction hash of the sent Ether.
    """
    try:
        # Convert Ether to Wei
        value = web3.toWei(amount_ether, "ether")

        # Check sender's balance
        balance = web3.eth.get_balance(from_address)
        if balance < value:
            logger.error("Insufficient balance for the transaction.")
            raise ValueError("Insufficient balance for the transaction.")

        # Get the current transaction nonce
        nonce = web3.eth.get_transaction_count(from_address)

        # Determine gas price
        gas_price = DEFAULT_GAS_PRICE or web3.eth.gas_price

        # Build the transaction
        tx = {
            "nonce": nonce,
            "to": to_address,
            "value": value,
            "gas": 21000,
            "gasPrice": gas_price,
        }

        # Sign the transaction
        signed_tx = web3.eth.account.sign_transaction(tx, private_key)

        # Send the transaction and get the transaction hash
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)

        # Log the transaction hash
        tx_hash_hex = web3.to_hex(tx_hash)
        logger.info(f"Transaction successful. Hash: {tx_hash_hex}")
        return tx_hash_hex

    except ValueError as ve:
        logger.error(f"Value error: {ve}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while sending Ether: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    try:
        amount_to_send = 0.01  # Define the amount of Ether to send
        logger.info(
            f"Starting Ether transfer: {amount_to_send} ETH from {FROM_ADDRESS} to {TO_ADDRESS}."
        )
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount_to_send)
        logger.info(f"Transaction completed. Hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"Script terminated due to error: {e}", exc_info=True)
