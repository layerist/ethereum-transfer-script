import os
import logging
from web3 import Web3
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения из файла .env
load_dotenv()

# Получение переменных окружения
INFURA_URL = os.getenv("INFURA_URL")
FROM_ADDRESS = os.getenv("FROM_ADDRESS")
TO_ADDRESS = os.getenv("TO_ADDRESS")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# Проверка наличия всех необходимых переменных окружения
required_env_vars = {
    "INFURA_URL": INFURA_URL,
    "FROM_ADDRESS": FROM_ADDRESS,
    "TO_ADDRESS": TO_ADDRESS,
    "PRIVATE_KEY": PRIVATE_KEY
}

missing_vars = [key for key, value in required_env_vars.items() if not value]

if missing_vars:
    raise ValueError(f"Отсутствуют следующие переменные окружения: {', '.join(missing_vars)}")

# Подключение к Ethereum узлу
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

# Проверка подключения
if not web3.isConnected():
    raise ConnectionError("Не удалось подключиться к Ethereum узлу")

# Сумма для отправки (в эфирах)
amount = 0.01

# Конвертация эфира в вей
value = web3.toWei(amount, 'ether')

try:
    # Получение nonce (число транзакций, отправленных с адреса)
    nonce = web3.eth.getTransactionCount(FROM_ADDRESS)

    # Построение транзакции
    tx = {
        'nonce': nonce,
        'to': TO_ADDRESS,
        'value': value,
        'gas': 21000,
        'gasPrice': web3.toWei('50', 'gwei')
    }

    # Подписание транзакции
    signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)

    # Отправка транзакции
    tx_hash = web3.eth.sendRawTransaction(signed_tx.rawTransaction)

    # Вывод хеша транзакции
    logger.info(f"Транзакция успешна с хешем: {web3.toHex(tx_hash)}")

except Exception as e:
    logger.error(f"Произошла ошибка: {e}")
