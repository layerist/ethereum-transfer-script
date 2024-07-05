from web3 import Web3

# Connect to Ethereum node
infura_url = "https://mainnet.infura.io/v3/YOUR_INFURA_PROJECT_ID"
web3 = Web3(Web3.HTTPProvider(infura_url))

# Check if connected
if not web3.isConnected():
    raise Exception("Failed to connect to Ethereum node")

# Wallet addresses
from_address = "YOUR_FROM_ADDRESS"
to_address = "YOUR_TO_ADDRESS"

# Private key of the sender (keep this secure and never share it)
private_key = "YOUR_PRIVATE_KEY"

# Amount to send (in ether)
amount = 0.01

# Convert ether to wei
value = web3.toWei(amount, 'ether')

# Get the nonce (number of transactions sent from the address)
nonce = web3.eth.getTransactionCount(from_address)

# Build the transaction
tx = {
    'nonce': nonce,
    'to': to_address,
    'value': value,
    'gas': 21000,
    'gasPrice': web3.toWei('50', 'gwei')
}

# Sign the transaction
signed_tx = web3.eth.account.sign_transaction(tx, private_key)

# Send the transaction
tx_hash = web3.eth.sendRawTransaction(signed_tx.rawTransaction)

# Get transaction hash
print(f"Transaction successful with hash: {web3.toHex(tx_hash)}")
