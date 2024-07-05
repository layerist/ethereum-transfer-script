# Ethereum Transfer Script

This script allows you to send Ethereum from one wallet to another using the `web3.py` library.

## Prerequisites

- Python 3.x
- `web3` library
- An Infura account and project ID
- Ethereum wallet addresses and the private key of the sender

## Installation

1. Clone the repository:
   ```sh
   git clone https://github.com/layerist/eth-transfer-script.git
   cd eth-transfer-script
   ```

2. Install the required Python packages:
   ```sh
   pip install web3
   ```

## Configuration

1. Replace the placeholders in the script with your actual values:
   - `YOUR_INFURA_PROJECT_ID`: Your Infura project ID.
   - `YOUR_FROM_ADDRESS`: The Ethereum address you are sending from.
   - `YOUR_TO_ADDRESS`: The Ethereum address you are sending to.
   - `YOUR_PRIVATE_KEY`: The private key of the sender's wallet.

   ```python
   infura_url = "https://mainnet.infura.io/v3/YOUR_INFURA_PROJECT_ID"
   from_address = "YOUR_FROM_ADDRESS"
   to_address = "YOUR_TO_ADDRESS"
   private_key = "YOUR_PRIVATE_KEY"
   ```

2. Set the amount of Ethereum you want to send by modifying the `amount` variable:
   ```python
   amount = 0.01  # Amount in ether
   ```

## Usage

Run the script:
```sh
python send_eth.py
```

If the transaction is successful, the script will output the transaction hash.

## Security Note

- Keep your private key secure and never share it.
- Consider using environment variables or a secure vault to store sensitive information.

## License

This project is licensed under the MIT License.

Replace `YOUR_INFURA_PROJECT_ID`, `YOUR_FROM_ADDRESS`, `YOUR_TO_ADDRESS`, and `YOUR_PRIVATE_KEY` with your actual details before running the script. 
