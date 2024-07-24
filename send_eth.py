import os
from web3 import Web3
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Get environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# Validate environment variables
if not INFURA_URL or not FROM_ADDRESS or not TO_ADDRESS or not PRIVATE_KEY:
    raise ValueError("One or more environment variables are missing")

# Connect to Ethereum node
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

# Check if connected
if not web3.isConnected():
    raise ConnectionError("Failed to connect to Ethereum node")

# Amount to send (in ether)
amount = 0.01

# Convert ether to wei
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
        'gasPrice': web3.toWei('50', 'gwei')
    }

    # Sign the transaction
    signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)

    # Send the transaction
    tx_hash = web3.eth.sendRawTransaction(signed_tx.rawTransaction)

    # Get transaction hash
    print(f"Transaction successful with hash: {web3.toHex(tx_hash)}")

except Exception as e:
    print(f"An error occurred: {e}")
