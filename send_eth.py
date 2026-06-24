#!/usr/bin/env python3
"""
Ultra-Reliable ETH Sweeper v5 — Single Wallet Safe Edition

Purpose:
- Sweep ETH/native coin from ONE controlled wallet to ONE destination.
- No multi-wallet sweeping.
- No private keys in source code.
- Dry-run by default.

Key features:
- Multi-RPC failover with health/cooldown
- Optional RPC proxy with fail-closed mode
- Strict address/private-key/chain validation
- Persistent pending tx state
- Safe resume after restart
- Rebroadcast signed raw tx
- EIP-1559 dynamic fees + replacement bump
- Legacy gasPrice fallback for non-London chains
- Unknown pending nonce protection
- Graceful shutdown
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import time

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3, HTTPProvider
from web3.exceptions import TransactionNotFound

try:
    from web3.middleware import ExtraDataToPOAMiddleware
except Exception:  # web3 version compatibility
    ExtraDataToPOAMiddleware = None


# ============================================================
# LOGGING / SHUTDOWN
# ============================================================

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("ETH_SWEEPER")

SHUTDOWN = False


def shutdown_handler(*_: Any) -> None:
    global SHUTDOWN
    SHUTDOWN = True
    log.warning("Shutdown requested...")


signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# ============================================================
# HELPERS
# ============================================================

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WEI_IN_ETH = Decimal("1000000000000000000")


class ConfigError(RuntimeError):
    pass


class NoSweepableBalance(RuntimeError):
    pass


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int_env(name: str, default: int, min_value: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be integer") from exc

    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}")

    return value


def parse_decimal_env(name: str, default: str, min_value: Optional[str] = None) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be Decimal") from exc

    if min_value is not None and value < Decimal(min_value):
        raise ConfigError(f"{name} must be >= {min_value}")

    return value


def gwei_to_wei(value: Decimal) -> int:
    return int(value * Decimal("1000000000"))


def wei_to_eth(value: int) -> str:
    return f"{Decimal(value) / WEI_IN_ETH:.18f}".rstrip("0").rstrip(".")


def redact_url(url: str) -> str:
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        if p.username or p.password:
            host = f"***:***@{host}"
        return urlunsplit((p.scheme, host, p.path, p.query, p.fragment))
    except Exception:
        return "<redacted-rpc-url>"


def hex_to_bytes(raw_hex: str) -> bytes:
    raw_hex = raw_hex.strip()
    if raw_hex.startswith("0x"):
        raw_hex = raw_hex[2:]
    return bytes.fromhex(raw_hex)


def is_already_known_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        x in msg
        for x in (
            "already known",
            "known transaction",
            "already imported",
            "transaction already in mempool",
        )
    )


def is_nonce_low_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "nonce too low" in msg or "already used" in msg


# ============================================================
# CONFIG
# ============================================================

@dataclass(frozen=True)
class Config:
    rpc_urls: list[str]
    private_key: str
    from_address: str
    to_address: str

    expected_chain_id: Optional[int]
    dry_run: bool

    state_file: Path

    rpc_timeout_sec: int
    receipt_timeout_sec: int
    receipt_poll_sec: int
    rebroadcast_every_sec: int

    gas_limit: int
    reserve_wei: int
    min_sweep_wei: int

    priority_fee_gwei: Decimal
    max_fee_multiplier: Decimal
    fee_bump_pct: Decimal

    max_replacements: int
    max_sweep_rounds: int

    allow_unknown_pending: bool

    rpc_proxy_url: Optional[str]
    require_rpc_proxy: bool

    @classmethod
    def load(cls) -> "Config":
        rpc_urls = [x.strip() for x in os.getenv("RPC_URLS", "").split(",") if x.strip()]
        if not rpc_urls:
            raise ConfigError("RPC_URLS is required")

        private_key = os.getenv("PRIVATE_KEY", "").strip()
        from_address = os.getenv("FROM_ADDRESS", "").strip()
        to_address = os.getenv("TO_ADDRESS", "").strip()

        if not private_key:
            raise ConfigError("PRIVATE_KEY is required")
        if not from_address:
            raise ConfigError("FROM_ADDRESS is required")
        if not to_address:
            raise ConfigError("TO_ADDRESS is required")

        expected_chain_raw = os.getenv("EXPECTED_CHAIN_ID", "").strip()
        expected_chain_id = int(expected_chain_raw) if expected_chain_raw else None

        rpc_proxy_url = os.getenv("RPC_PROXY_URL", "").strip() or None
        require_rpc_proxy = parse_bool(os.getenv("REQUIRE_RPC_PROXY"), False)
        if require_rpc_proxy and not rpc_proxy_url:
            raise ConfigError("REQUIRE_RPC_PROXY=true, but RPC_PROXY_URL is empty")

        return cls(
            rpc_urls=rpc_urls,
            private_key=private_key,
            from_address=from_address,
            to_address=to_address,
            expected_chain_id=expected_chain_id,
            dry_run=parse_bool(os.getenv("DRY_RUN"), True),

            state_file=Path(os.getenv("STATE_FILE", "eth_sweeper_state.json")),

            rpc_timeout_sec=parse_int_env("RPC_TIMEOUT_SEC", 20, 1),
            receipt_timeout_sec=parse_int_env("RECEIPT_TIMEOUT_SEC", 180, 10),
            receipt_poll_sec=parse_int_env("RECEIPT_POLL_SEC", 5, 1),
            rebroadcast_every_sec=parse_int_env("REBROADCAST_EVERY_SEC", 30, 5),

            gas_limit=parse_int_env("GAS_LIMIT", 21000, 21000),
            reserve_wei=parse_int_env("RESERVE_WEI", 0, 0),
            min_sweep_wei=parse_int_env("MIN_SWEEP_WEI", 1, 1),

            priority_fee_gwei=parse_decimal_env("PRIORITY_FEE_GWEI", "2", "0"),
            max_fee_multiplier=parse_decimal_env("MAX_FEE_MULTIPLIER", "2", "1"),
            fee_bump_pct=parse_decimal_env("FEE_BUMP_PCT", "15", "1"),

            max_replacements=parse_int_env("MAX_REPLACEMENTS", 3, 0),
            max_sweep_rounds=parse_int_env("MAX_SWEEP_ROUNDS", 1, 1),

            allow_unknown_pending=parse_bool(os.getenv("ALLOW_UNKNOWN_PENDING"), False),

            rpc_proxy_url=rpc_proxy_url,
            require_rpc_proxy=require_rpc_proxy,
        )


# ============================================================
# RPC MANAGER
# ============================================================

@dataclass
class RPCNode:
    url: str
    w3: Web3
    health: int = 100
    failures: int = 0
    cooldown_until: float = 0.0
    disabled: bool = False


class RPCManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.nodes = [RPCNode(url=u, w3=self._make_web3(u)) for u in cfg.rpc_urls]

    def _make_web3(self, url: str) -> Web3:
        request_kwargs: dict[str, Any] = {"timeout": self.cfg.rpc_timeout_sec}

        if self.cfg.rpc_proxy_url:
            request_kwargs["proxies"] = {
                "http": self.cfg.rpc_proxy_url,
                "https": self.cfg.rpc_proxy_url,
            }

        w3 = Web3(HTTPProvider(url, request_kwargs=request_kwargs))

        if ExtraDataToPOAMiddleware is not None:
            try:
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            except ValueError:
                pass

        return w3

    def _ranked(self) -> list[RPCNode]:
        now = time.time()
        live = [
            n for n in self.nodes
            if not n.disabled and n.cooldown_until <= now
        ]

        if not live:
            live = [n for n in self.nodes if not n.disabled]

        return sorted(live, key=lambda n: n.health, reverse=True)

    def _success(self, node: RPCNode) -> None:
        node.failures = 0
        node.health = min(100, node.health + 3)
        node.cooldown_until = 0

    def _failure(self, node: RPCNode, exc: Exception) -> None:
        node.failures += 1
        node.health = max(0, node.health - min(40, 8 * node.failures))
        node.cooldown_until = time.time() + min(60, 2 ** min(node.failures, 6))
        log.debug(
            "RPC failure: url=%s health=%s err=%s",
            redact_url(node.url),
            node.health,
            exc,
        )

    def call(self, label: str, fn: Callable[[Web3], Any]) -> Any:
        last_error: Optional[Exception] = None

        for node in self._ranked():
            if SHUTDOWN:
                raise RuntimeError("Shutdown requested")

            try:
                result = fn(node.w3)
                self._success(node)
                return result
            except Exception as exc:
                last_error = exc
                self._failure(node, exc)

        raise RuntimeError(f"All RPC endpoints failed during: {label}. Last error: {last_error}")

    def retain_chain(self, chain_id: int) -> None:
        ok = 0

        for node in self.nodes:
            try:
                node_chain_id = node.w3.eth.chain_id
                if int(node_chain_id) != int(chain_id):
                    node.disabled = True
                    log.error(
                        "Disabled RPC with wrong chain_id: url=%s got=%s expected=%s",
                        redact_url(node.url),
                        node_chain_id,
                        chain_id,
                    )
                else:
                    ok += 1
                    self._success(node)
            except Exception as exc:
                self._failure(node, exc)

        if ok == 0:
            raise RuntimeError(f"No RPC endpoint matches chain_id={chain_id}")

    def broadcast_raw(self, raw_tx: bytes, tx_hash: str) -> None:
        accepted = False
        errors: list[str] = []

        for node in self._ranked():
            if SHUTDOWN:
                raise RuntimeError("Shutdown requested")

            try:
                node.w3.eth.send_raw_transaction(raw_tx)
                self._success(node)
                accepted = True
                log.info("Broadcast accepted by RPC: %s", redact_url(node.url))
            except Exception as exc:
                if is_already_known_error(exc):
                    self._success(node)
                    accepted = True
                    log.info("Transaction already known by RPC: %s", redact_url(node.url))
                elif is_nonce_low_error(exc):
                    self._success(node)
                    errors.append(f"{redact_url(node.url)}: nonce too low")
                else:
                    self._failure(node, exc)
                    errors.append(f"{redact_url(node.url)}: {exc}")

        if not accepted:
            raise RuntimeError(f"Broadcast failed for {tx_hash}. Errors: {errors}")

    def find_receipt(self, tx_hash: str) -> Optional[Any]:
        for node in self._ranked():
            try:
                receipt = node.w3.eth.get_transaction_receipt(tx_hash)
                self._success(node)
                return receipt
            except TransactionNotFound:
                self._success(node)
                continue
            except Exception as exc:
                self._failure(node, exc)

        return None


# ============================================================
# STATE
# ============================================================

class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None

        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = time.time()

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)

        os.replace(tmp, self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


# ============================================================
# SWEEPER
# ============================================================

class Sweeper:
    def __init__(self):
        self.cfg = Config.load()
        self.rpc = RPCManager(self.cfg)
        self.state = StateStore(self.cfg.state_file)

        self.account = Account.from_key(self.cfg.private_key)

        self.from_addr = Web3.to_checksum_address(self.cfg.from_address)
        self.to_addr = Web3.to_checksum_address(self.cfg.to_address)

        self._validate_static_config()

        detected_chain_id = int(self.rpc.call("chain_id", lambda w3: w3.eth.chain_id))

        if self.cfg.expected_chain_id is not None:
            if detected_chain_id != self.cfg.expected_chain_id:
                raise ConfigError(
                    f"Wrong chain_id from RPC: got={detected_chain_id}, "
                    f"expected={self.cfg.expected_chain_id}"
                )
            self.chain_id = self.cfg.expected_chain_id
        else:
            self.chain_id = detected_chain_id
            log.warning(
                "EXPECTED_CHAIN_ID is not set. Detected chain_id=%s. "
                "Better set EXPECTED_CHAIN_ID explicitly.",
                self.chain_id,
            )

        self.rpc.retain_chain(self.chain_id)

        log.info(
            "Config loaded: from=%s to=%s chain_id=%s dry_run=%s state=%s rpc_count=%s",
            self.from_addr,
            self.to_addr,
            self.chain_id,
            self.cfg.dry_run,
            self.cfg.state_file,
            len(self.cfg.rpc_urls),
        )

    def _validate_static_config(self) -> None:
        if self.account.address.lower() != self.from_addr.lower():
            raise ConfigError(
                f"PRIVATE_KEY mismatch: key address={self.account.address}, "
                f"FROM_ADDRESS={self.from_addr}"
            )

        if self.from_addr.lower() == self.to_addr.lower():
            raise ConfigError("FROM_ADDRESS and TO_ADDRESS must be different")

        if self.from_addr.lower() == ZERO_ADDRESS.lower():
            raise ConfigError("FROM_ADDRESS cannot be zero address")

        if self.to_addr.lower() == ZERO_ADDRESS.lower():
            raise ConfigError("TO_ADDRESS cannot be zero address")

    def _nonce_counts(self) -> tuple[int, int]:
        latest = int(
            self.rpc.call(
                "latest nonce",
                lambda w3: w3.eth.get_transaction_count(self.from_addr, "latest"),
            )
        )
        pending = int(
            self.rpc.call(
                "pending nonce",
                lambda w3: w3.eth.get_transaction_count(self.from_addr, "pending"),
            )
        )
        return latest, pending

    def _fee_data(
        self,
        previous_fee: Optional[dict[str, int]] = None,
        bump: bool = False,
    ) -> dict[str, int]:
        block = self.rpc.call("latest block", lambda w3: w3.eth.get_block("latest"))
        bump_factor = Decimal("1") + (self.cfg.fee_bump_pct / Decimal("100"))

        base_fee = block.get("baseFeePerGas")

        if base_fee is not None:
            priority = gwei_to_wei(self.cfg.priority_fee_gwei)
            max_fee = int(Decimal(int(base_fee)) * self.cfg.max_fee_multiplier + Decimal(priority))

            if bump and previous_fee:
                old_priority = int(previous_fee["maxPriorityFeePerGas"])
                old_max_fee = int(previous_fee["maxFeePerGas"])

                priority = max(priority, int(Decimal(old_priority) * bump_factor))
                max_fee = max(max_fee, int(Decimal(old_max_fee) * bump_factor))

            max_fee = max(max_fee, priority)

            return {
                "type": 2,
                "maxPriorityFeePerGas": int(priority),
                "maxFeePerGas": int(max_fee),
            }

        gas_price = int(self.rpc.call("legacy gas price", lambda w3: w3.eth.gas_price))

        if bump and previous_fee:
            old_gas_price = int(previous_fee["gasPrice"])
            gas_price = max(gas_price, int(Decimal(old_gas_price) * bump_factor))

        return {
            "gasPrice": int(gas_price),
        }

    def _fee_ceiling(self, fee: dict[str, int]) -> int:
        if "maxFeePerGas" in fee:
            return int(fee["maxFeePerGas"])
        return int(fee["gasPrice"])

    def _build_tx(
        self,
        nonce: int,
        previous_fee: Optional[dict[str, int]] = None,
        bump: bool = False,
    ) -> dict[str, Any]:
        fee = self._fee_data(previous_fee=previous_fee, bump=bump)

        balance = int(
            self.rpc.call(
                "balance",
                lambda w3: w3.eth.get_balance(self.from_addr),
            )
        )

        gas_cost_ceiling = self.cfg.gas_limit * self._fee_ceiling(fee)
        value = balance - gas_cost_ceiling - self.cfg.reserve_wei

        if value < self.cfg.min_sweep_wei:
            raise NoSweepableBalance(
                f"No sweepable balance: balance={wei_to_eth(balance)} ETH, "
                f"max_gas_cost={wei_to_eth(gas_cost_ceiling)} ETH, "
                f"reserve={wei_to_eth(self.cfg.reserve_wei)} ETH, "
                f"min_sweep={wei_to_eth(self.cfg.min_sweep_wei)} ETH"
            )

        tx: dict[str, Any] = {
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": self.to_addr,
            "value": int(value),
            "gas": self.cfg.gas_limit,
            **fee,
        }

        return tx

    def _sign_to_state(
        self,
        tx: dict[str, Any],
        replacement_count: int = 0,
        replaced_tx_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = getattr(signed, "rawTransaction")

        raw_hex = Web3.to_hex(raw)
        tx_hash = Web3.to_hex(signed.hash)

        fee_fields = {
            k: int(v)
            for k, v in tx.items()
            if k in {"maxFeePerGas", "maxPriorityFeePerGas", "gasPrice"}
        }

        return {
            "schema": "eth-sweeper-v5",
            "created_at": time.time(),
            "updated_at": time.time(),

            "from": self.from_addr,
            "to": self.to_addr,
            "chain_id": self.chain_id,

            "nonce": int(tx["nonce"]),
            "gas": int(tx["gas"]),
            "value": int(tx["value"]),
            "fee": fee_fields,

            "tx_hash": tx_hash,
            "raw_transaction": raw_hex,

            "replacement_count": int(replacement_count),
            "replaced_tx_hash": replaced_tx_hash,

            "last_broadcast_at": 0,
            "status": "pending",
        }

    def _state_matches_config(self, s: dict[str, Any]) -> bool:
        return (
            s.get("schema") == "eth-sweeper-v5"
            and str(s.get("from", "")).lower() == self.from_addr.lower()
            and str(s.get("to", "")).lower() == self.to_addr.lower()
            and int(s.get("chain_id")) == int(self.chain_id)
            and s.get("status") == "pending"
        )

    def _print_tx_summary(self, s: dict[str, Any], prefix: str) -> None:
        fee = s["fee"]
        fee_part = ", ".join(f"{k}={v}" for k, v in fee.items())

        log.info(
            "%s tx=%s nonce=%s value=%s ETH gas=%s replacements=%s %s",
            prefix,
            s["tx_hash"],
            s["nonce"],
            wei_to_eth(int(s["value"])),
            s["gas"],
            s.get("replacement_count", 0),
            fee_part,
        )

    def _broadcast_state(self, s: dict[str, Any]) -> None:
        raw = hex_to_bytes(s["raw_transaction"])
        self.rpc.broadcast_raw(raw, s["tx_hash"])
        s["last_broadcast_at"] = time.time()
        self.state.save(s)

    def _handle_receipt(self, receipt: Any, s: dict[str, Any]) -> bool:
        status = int(receipt.get("status", 0))
        block_number = receipt.get("blockNumber")

        if status == 1:
            log.info(
                "Confirmed: tx=%s block=%s value=%s ETH",
                s["tx_hash"],
                block_number,
                wei_to_eth(int(s["value"])),
            )
            self.state.clear()
            return True

        self.state.clear()
        raise RuntimeError(f"Transaction failed/reverted: tx={s['tx_hash']} block={block_number}")

    def _wait_for_receipt(self, s: dict[str, Any], timeout_sec: int) -> Optional[Any]:
        deadline = time.time() + timeout_sec

        while not SHUTDOWN and time.time() < deadline:
            receipt = self.rpc.find_receipt(s["tx_hash"])
            if receipt is not None:
                return receipt

            sleep_for = self.cfg.receipt_poll_sec + random.uniform(0, 1.5)
            time.sleep(sleep_for)

        return None

    def _replace_pending(self, s: dict[str, Any]) -> dict[str, Any]:
        replacement_count = int(s.get("replacement_count", 0))

        if replacement_count >= self.cfg.max_replacements:
            raise TimeoutError(
                f"Max replacements reached: tx={s['tx_hash']} "
                f"replacement_count={replacement_count}"
            )

        tx = self._build_tx(
            nonce=int(s["nonce"]),
            previous_fee=s["fee"],
            bump=True,
        )

        new_state = self._sign_to_state(
            tx,
            replacement_count=replacement_count + 1,
            replaced_tx_hash=s["tx_hash"],
        )

        self.state.save(new_state)

        log.warning(
            "Replacing pending tx: old=%s new=%s nonce=%s replacement_count=%s",
            s["tx_hash"],
            new_state["tx_hash"],
            new_state["nonce"],
            new_state["replacement_count"],
        )

        self._print_tx_summary(new_state, "Replacement prepared:")
        self._broadcast_state(new_state)
        return new_state

    def _monitor_and_recover(self, s: dict[str, Any]) -> bool:
        if not self._state_matches_config(s):
            raise RuntimeError(
                "Existing state file does not match current config. "
                "Review it manually before deleting or reusing it."
            )

        self._print_tx_summary(s, "Recovered pending state:")

        if self.cfg.dry_run:
            log.warning("DRY_RUN=true: not broadcasting recovered transaction")
            return False

        while not SHUTDOWN:
            receipt = self.rpc.find_receipt(s["tx_hash"])
            if receipt is not None:
                return self._handle_receipt(receipt, s)

            last_broadcast_at = float(s.get("last_broadcast_at", 0) or 0)
            if time.time() - last_broadcast_at >= self.cfg.rebroadcast_every_sec:
                log.info("Rebroadcasting pending tx: %s", s["tx_hash"])
                self._broadcast_state(s)

            receipt = self._wait_for_receipt(s, self.cfg.receipt_timeout_sec)
            if receipt is not None:
                return self._handle_receipt(receipt, s)

            s = self._replace_pending(s)

        return False

    def _prepare_new_state(self) -> dict[str, Any]:
        latest_nonce, pending_nonce = self._nonce_counts()

        if pending_nonce > latest_nonce and not self.cfg.allow_unknown_pending:
            raise RuntimeError(
                f"Unknown pending tx detected: latest_nonce={latest_nonce}, "
                f"pending_nonce={pending_nonce}. "
                f"State file has no known pending tx. "
                f"Set ALLOW_UNKNOWN_PENDING=true only if you understand the risk."
            )

        nonce = pending_nonce
        tx = self._build_tx(nonce=nonce)
        return self._sign_to_state(tx)

    def run(self) -> None:
        log.warning("DRY_RUN=%s", self.cfg.dry_run)

        for round_no in range(1, self.cfg.max_sweep_rounds + 1):
            if SHUTDOWN:
                break

            log.info("Sweep round %s/%s", round_no, self.cfg.max_sweep_rounds)

            existing_state = self.state.load()
            if existing_state:
                confirmed = self._monitor_and_recover(existing_state)
                if not confirmed:
                    return
                continue

            try:
                s = self._prepare_new_state()
            except NoSweepableBalance as exc:
                log.info("%s", exc)
                return

            self._print_tx_summary(s, "Prepared:")

            if self.cfg.dry_run:
                log.warning("DRY_RUN=true: transaction was NOT saved and NOT broadcasted")
                return

            self.state.save(s)
            self._broadcast_state(s)

            confirmed = self._monitor_and_recover(s)
            if not confirmed:
                return

        log.info("Done")


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    try:
        Sweeper().run()
    except KeyboardInterrupt:
        shutdown_handler()
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        raise
