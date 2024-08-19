import os
import logging
from web3 import Web3
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# Retrieve environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# Verify that all necessary environment variables are set
required_env_vars = {
    "INFURA_URL": INFURA_URL,
    "FROM_ADDRESS": FROM_ADDRESS,
    "TO_ADDRESS": TO_ADDRESS,
    "PRIVATE_KEY": PRIVATE_KEY
}

missing_vars = [key for key, value in required_env_vars.items() if not value]
if missing_vars:
    raise EnvironmentError(f"Missing environment variables: {', '.join(missing_vars)}")

# Connect to the Ethereum node
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

# Check connection
if not web3.isConnected():
    raise ConnectionError("Failed to connect to Ethereum node")

# Amount to send (in Ether)
amount = 0.01

# Convert Ether to Wei
value = web3.toWei(amount, 'ether')

try:
    # Get the nonce (number of transactions sent from the address)
    nonce = web3.eth.getTransactionCount(FROM_ADDRESS)

    # Build the transaction
    tx = {
        'nonce': nonce,
        'to': TO_ADDRESS,
        'value': value,
        'gas': 21000,
        'gasPrice': web3.eth.gasPrice
    }

    # Sign the transaction
    signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)

    # Send the transaction
    tx_hash = web3.eth.sendRawTransaction(signed_tx.rawTransaction)

    # Log the transaction hash
    logger.info(f"Transaction successful with hash: {web3.toHex(tx_hash)}")

except Exception as e:
    logger.error(f"An error occurred: {e}")
    raise
