import os
import logging
from typing import Optional
from dotenv import load_dotenv
from web3 import Web3, exceptions
from web3.middleware import geth_poa_middleware

# Load .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("EtherTransfer")

# Load environment variables
INFURA_URL: Optional[str] = os.getenv("INFURA_URL")
FROM_ADDRESS: Optional[str] = os.getenv("FROM_ADDRESS")
TO_ADDRESS: Optional[str] = os.getenv("TO_ADDRESS")
PRIVATE_KEY: Optional[str] = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE: Optional[str] = os.getenv("DEFAULT_GAS_PRICE")
TRANSFER_AMOUNT: Optional[str] = os.getenv("TRANSFER_AMOUNT")

if not INFURA_URL:
    logger.critical("Missing INFURA_URL in environment. Please check your .env file.")
    raise EnvironmentError("INFURA_URL not set.")

# Setup Web3
web3 = Web3(Web3.HTTPProvider(INFURA_URL))
web3.middleware_onion.inject(geth_poa_middleware, layer=0)

if not web3.is_connected():
    logger.critical("Web3 connection failed. Please verify your INFURA_URL and internet connection.")
    raise ConnectionError("Could not connect to Ethereum network.")


def validate_ethereum_address(address: Optional[str], label: str) -> None:
    """
    Validate that a given address is a proper Ethereum address.
    """
    if not address or not Web3.is_address(address):
        raise ValueError(f"Invalid {label}: {address}")


def validate_env_vars() -> None:
    """
    Validate presence and correctness of required environment variables.
    """
    required_vars = {
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY,
    }
    missing_vars = [k for k, v in required_vars.items() if not v]
    if missing_vars:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

    validate_ethereum_address(FROM_ADDRESS, "FROM_ADDRESS")
    validate_ethereum_address(TO_ADDRESS, "TO_ADDRESS")


def get_gas_price() -> int:
    """
    Get the gas price either from environment or from network.
    """
    if DEFAULT_GAS_PRICE:
        try:
            gas_price = int(DEFAULT_GAS_PRICE)
            logger.debug(f"Using custom gas price: {gas_price} wei")
            return gas_price
        except ValueError:
            logger.warning("Invalid DEFAULT_GAS_PRICE in .env, falling back to network gas price.")

    gas_price = web3.eth.gas_price
    logger.debug(f"Using network gas price: {gas_price} wei")
    return gas_price


def estimate_gas(from_addr: str, to_addr: str, value: int) -> int:
    """
    Estimate gas for a transaction.
    """
    try:
        gas_estimate = web3.eth.estimate_gas({
            'from': from_addr,
            'to': to_addr,
            'value': value,
        })
        logger.debug(f"Estimated gas: {gas_estimate}")
        return gas_estimate
    except Exception as e:
        logger.warning(f"Gas estimation failed: {e}, using default 21000.")
        return 21000


def send_ether(from_addr: str, to_addr: str, private_key: str, amount_eth: float) -> str:
    """
    Send Ether between accounts.
    """
    value = web3.to_wei(amount_eth, 'ether')
    balance = web3.eth.get_balance(from_addr)

    if balance < value:
        available = web3.from_wei(balance, 'ether')
        raise ValueError(
            f"Insufficient balance: {available:.6f} ETH available, "
            f"{amount_eth:.6f} ETH required."
        )

    nonce = web3.eth.get_transaction_count(from_addr)
    gas_price = get_gas_price()
    gas_limit = estimate_gas(from_addr, to_addr, value)

    tx = {
        'nonce': nonce,
        'to': to_addr,
        'value': value,
        'gas': gas_limit,
        'gasPrice': gas_price,
        'chainId': web3.eth.chain_id
    }

    try:
        signed_tx = web3.eth.account.sign_transaction(tx, private_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hex = web3.to_hex(tx_hash)
        logger.info(f"Transaction submitted: {tx_hex}")
        return tx_hex
    except exceptions.InsufficientFunds:
        logger.error("Not enough ETH to pay for gas.")
        raise
    except Exception as e:
        logger.exception(f"Transaction failed: {e}")
        raise


def main() -> None:
    """
    Main execution flow.
    """
    try:
        validate_env_vars()
        amount = float(TRANSFER_AMOUNT) if TRANSFER_AMOUNT else 0.01
        logger.info(f"Initiating transfer of {amount} ETH from {FROM_ADDRESS} to {TO_ADDRESS}")
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount)
        logger.info(f"Transaction completed successfully: {tx_hash}")
    except Exception as e:
        logger.critical(f"Transfer failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
