import os
import logging
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3, exceptions
from web3.middleware import geth_poa_middleware

# Load environment variables from .env
load_dotenv()

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("EtherTransfer")

# Load environment variables
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
DEFAULT_GAS_PRICE = os.getenv("DEFAULT_GAS_PRICE")
TRANSFER_AMOUNT = os.getenv("TRANSFER_AMOUNT")

# Web3 setup
if not INFURA_URL:
    logger.critical("INFURA_URL is missing. Set it in your .env file.")
    raise EnvironmentError("Missing INFURA_URL.")

web3 = Web3(Web3.HTTPProvider(INFURA_URL))

# Add middleware for compatibility with some networks (e.g., Goerli, BSC)
web3.middleware_onion.inject(geth_poa_middleware, layer=0)

if not web3.is_connected():
    logger.critical("Web3 connection failed. Check your INFURA_URL and internet connection.")
    raise ConnectionError("Unable to connect to Ethereum network.")


def validate_ethereum_address(address: Optional[str], label: str) -> None:
    if not address or not Web3.is_address(address):
        raise ValueError(f"Invalid {label}: {address}")


def validate_env_vars() -> None:
    required = {
        "FROM_ADDRESS": FROM_ADDRESS,
        "TO_ADDRESS": TO_ADDRESS,
        "PRIVATE_KEY": PRIVATE_KEY,
    }
    missing = [key for key, val in required.items() if not val]
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")

    validate_ethereum_address(FROM_ADDRESS, "FROM_ADDRESS")
    validate_ethereum_address(TO_ADDRESS, "TO_ADDRESS")


def get_gas_price() -> int:
    if DEFAULT_GAS_PRICE:
        try:
            gas_price = int(DEFAULT_GAS_PRICE)
            logger.debug(f"Using custom gas price: {gas_price} wei")
            return gas_price
        except ValueError:
            logger.warning("DEFAULT_GAS_PRICE is invalid. Falling back to network gas price.")
    gas_price = web3.eth.gas_price
    logger.debug(f"Using network gas price: {gas_price} wei")
    return gas_price


def estimate_gas(from_addr: str, to_addr: str, value: int) -> int:
    try:
        gas = web3.eth.estimate_gas({
            'from': from_addr,
            'to': to_addr,
            'value': value
        })
        logger.debug(f"Estimated gas: {gas}")
        return gas
    except Exception as e:
        logger.warning(f"Gas estimation failed: {e}. Using default 21000.")
        return 21000


def send_ether(from_addr: str, to_addr: str, priv_key: str, amount_eth: float) -> str:
    value = web3.to_wei(amount_eth, 'ether')
    balance = web3.eth.get_balance(from_addr)

    if balance < value:
        available_eth = web3.from_wei(balance, 'ether')
        raise ValueError(f"Insufficient balance: {available_eth:.6f} ETH available, "
                         f"{amount_eth:.6f} ETH required.")

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
        signed_tx = web3.eth.account.sign_transaction(tx, priv_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hex = web3.to_hex(tx_hash)
        logger.info(f"Transaction sent. Hash: {tx_hex}")
        return tx_hex
    except exceptions.InsufficientFunds:
        logger.error("Not enough ETH to cover gas fees.")
        raise
    except ValueError as ve:
        logger.error(f"Web3 transaction error: {ve}")
        raise
    except Exception as e:
        logger.exception("Unexpected error during transaction.")
        raise


def main() -> None:
    try:
        validate_env_vars()
        amount = float(TRANSFER_AMOUNT) if TRANSFER_AMOUNT else 0.01
        logger.info(f"Transferring {amount} ETH from {FROM_ADDRESS} to {TO_ADDRESS}")
        tx_hash = send_ether(FROM_ADDRESS, TO_ADDRESS, PRIVATE_KEY, amount)
        logger.info(f"Transaction successful. Hash: {tx_hash}")
    except Exception as e:
        logger.critical(f"Transfer failed: {e}", exc_info=True)


if __name__ == "__main__":
    main()
