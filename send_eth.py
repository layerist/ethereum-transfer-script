import os
import logging
from web3 import Web3
from dotenv import load_dotenv
from typing import Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# Retrieve environment variables
INFURA_URL: Optional[str] = os.getenv("INFURA_URL")
FROM_ADDRESS: Optional[str] = os.getenv("FROM_ADDRESS")
TO_ADDRESS: Optional[str] = os.getenv("TO_ADDRESS")
PRIVATE_KEY: Optional[str] = os.getenv("PRIVATE_KEY")

def validate_env_vars():
    """Ensure all required environment variables are present and valid."""
    required_vars = {
        "INFURA_URL": INFURA_URL,
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY
    }
    
    missing_vars = [key for key, value in required_vars.items() if not value]
    if missing_vars:
        error_message = f"Missing environment variables: {', '.join(missing_vars)}"
        logger.critical(error_message)
        raise EnvironmentError(error_message)

# Validate environment variables
validate_env_vars()

# Connect to the Ethereum node
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

if not web3.isConnected():
    logger.critical("Failed to connect to Ethereum node. Check INFURA_URL.")
    raise ConnectionError("Failed to connect to Ethereum node")

def send_ether(from_address: str, to_address: str, private_key: str, amount_ether: float) -> str:
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
        value = web3.toWei(amount_ether, 'ether')

        # Check balance
        balance = web3.eth.get_balance(from_address)
        if balance < value:
            raise ValueError("Insufficient balance for the transaction.")

        # Get the current transaction nonce
        nonce = web3.eth.getTransactionCount(from_address)

        # Build the transaction
        tx = {
            'nonce': nonce,
            'to': to_address,
            'value': value,
            'gas': 21000,
            'gasPrice': web3.eth.gas_price
        }

        # Sign the transaction
        signed_tx = web3.eth.account.sign_transaction(tx, private_key)

        # Send the transaction and get the transaction hash
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)

        # Log the transaction hash
        tx_hash_hex = web3.toHex(tx_hash)
        logger.info(f"Transaction successful with hash: {tx_hash_hex}")
        return tx_hash_hex

    except ValueError as ve:
        logger.error(f"Value error: {ve}")
        raise
    except Exception as e:
        logger.error(f"Error while sending Ether: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    amount_to_send = 0.01  # Define the amount of Ether to be sent
    try:
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount_to_send)
        logger.info(f"Transaction hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"Script terminated due to error: {e}", exc_info=True)
