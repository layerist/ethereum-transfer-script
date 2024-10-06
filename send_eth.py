import os
import logging
from web3 import Web3
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# Retrieve environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# Function to validate environment variables
def validate_env_vars():
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
    
# Validate the environment variables
validate_env_vars()

# Connect to the Ethereum node
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

if not web3.isConnected():
    logger.critical("Failed to connect to Ethereum node")
    raise ConnectionError("Failed to connect to Ethereum node")

def send_ether(from_address, to_address, private_key, amount_ether):
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

        # Get the current transaction nonce
        nonce = web3.eth.getTransactionCount(from_address)

        # Build the transaction
        tx = {
            'nonce': nonce,
            'to': to_address,
            'value': value,
            'gas': 21000,
            'gasPrice': web3.eth.gasPrice
        }

        # Sign the transaction
        signed_tx = web3.eth.account.sign_transaction(tx, private_key)

        # Send the transaction and get the transaction hash
        tx_hash = web3.eth.sendRawTransaction(signed_tx.rawTransaction)

        # Log the transaction hash
        logger.info(f"Transaction successful with hash: {web3.toHex(tx_hash)}")
        return web3.toHex(tx_hash)

    except Exception as e:
        logger.error(f"Error while sending Ether: {e}")
        raise

if __name__ == "__main__":
    try:
        # Amount of Ether to be sent
        amount_to_send = 0.01

        # Send Ether and log the transaction hash
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount_to_send)
        logger.info(f"Transaction hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"Script terminated due to error: {e}")
