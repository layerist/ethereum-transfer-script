import os
import logging
from web3 import Web3, exceptions
from dotenv import load_dotenv
from typing import Optional

# Load environment variables from .env
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("EtherTransfer")

# Load environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE = os.getenv("DEFAULT_GAS_PRICE")
TRANSFER_AMOUNT = os.getenv("TRANSFER_AMOUNT")  # Optional: can override hardcoded amount

# Initialize Web3
if not INFURA_URL:
    logger.critical("INFURA_URL is missing. Please provide it in the environment variables.")
    raise EnvironmentError("Missing INFURA_URL.")

web3 = Web3(Web3.HTTPProvider(INFURA_URL))

if not web3.is_connected():
    logger.critical("Failed to connect to Ethereum network. Check INFURA_URL or network connectivity.")
    raise ConnectionError("Web3 connection failed.")

def validate_ethereum_address(address: Optional[str], label: str) -> None:
    """Validate an Ethereum address."""
    if not address or not Web3.is_address(address):
        raise ValueError(f"Invalid {label} address: {address}")

def get_gas_price() -> int:
    """Get gas price from environment or fetch from network."""
    if DEFAULT_GAS_PRICE:
        try:
            gas_price = int(DEFAULT_GAS_PRICE)
            logger.debug(f"Using provided DEFAULT_GAS_PRICE: {gas_price} wei")
            return gas_price
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE in .env, using network gas price.")
    gas_price = web3.eth.gas_price
    logger.debug(f"Using network gas price: {gas_price} wei")
    return gas_price

def validate_env_vars() -> None:
    """Ensure all required environment variables are valid."""
    required = {"FROM_ADDRESS": FROM_ADDRESS, "TO_ADDRESS": TO_ADDRESS, "PRIVATE_KEY": PRIVATE_KEY}
    missing = [key for key, val in required.items() if not val]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")
    validate_ethereum_address(FROM_ADDRESS, "FROM_ADDRESS")
    validate_ethereum_address(TO_ADDRESS, "TO_ADDRESS")

def send_ether(from_addr: str, to_addr: str, priv_key: str, amount_eth: float) -> str:
    """Send ETH from from_addr to to_addr."""
    value = web3.to_wei(amount_eth, 'ether')
    balance = web3.eth.get_balance(from_addr)
    if balance < value:
        available_eth = web3.from_wei(balance, 'ether')
        raise ValueError(f"Insufficient balance: {available_eth} ETH available, {amount_eth} ETH required.")

    nonce = web3.eth.get_transaction_count(from_addr)
    gas_price = get_gas_price()

    try:
        estimated_gas = web3.eth.estimate_gas({
            'from': from_addr,
            'to': to_addr,
            'value': value
        })
        logger.debug(f"Estimated gas: {estimated_gas}")
    except Exception as e:
        logger.warning(f"Gas estimation failed ({e}), falling back to 21000 (standard for ETH transfer).")
        estimated_gas = 21000

    tx = {
        'nonce': nonce,
        'to': to_addr,
        'value': value,
        'gas': estimated_gas,
        'gasPrice': gas_price,
        'chainId': web3.eth.chain_id
    }

    try:
        signed_tx = web3.eth.account.sign_transaction(tx, priv_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hex = web3.to_hex(tx_hash)
        logger.info(f"Transaction sent successfully. Hash: {tx_hex}")
        return tx_hex
    except exceptions.InsufficientFunds:
        logger.error("Not enough ETH to cover transfer amount and gas fees.")
        raise
    except ValueError as ve:
        logger.error(f"Transaction failed: {ve}")
        raise
    except Exception as e:
        logger.exception("Unexpected error occurred during transaction.")
        raise

def main():
    """Main function for transferring ETH."""
    try:
        validate_env_vars()
        amount = float(TRANSFER_AMOUNT) if TRANSFER_AMOUNT else 0.01
        logger.info(f"Initiating transfer of {amount} ETH from {FROM_ADDRESS} to {TO_ADDRESS}.")
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount)
        logger.info(f"Transfer successful. Transaction hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"Transfer failed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
