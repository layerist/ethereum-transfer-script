#!/usr/bin/env python3
"""
Ultra-Reliable Native Coin Sweeper v7 — Single Wallet Safe Edition

Sweeps the native coin of one EVM wallet to one fixed destination.

Safety properties:
- dry-run by default;
- explicit chain-id validation is strongly recommended;
- single-process lock prevents concurrent use of the same state file;
- atomic, fsync-backed state writes;
- persistent replacement history and receipt checks for every known tx hash;
- replacement-fee headroom is reserved before the initial sweep;
- mined-but-not-final transactions are never replaced while awaiting confirmations;
- replacement transactions preserve nonce, destination, value, gas and chain;
- persisted raw transactions are hash-checked and signer-verified before recovery;
- conservative nonce reconciliation across several RPC endpoints;
- wrong-chain RPC endpoints are permanently disabled;
- EIP-1559 and legacy fee support;
- fee caps prevent accidental transactions during extreme fee spikes;
- optional proxy with fail-closed configuration;
- graceful shutdown without losing recoverable transaction state.

Python: 3.10+
Dependencies: web3, eth-account, python-dotenv
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import signal
import socket
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TypeVar
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from eth_account import Account
from web3 import HTTPProvider, Web3
from web3.exceptions import TransactionNotFound

try:
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError:  # web3 compatibility
    ExtraDataToPOAMiddleware = None


# =============================================================================
# LOGGING / SHUTDOWN
# =============================================================================

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("NATIVE_SWEEPER")

SHUTDOWN = False
T = TypeVar("T")


def shutdown_handler(*_: Any) -> None:
    global SHUTDOWN
    if not SHUTDOWN:
        SHUTDOWN = True
        log.warning("Shutdown requested; finishing the current atomic operation...")


signal.signal(signal.SIGINT, shutdown_handler)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, shutdown_handler)


# =============================================================================
# ERRORS / HELPERS
# =============================================================================

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WEI_PER_ETH = Decimal("1000000000000000000")
WEI_PER_GWEI = Decimal("1000000000")
STATE_SCHEMA = "native-sweeper-v7"
SUPPORTED_STATE_SCHEMAS = {"native-sweeper-v6", STATE_SCHEMA}


class SweeperError(RuntimeError):
    pass


class ConfigError(SweeperError):
    pass


class StateError(SweeperError):
    pass


class NoSweepableBalance(SweeperError):
    pass


class UnknownPendingTransaction(SweeperError):
    pass


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"Invalid boolean value: {value!r}")


def parse_int_env(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc

    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return value


def parse_decimal_env(
    name: str,
    default: str,
    *,
    minimum: Optional[str] = None,
    maximum: Optional[str] = None,
) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number, got {raw!r}") from exc

    if not value.is_finite():
        raise ConfigError(f"{name} must be finite")
    if minimum is not None and value < Decimal(minimum):
        raise ConfigError(f"{name} must be >= {minimum}")
    if maximum is not None and value > Decimal(maximum):
        raise ConfigError(f"{name} must be <= {maximum}")
    return value


def gwei_to_wei(value: Decimal) -> int:
    return int((value * WEI_PER_GWEI).to_integral_value(rounding=ROUND_CEILING))


def wei_to_coin(value: int) -> str:
    text = f"{Decimal(value) / WEI_PER_ETH:.18f}".rstrip("0").rstrip(".")
    return text or "0"


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        if parts.username or parts.password:
            host = f"***:***@{host}"
        # Query strings often contain API keys.
        query = "<redacted>" if parts.query else ""
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except Exception:
        return "<redacted-rpc-url>"


def decode_raw_transaction(raw_hex: str) -> bytes:
    value = raw_hex.strip()
    if value.startswith("0x"):
        value = value[2:]
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise StateError("State contains an invalid raw_transaction hex string") from exc


def exception_text(exc: BaseException) -> str:
    return str(exc).lower()


def is_already_known_error(exc: BaseException) -> bool:
    text = exception_text(exc)
    return any(
        marker in text
        for marker in (
            "already known",
            "known transaction",
            "already imported",
            "transaction already in mempool",
        )
    )


def is_nonce_low_error(exc: BaseException) -> bool:
    text = exception_text(exc)
    return "nonce too low" in text or "nonce has already been used" in text


def is_replacement_underpriced_error(exc: BaseException) -> bool:
    text = exception_text(exc)
    return "replacement transaction underpriced" in text or "fee too low to replace" in text


def ensure_parent_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass(frozen=True)
class Config:
    rpc_urls: tuple[str, ...]
    private_key: str
    from_address: str
    to_address: str
    expected_chain_id: Optional[int]
    dry_run: bool

    state_file: Path
    lock_file: Path

    rpc_timeout_sec: int
    receipt_timeout_sec: int
    receipt_poll_sec: int
    rebroadcast_every_sec: int

    gas_limit: int
    auto_estimate_gas: bool
    gas_estimate_multiplier: Decimal
    reserve_wei: int
    min_sweep_wei: int

    priority_fee_gwei: Decimal
    max_fee_multiplier: Decimal
    fee_bump_pct: Decimal
    replacement_headroom_bumps: int
    max_fee_gwei: Decimal
    max_priority_fee_gwei: Decimal

    max_replacements: int
    max_sweep_rounds: int
    min_confirmations: int

    allow_unknown_pending: bool
    allow_contract_destination: bool

    rpc_proxy_url: Optional[str]
    require_rpc_proxy: bool

    @classmethod
    def load(cls) -> "Config":
        rpc_urls = tuple(
            dict.fromkeys(
                value.strip()
                for value in os.getenv("RPC_URLS", "").split(",")
                if value.strip()
            )
        )
        if not rpc_urls:
            raise ConfigError("RPC_URLS is required")
        for url in rpc_urls:
            if urlsplit(url).scheme not in {"http", "https"}:
                raise ConfigError(f"Unsupported RPC URL scheme: {redact_url(url)}")

        private_key = os.getenv("PRIVATE_KEY", "").strip()
        from_address = os.getenv("FROM_ADDRESS", "").strip()
        to_address = os.getenv("TO_ADDRESS", "").strip()
        if not private_key:
            raise ConfigError("PRIVATE_KEY is required")
        if not from_address:
            raise ConfigError("FROM_ADDRESS is required")
        if not to_address:
            raise ConfigError("TO_ADDRESS is required")

        expected_raw = os.getenv("EXPECTED_CHAIN_ID", "").strip()
        if expected_raw:
            try:
                expected_chain_id = int(expected_raw)
            except ValueError as exc:
                raise ConfigError("EXPECTED_CHAIN_ID must be an integer") from exc
            if expected_chain_id <= 0:
                raise ConfigError("EXPECTED_CHAIN_ID must be > 0")
        else:
            expected_chain_id = None

        state_file = Path(os.getenv("STATE_FILE", "native_sweeper_state.json")).expanduser()
        lock_file = Path(os.getenv("LOCK_FILE", f"{state_file}.lock")).expanduser()
        if state_file.resolve() == lock_file.resolve():
            raise ConfigError("STATE_FILE and LOCK_FILE must be different")

        proxy_url = os.getenv("RPC_PROXY_URL", "").strip() or None
        require_proxy = parse_bool(os.getenv("REQUIRE_RPC_PROXY"), False)
        if require_proxy and not proxy_url:
            raise ConfigError("REQUIRE_RPC_PROXY=true but RPC_PROXY_URL is empty")
        if proxy_url and urlsplit(proxy_url).scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ConfigError("RPC_PROXY_URL must use http, https, socks5 or socks5h")

        max_replacements = parse_int_env("MAX_REPLACEMENTS", 3, minimum=0, maximum=100)
        replacement_headroom_bumps = parse_int_env(
            "REPLACEMENT_HEADROOM_BUMPS",
            max_replacements,
            minimum=0,
            maximum=100,
        )

        return cls(
            rpc_urls=rpc_urls,
            private_key=private_key,
            from_address=from_address,
            to_address=to_address,
            expected_chain_id=expected_chain_id,
            dry_run=parse_bool(os.getenv("DRY_RUN"), True),
            state_file=state_file,
            lock_file=lock_file,
            rpc_timeout_sec=parse_int_env("RPC_TIMEOUT_SEC", 20, minimum=1, maximum=300),
            receipt_timeout_sec=parse_int_env("RECEIPT_TIMEOUT_SEC", 180, minimum=10),
            receipt_poll_sec=parse_int_env("RECEIPT_POLL_SEC", 5, minimum=1, maximum=300),
            rebroadcast_every_sec=parse_int_env("REBROADCAST_EVERY_SEC", 45, minimum=5),
            gas_limit=parse_int_env("GAS_LIMIT", 21_000, minimum=21_000, maximum=30_000_000),
            auto_estimate_gas=parse_bool(os.getenv("AUTO_ESTIMATE_GAS"), True),
            gas_estimate_multiplier=parse_decimal_env(
                "GAS_ESTIMATE_MULTIPLIER", "1.20", minimum="1", maximum="3"
            ),
            reserve_wei=parse_int_env("RESERVE_WEI", 0, minimum=0),
            min_sweep_wei=parse_int_env("MIN_SWEEP_WEI", 1, minimum=1),
            priority_fee_gwei=parse_decimal_env("PRIORITY_FEE_GWEI", "2", minimum="0"),
            max_fee_multiplier=parse_decimal_env("MAX_FEE_MULTIPLIER", "2", minimum="1", maximum="10"),
            fee_bump_pct=parse_decimal_env("FEE_BUMP_PCT", "15", minimum="10", maximum="200"),
            replacement_headroom_bumps=replacement_headroom_bumps,
            max_fee_gwei=parse_decimal_env("MAX_FEE_GWEI", "500", minimum="0.000000001"),
            max_priority_fee_gwei=parse_decimal_env(
                "MAX_PRIORITY_FEE_GWEI", "100", minimum="0"
            ),
            max_replacements=max_replacements,
            max_sweep_rounds=parse_int_env("MAX_SWEEP_ROUNDS", 1, minimum=1, maximum=100),
            min_confirmations=parse_int_env("MIN_CONFIRMATIONS", 1, minimum=1, maximum=10_000),
            allow_unknown_pending=parse_bool(os.getenv("ALLOW_UNKNOWN_PENDING"), False),
            allow_contract_destination=parse_bool(
                os.getenv("ALLOW_CONTRACT_DESTINATION"), False
            ),
            rpc_proxy_url=proxy_url,
            require_rpc_proxy=require_proxy,
        )


# =============================================================================
# PROCESS LOCK / PERSISTENT STATE
# =============================================================================

class ProcessLock:
    """Simple cross-platform best-effort lock based on atomic file creation."""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        ensure_parent_directory(self.path)
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": time.time(),
        }
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            details = ""
            try:
                details = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise StateError(
                f"Another sweeper instance may be running; lock exists: {self.path}. "
                f"Lock contents: {details or '<unreadable>'}. Remove it only after "
                "confirming that no sweeper process is active."
            ) from exc

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                raise

        self.acquired = True
        atexit.register(self.release)

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                log.error("Unable to remove lock file %s: %s", self.path, exc)
            self.acquired = False

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Cannot read valid state file {self.path}: {exc}") from exc
        if not isinstance(value, dict):
            raise StateError("State file root must be a JSON object")
        return value

    def save(self, state: dict[str, Any]) -> None:
        ensure_parent_directory(self.path)
        state["updated_at"] = time.time()
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")

        try:
            fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._fsync_directory(self.path.parent)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StateError(f"Unable to save state file {self.path}: {exc}") from exc

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
            self._fsync_directory(self.path.parent)
        except OSError as exc:
            raise StateError(f"Unable to remove state file {self.path}: {exc}") from exc

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        # Directory fsync is unsupported on some platforms (notably Windows).
        try:
            flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
            fd = os.open(directory, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


# =============================================================================
# RPC MANAGEMENT
# =============================================================================

@dataclass
class RPCNode:
    url: str
    w3: Web3
    health: int = 100
    failures: int = 0
    cooldown_until: float = 0.0
    disabled: bool = False
    last_error: str = ""


@dataclass
class BroadcastResult:
    accepted: bool = False
    nonce_too_low: bool = False
    replacement_underpriced: bool = False
    errors: list[str] = field(default_factory=list)


class RPCManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.nodes = [RPCNode(url=url, w3=self._make_web3(url)) for url in cfg.rpc_urls]

    def _make_web3(self, url: str) -> Web3:
        request_kwargs: dict[str, Any] = {"timeout": self.cfg.rpc_timeout_sec}
        if self.cfg.rpc_proxy_url:
            request_kwargs["proxies"] = {
                "http": self.cfg.rpc_proxy_url,
                "https": self.cfg.rpc_proxy_url,
            }

        provider = HTTPProvider(url, request_kwargs=request_kwargs)
        w3 = Web3(provider)
        if ExtraDataToPOAMiddleware is not None:
            try:
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            except ValueError:
                pass
        return w3

    def ranked_nodes(self, *, include_cooling: bool = False) -> list[RPCNode]:
        now = time.time()
        nodes = [node for node in self.nodes if not node.disabled]
        if not include_cooling:
            ready = [node for node in nodes if node.cooldown_until <= now]
            if ready:
                nodes = ready
        return sorted(nodes, key=lambda node: (node.health, -node.failures), reverse=True)

    def _success(self, node: RPCNode) -> None:
        node.failures = 0
        node.health = min(100, node.health + 3)
        node.cooldown_until = 0.0
        node.last_error = ""

    def _failure(self, node: RPCNode, exc: BaseException) -> None:
        node.failures += 1
        node.health = max(0, node.health - min(40, 8 * node.failures))
        node.cooldown_until = time.time() + min(120, 2 ** min(node.failures, 7))
        node.last_error = str(exc)
        log.debug(
            "RPC failure: url=%s health=%s cooldown=%.1fs error=%s",
            redact_url(node.url),
            node.health,
            max(0.0, node.cooldown_until - time.time()),
            exc,
        )

    def call(self, label: str, fn: Callable[[Web3], T]) -> T:
        last_error: Optional[BaseException] = None
        for node in self.ranked_nodes():
            if SHUTDOWN:
                raise SweeperError("Shutdown requested")
            try:
                result = fn(node.w3)
                self._success(node)
                return result
            except Exception as exc:
                last_error = exc
                self._failure(node, exc)
        raise SweeperError(f"All RPC endpoints failed during {label}: {last_error}")

    def collect(self, label: str, fn: Callable[[Web3], T]) -> list[tuple[RPCNode, T]]:
        results: list[tuple[RPCNode, T]] = []
        for node in self.ranked_nodes(include_cooling=True):
            if SHUTDOWN:
                break
            try:
                value = fn(node.w3)
                self._success(node)
                results.append((node, value))
            except Exception as exc:
                self._failure(node, exc)
        if not results:
            errors = "; ".join(
                f"{redact_url(node.url)}: {node.last_error}" for node in self.nodes
            )
            raise SweeperError(f"Every RPC failed during {label}: {errors}")
        return results

    def validate_chain(self, expected_chain_id: Optional[int]) -> int:
        results = self.collect("chain-id validation", lambda w3: int(w3.eth.chain_id))
        counts: dict[int, int] = {}
        for _, chain_id in results:
            counts[chain_id] = counts.get(chain_id, 0) + 1

        if expected_chain_id is None:
            # Choose the majority among reachable endpoints, not simply the first one.
            chain_id = max(counts, key=lambda item: (counts[item], item))
            log.warning(
                "EXPECTED_CHAIN_ID is not set; selected majority chain_id=%s from %s. "
                "Set it explicitly before production use.",
                chain_id,
                counts,
            )
        else:
            chain_id = expected_chain_id

        matching = 0
        for node, node_chain_id in results:
            if node_chain_id != chain_id:
                node.disabled = True
                log.error(
                    "Disabled wrong-chain RPC: url=%s got=%s expected=%s",
                    redact_url(node.url),
                    node_chain_id,
                    chain_id,
                )
            else:
                matching += 1

        if matching == 0:
            raise ConfigError(f"No reachable RPC endpoint matches chain_id={chain_id}")
        return chain_id

    def nonce_counts(self, address: str) -> tuple[int, int]:
        latest_results = self.collect(
            "latest nonce",
            lambda w3: int(w3.eth.get_transaction_count(address, "latest")),
        )
        pending_results = self.collect(
            "pending nonce",
            lambda w3: int(w3.eth.get_transaction_count(address, "pending")),
        )
        latest_values = [value for _, value in latest_results]
        pending_values = [value for _, value in pending_results]

        latest = max(latest_values)
        pending = max(max(pending_values), latest)
        if len(set(latest_values)) > 1 or len(set(pending_values)) > 1:
            log.warning(
                "RPC nonce disagreement: latest=%s pending=%s; using latest=%s pending=%s",
                latest_values,
                pending_values,
                latest,
                pending,
            )
        return latest, pending

    def broadcast_raw(self, raw_tx: bytes, tx_hash: str) -> BroadcastResult:
        result = BroadcastResult()
        for node in self.ranked_nodes(include_cooling=True):
            if SHUTDOWN:
                raise SweeperError("Shutdown requested")
            try:
                returned_hash = Web3.to_hex(node.w3.eth.send_raw_transaction(raw_tx))
                self._success(node)
                result.accepted = True
                if returned_hash.lower() != tx_hash.lower():
                    log.warning(
                        "RPC returned a different hash: expected=%s got=%s url=%s",
                        tx_hash,
                        returned_hash,
                        redact_url(node.url),
                    )
                else:
                    log.info("Broadcast accepted by %s", redact_url(node.url))
            except Exception as exc:
                if is_already_known_error(exc):
                    self._success(node)
                    result.accepted = True
                    log.info("Transaction already known by %s", redact_url(node.url))
                elif is_nonce_low_error(exc):
                    self._success(node)
                    result.nonce_too_low = True
                    result.errors.append(f"{redact_url(node.url)}: nonce too low")
                elif is_replacement_underpriced_error(exc):
                    self._success(node)
                    result.replacement_underpriced = True
                    result.errors.append(f"{redact_url(node.url)}: replacement underpriced")
                else:
                    self._failure(node, exc)
                    result.errors.append(f"{redact_url(node.url)}: {exc}")

        if not result.accepted and not result.nonce_too_low and not result.replacement_underpriced:
            raise SweeperError(f"Broadcast failed for {tx_hash}: {result.errors}")
        return result

    def find_any_receipt(self, tx_hashes: Iterable[str]) -> Optional[tuple[str, Any]]:
        hashes = list(dict.fromkeys(tx_hashes))
        for node in self.ranked_nodes(include_cooling=True):
            for tx_hash in reversed(hashes):
                try:
                    receipt = node.w3.eth.get_transaction_receipt(tx_hash)
                    self._success(node)
                    return tx_hash, receipt
                except TransactionNotFound:
                    self._success(node)
                except Exception as exc:
                    self._failure(node, exc)
                    break
        return None


# =============================================================================
# SWEEPER
# =============================================================================

class Sweeper:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config.load()
        self.account = Account.from_key(self.cfg.private_key)

        try:
            self.from_addr = Web3.to_checksum_address(self.cfg.from_address)
            self.to_addr = Web3.to_checksum_address(self.cfg.to_address)
        except ValueError as exc:
            raise ConfigError(f"Invalid EVM address: {exc}") from exc

        self._validate_static_config()
        self.rpc = RPCManager(self.cfg)
        self.chain_id = self.rpc.validate_chain(self.cfg.expected_chain_id)
        self.state = StateStore(self.cfg.state_file)

        self._validate_destination()
        log.info(
            "Configuration: from=%s to=%s chain_id=%s dry_run=%s state=%s rpc_count=%s",
            self.from_addr,
            self.to_addr,
            self.chain_id,
            self.cfg.dry_run,
            self.cfg.state_file,
            len(self.rpc.ranked_nodes(include_cooling=True)),
        )

    def _validate_static_config(self) -> None:
        if self.account.address.lower() != self.from_addr.lower():
            raise ConfigError(
                f"PRIVATE_KEY belongs to {self.account.address}, not FROM_ADDRESS={self.from_addr}"
            )
        if self.from_addr.lower() == self.to_addr.lower():
            raise ConfigError("FROM_ADDRESS and TO_ADDRESS must be different")
        if self.from_addr.lower() == ZERO_ADDRESS.lower():
            raise ConfigError("FROM_ADDRESS cannot be the zero address")
        if self.to_addr.lower() == ZERO_ADDRESS.lower():
            raise ConfigError("TO_ADDRESS cannot be the zero address")
        if self.cfg.max_priority_fee_gwei > self.cfg.max_fee_gwei:
            raise ConfigError("MAX_PRIORITY_FEE_GWEI cannot exceed MAX_FEE_GWEI")
        if self.cfg.replacement_headroom_bumps < self.cfg.max_replacements:
            log.warning(
                "REPLACEMENT_HEADROOM_BUMPS=%s is below MAX_REPLACEMENTS=%s; later "
                "same-value replacements may become unaffordable without RESERVE_WEI",
                self.cfg.replacement_headroom_bumps,
                self.cfg.max_replacements,
            )

    def _validate_destination(self) -> None:
        code = bytes(
            self.rpc.call(
                "destination bytecode",
                lambda w3: w3.eth.get_code(self.to_addr, "latest"),
            )
        )
        if code and not self.cfg.allow_contract_destination:
            raise ConfigError(
                "TO_ADDRESS contains contract bytecode. Native transfers to contracts may "
                "execute code or revert. Set ALLOW_CONTRACT_DESTINATION=true only after review."
            )
        if code:
            log.warning("Destination is a contract (%s bytecode bytes)", len(code))

    def _current_balance(self) -> int:
        values = self.rpc.collect(
            "wallet balance",
            lambda w3: int(w3.eth.get_balance(self.from_addr, "latest")),
        )
        balances = [value for _, value in values]
        # Using the minimum avoids signing a tx unaffordable on the most advanced RPC.
        balance = min(balances)
        if len(set(balances)) > 1:
            log.warning("RPC balance disagreement: %s; using conservative minimum=%s", balances, balance)
        return balance

    def _fee_data(
        self,
        *,
        previous_fee: Optional[dict[str, int]] = None,
        bump: bool = False,
    ) -> dict[str, int]:
        block = self.rpc.call("latest block", lambda w3: w3.eth.get_block("latest"))
        bump_factor = Decimal("1") + self.cfg.fee_bump_pct / Decimal("100")
        base_fee = block.get("baseFeePerGas")

        if base_fee is not None:
            priority = gwei_to_wei(self.cfg.priority_fee_gwei)
            max_fee = int(
                Decimal(int(base_fee)) * self.cfg.max_fee_multiplier + Decimal(priority)
            )
            if bump and previous_fee:
                old_priority = int(previous_fee["maxPriorityFeePerGas"])
                old_max_fee = int(previous_fee["maxFeePerGas"])
                priority = max(priority, self._ceil_decimal(Decimal(old_priority) * bump_factor))
                max_fee = max(max_fee, self._ceil_decimal(Decimal(old_max_fee) * bump_factor))

            max_priority_cap = gwei_to_wei(self.cfg.max_priority_fee_gwei)
            max_fee_cap = gwei_to_wei(self.cfg.max_fee_gwei)
            if priority > max_priority_cap:
                raise ConfigError(
                    f"Required priority fee {priority} wei exceeds MAX_PRIORITY_FEE_GWEI="
                    f"{self.cfg.max_priority_fee_gwei}"
                )
            if max_fee > max_fee_cap:
                raise ConfigError(
                    f"Required max fee {max_fee} wei exceeds MAX_FEE_GWEI={self.cfg.max_fee_gwei}"
                )
            if max_fee < priority:
                max_fee = priority
            return {
                "type": 2,
                "maxPriorityFeePerGas": priority,
                "maxFeePerGas": max_fee,
            }

        gas_price = int(self.rpc.call("legacy gas price", lambda w3: w3.eth.gas_price))
        if bump and previous_fee:
            gas_price = max(
                gas_price,
                self._ceil_decimal(Decimal(int(previous_fee["gasPrice"])) * bump_factor),
            )
        cap = gwei_to_wei(self.cfg.max_fee_gwei)
        if gas_price > cap:
            raise ConfigError(
                f"Required gasPrice {gas_price} wei exceeds MAX_FEE_GWEI={self.cfg.max_fee_gwei}"
            )
        return {"gasPrice": gas_price}

    @staticmethod
    def _fee_ceiling(fee: dict[str, int]) -> int:
        return int(fee.get("maxFeePerGas", fee.get("gasPrice", 0)))

    @staticmethod
    def _ceil_decimal(value: Decimal) -> int:
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    def _replacement_budget_per_gas(self, fee: dict[str, int]) -> int:
        """Reserve enough value for configured future fee bumps.

        This prevents the first sweep from consuming so much balance that a
        same-value replacement becomes unaffordable immediately afterwards.
        It intentionally models fee bumps from the current quote; a sudden
        base-fee jump can still exceed this budget and is then stopped by the
        configured hard fee caps.
        """
        current = self._fee_ceiling(fee)
        if self.cfg.replacement_headroom_bumps <= 0:
            return current

        bump_factor = Decimal("1") + self.cfg.fee_bump_pct / Decimal("100")
        budget = Decimal(current) * (bump_factor ** self.cfg.replacement_headroom_bumps)
        cap = gwei_to_wei(self.cfg.max_fee_gwei)
        return min(cap, self._ceil_decimal(budget))

    def _determine_gas_limit(self, fee: dict[str, int], balance: int) -> int:
        if not self.cfg.auto_estimate_gas:
            return self.cfg.gas_limit

        provisional_gas = self.cfg.gas_limit
        provisional_value = max(
            0,
            balance - provisional_gas * self._fee_ceiling(fee) - self.cfg.reserve_wei,
        )
        estimate_tx: dict[str, Any] = {
            "from": self.from_addr,
            "to": self.to_addr,
            "value": provisional_value,
            **fee,
        }
        estimate = int(
            self.rpc.call(
                "gas estimate",
                lambda w3: w3.eth.estimate_gas(estimate_tx, "latest"),
            )
        )
        padded = int(
            (Decimal(estimate) * self.cfg.gas_estimate_multiplier).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        gas_limit = max(21_000, padded)
        if gas_limit > self.cfg.gas_limit:
            raise ConfigError(
                f"Estimated gas limit {gas_limit} exceeds configured GAS_LIMIT={self.cfg.gas_limit}. "
                "Review the destination and increase GAS_LIMIT deliberately if appropriate."
            )
        return gas_limit

    def _build_new_tx(self, nonce: int) -> dict[str, Any]:
        fee = self._fee_data()
        balance = self._current_balance()
        gas_limit = self._determine_gas_limit(fee, balance)
        quoted_fee_cost = gas_limit * self._fee_ceiling(fee)
        replacement_budget = gas_limit * self._replacement_budget_per_gas(fee)
        value = balance - replacement_budget - self.cfg.reserve_wei
        if value < self.cfg.min_sweep_wei:
            raise NoSweepableBalance(
                "No sweepable balance: "
                f"balance={wei_to_coin(balance)}, quoted_gas_cost={wei_to_coin(quoted_fee_cost)}, "
                f"replacement_budget={wei_to_coin(replacement_budget)}, "
                f"reserve={wei_to_coin(self.cfg.reserve_wei)}, "
                f"minimum={wei_to_coin(self.cfg.min_sweep_wei)}"
            )
        return {
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": self.to_addr,
            "value": value,
            "gas": gas_limit,
            **fee,
        }

    def _build_replacement_tx(self, state: dict[str, Any]) -> dict[str, Any]:
        # A replacement must preserve the original payment. Only fee fields change.
        fee = self._fee_data(previous_fee=state["current"]["fee"], bump=True)
        tx = {
            "chainId": self.chain_id,
            "nonce": int(state["nonce"]),
            "to": self.to_addr,
            "value": int(state["value"]),
            "gas": int(state["gas"]),
            **fee,
        }
        required = int(state["value"]) + int(state["gas"]) * self._fee_ceiling(fee)
        balance = self._current_balance()
        if required > balance:
            raise NoSweepableBalance(
                "Cannot afford replacement while preserving its original value: "
                f"required={wei_to_coin(required)}, balance={wei_to_coin(balance)}. "
                "Do not create a conflicting transaction; add funds or wait for fee conditions."
            )
        return tx

    def _sign(self, tx: dict[str, Any]) -> dict[str, Any]:
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = getattr(signed, "rawTransaction")
        fee = {
            key: int(value)
            for key, value in tx.items()
            if key in {"maxFeePerGas", "maxPriorityFeePerGas", "gasPrice"}
        }
        return {
            "tx_hash": Web3.to_hex(signed.hash),
            "raw_transaction": Web3.to_hex(raw),
            "fee": fee,
            "signed_at": time.time(),
            "last_broadcast_at": 0.0,
            "broadcast_attempts": 0,
        }

    def _new_state(self, tx: dict[str, Any]) -> dict[str, Any]:
        current = self._sign(tx)
        return {
            "schema": STATE_SCHEMA,
            "status": "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
            "from": self.from_addr,
            "to": self.to_addr,
            "chain_id": self.chain_id,
            "nonce": int(tx["nonce"]),
            "gas": int(tx["gas"]),
            "value": int(tx["value"]),
            "replacement_count": 0,
            "current": current,
            "history": [],
        }

    def _validate_state(self, state: dict[str, Any]) -> None:
        required = {
            "schema",
            "status",
            "from",
            "to",
            "chain_id",
            "nonce",
            "gas",
            "value",
            "replacement_count",
            "current",
            "history",
        }
        missing = required - state.keys()
        if missing:
            raise StateError(f"State file is missing fields: {sorted(missing)}")
        if state["schema"] not in SUPPORTED_STATE_SCHEMAS or state["status"] != "pending":
            raise StateError("State file schema/status is unsupported")
        if state["schema"] != STATE_SCHEMA:
            log.warning(
                "Recovering compatible legacy state schema=%s; it will be upgraded on the next save",
                state["schema"],
            )
            state["schema"] = STATE_SCHEMA
        if str(state["from"]).lower() != self.from_addr.lower():
            raise StateError("State FROM address does not match configuration")
        if str(state["to"]).lower() != self.to_addr.lower():
            raise StateError("State TO address does not match configuration")
        if int(state["chain_id"]) != self.chain_id:
            raise StateError("State chain_id does not match configuration")
        if not isinstance(state["history"], list) or not isinstance(state["current"], dict):
            raise StateError("State history/current has an invalid type")
        for key in ("tx_hash", "raw_transaction", "fee"):
            if key not in state["current"]:
                raise StateError(f"Current state entry is missing {key}")
        if int(state["nonce"]) < 0 or int(state["gas"]) < 21_000 or int(state["value"]) < 0:
            raise StateError("State contains invalid numeric transaction fields")

        entries = [*state["history"], state["current"]]
        for entry in entries:
            if not isinstance(entry, dict):
                raise StateError("State transaction history contains a non-object entry")
            for key in ("tx_hash", "raw_transaction", "fee"):
                if key not in entry:
                    raise StateError(f"State transaction entry is missing {key}")
            raw = decode_raw_transaction(str(entry["raw_transaction"]))
            calculated_hash = Web3.to_hex(Web3.keccak(raw))
            if calculated_hash.lower() != str(entry["tx_hash"]).lower():
                raise StateError(
                    f"State raw transaction hash mismatch: stored={entry['tx_hash']} "
                    f"calculated={calculated_hash}"
                )
            try:
                recovered = Account.recover_transaction(raw)
            except Exception as exc:
                raise StateError("Cannot recover signer from state raw transaction") from exc
            if recovered.lower() != self.from_addr.lower():
                raise StateError(
                    f"State raw transaction signer {recovered} does not match {self.from_addr}"
                )

    @staticmethod
    def _all_hashes(state: dict[str, Any]) -> list[str]:
        hashes = [entry["tx_hash"] for entry in state.get("history", [])]
        hashes.append(state["current"]["tx_hash"])
        return list(dict.fromkeys(hashes))

    def _print_summary(self, state: dict[str, Any], prefix: str) -> None:
        current = state["current"]
        fee_text = ", ".join(f"{key}={value}" for key, value in current["fee"].items())
        log.info(
            "%s tx=%s nonce=%s value=%s gas=%s replacements=%s %s",
            prefix,
            current["tx_hash"],
            state["nonce"],
            wei_to_coin(int(state["value"])),
            state["gas"],
            state["replacement_count"],
            fee_text,
        )

    def _receipt_info(self, state: dict[str, Any]) -> Optional[tuple[str, Any, int]]:
        found = self.rpc.find_any_receipt(self._all_hashes(state))
        if found is None:
            return None
        tx_hash, receipt = found
        block_number = receipt.get("blockNumber")
        if block_number is None:
            return None
        latest_block = int(self.rpc.call("latest block number", lambda w3: w3.eth.block_number))
        confirmations = max(0, latest_block - int(block_number) + 1)
        return tx_hash, receipt, confirmations

    def _confirmed_receipt(self, state: dict[str, Any]) -> Optional[tuple[str, Any]]:
        info = self._receipt_info(state)
        if info is None:
            return None
        tx_hash, receipt, confirmations = info
        if confirmations < self.cfg.min_confirmations:
            log.info(
                "Mined but awaiting confirmations: tx=%s block=%s confirmations=%s/%s",
                tx_hash,
                receipt.get("blockNumber"),
                confirmations,
                self.cfg.min_confirmations,
            )
            return None
        return tx_hash, receipt

    def _handle_receipt(self, tx_hash: str, receipt: Any, state: dict[str, Any]) -> bool:
        status = int(receipt.get("status", 0))
        block_number = receipt.get("blockNumber")
        gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        if status == 1:
            log.info(
                "Confirmed: tx=%s block=%s value=%s gas_used=%s effective_gas_price=%s",
                tx_hash,
                block_number,
                wei_to_coin(int(state["value"])),
                gas_used,
                effective_gas_price,
            )
            self.state.clear()
            return True
        self.state.clear()
        raise SweeperError(f"Transaction reverted: tx={tx_hash} block={block_number}")

    def _broadcast_current(self, state: dict[str, Any]) -> BroadcastResult:
        current = state["current"]
        result = self.rpc.broadcast_raw(
            decode_raw_transaction(current["raw_transaction"]),
            current["tx_hash"],
        )
        current["last_broadcast_at"] = time.time()
        current["broadcast_attempts"] = int(current.get("broadcast_attempts", 0)) + 1
        self.state.save(state)
        return result

    def _reconcile_nonce_consumed(self, state: dict[str, Any]) -> Optional[bool]:
        info = self._receipt_info(state)
        if info is not None:
            tx_hash, receipt, confirmations = info
            if confirmations >= self.cfg.min_confirmations:
                return self._handle_receipt(tx_hash, receipt, state)
            log.info(
                "Known transaction is mined but not final enough yet: tx=%s "
                "confirmations=%s/%s; preserving state and never replacing it",
                tx_hash,
                confirmations,
                self.cfg.min_confirmations,
            )
            return None

        latest, pending = self.rpc.nonce_counts(self.from_addr)
        nonce = int(state["nonce"])
        if latest > nonce:
            # Retry receipts once after observing the confirmed nonce. Different
            # RPCs can expose nonce advancement slightly before another endpoint
            # exposes the receipt.
            time.sleep(min(1.0, float(self.cfg.receipt_poll_sec)))
            info = self._receipt_info(state)
            if info is not None:
                tx_hash, receipt, confirmations = info
                if confirmations >= self.cfg.min_confirmations:
                    return self._handle_receipt(tx_hash, receipt, state)
                log.info(
                    "Receipt appeared during nonce reconciliation: tx=%s confirmations=%s/%s",
                    tx_hash,
                    confirmations,
                    self.cfg.min_confirmations,
                )
                return None
            raise StateError(
                f"Nonce {nonce} is confirmed as consumed, but no receipt for any known hash was "
                "found. A conflicting transaction may have replaced this sweeper transaction. "
                f"Known hashes: {self._all_hashes(state)}"
            )
        if pending > nonce:
            log.warning(
                "Nonce %s is pending according to at least one RPC; keeping state and monitoring",
                nonce,
            )
        return None

    def _wait_for_receipt(self, state: dict[str, Any], timeout: int) -> Optional[tuple[str, Any]]:
        deadline = time.monotonic() + timeout
        while not SHUTDOWN and time.monotonic() < deadline:
            receipt = self._confirmed_receipt(state)
            if receipt is not None:
                return receipt
            time.sleep(self.cfg.receipt_poll_sec + random.uniform(0.0, 1.0))
        return None

    def _replace(self, state: dict[str, Any]) -> dict[str, Any]:
        count = int(state["replacement_count"])
        if count >= self.cfg.max_replacements:
            raise TimeoutError(
                f"Transaction remains pending and MAX_REPLACEMENTS={self.cfg.max_replacements} "
                f"was reached. State preserved at {self.cfg.state_file}."
            )

        replacement_tx = self._build_replacement_tx(state)
        old_current = state["current"]
        new_current = self._sign(replacement_tx)
        history_entry = dict(old_current)
        history_entry["replaced_at"] = time.time()
        state["history"].append(history_entry)
        state["current"] = new_current
        state["replacement_count"] = count + 1
        self.state.save(state)  # persist signed tx before any broadcast

        log.warning(
            "Prepared replacement: old=%s new=%s nonce=%s count=%s",
            old_current["tx_hash"],
            new_current["tx_hash"],
            state["nonce"],
            state["replacement_count"],
        )
        self._print_summary(state, "Replacement:")
        result = self._broadcast_current(state)
        if result.replacement_underpriced and not result.accepted:
            log.warning("Replacement was underpriced on all responding RPCs; another bump will follow")
        if result.nonce_too_low and not result.accepted:
            self._reconcile_nonce_consumed(state)
        return state

    def _monitor(self, state: dict[str, Any]) -> bool:
        self._validate_state(state)
        self._print_summary(state, "Recovered pending state:")

        if self.cfg.dry_run:
            log.warning("DRY_RUN=true: existing live state will not be broadcast or changed")
            return False

        while not SHUTDOWN:
            info = self._receipt_info(state)
            if info is not None:
                tx_hash, receipt, confirmations = info
                if confirmations >= self.cfg.min_confirmations:
                    return self._handle_receipt(tx_hash, receipt, state)

                # Once any known replacement is mined, never broadcast or replace
                # another transaction with this nonce while finality is pending.
                log.info(
                    "Mined tx awaiting confirmations: tx=%s confirmations=%s/%s",
                    tx_hash,
                    confirmations,
                    self.cfg.min_confirmations,
                )
                time.sleep(self.cfg.receipt_poll_sec + random.uniform(0.0, 1.0))
                continue

            self._reconcile_nonce_consumed(state)
            current = state["current"]
            since_broadcast = time.time() - float(current.get("last_broadcast_at", 0) or 0)
            if since_broadcast >= self.cfg.rebroadcast_every_sec:
                log.info("Rebroadcasting current tx: %s", current["tx_hash"])
                result = self._broadcast_current(state)
                if result.nonce_too_low and not result.accepted:
                    self._reconcile_nonce_consumed(state)

            receipt = self._wait_for_receipt(state, self.cfg.receipt_timeout_sec)
            if receipt is not None:
                return self._handle_receipt(receipt[0], receipt[1], state)

            # A tx may have become mined during the wait without yet reaching
            # MIN_CONFIRMATIONS. In that case replacement would be unsafe.
            info = self._receipt_info(state)
            if info is not None:
                tx_hash, _, confirmations = info
                log.info(
                    "Not replacing mined tx=%s while confirmations=%s/%s",
                    tx_hash,
                    confirmations,
                    self.cfg.min_confirmations,
                )
                continue

            state = self._replace(state)

        log.warning("Stopped with recoverable state preserved at %s", self.cfg.state_file)
        return False

    def _prepare_new_state(self) -> dict[str, Any]:
        latest, pending = self.rpc.nonce_counts(self.from_addr)
        if pending > latest and not self.cfg.allow_unknown_pending:
            raise UnknownPendingTransaction(
                f"Unknown pending transaction detected: latest_nonce={latest}, "
                f"pending_nonce={pending}. No matching sweeper state exists. Resolve it first, "
                "or set ALLOW_UNKNOWN_PENDING=true only when intentionally queuing after it."
            )
        nonce = pending
        return self._new_state(self._build_new_tx(nonce))

    def run(self) -> None:
        log.warning("DRY_RUN=%s", self.cfg.dry_run)
        for round_number in range(1, self.cfg.max_sweep_rounds + 1):
            if SHUTDOWN:
                break
            log.info("Sweep round %s/%s", round_number, self.cfg.max_sweep_rounds)

            existing = self.state.load()
            if existing is not None:
                if not self._monitor(existing):
                    return
                continue

            try:
                state = self._prepare_new_state()
            except NoSweepableBalance as exc:
                log.info("%s", exc)
                return

            self._print_summary(state, "Prepared:")
            if self.cfg.dry_run:
                log.warning("DRY_RUN=true: transaction was not saved and not broadcast")
                return

            # Save before broadcast so a crash cannot create an unknown pending tx.
            self.state.save(state)
            result = self._broadcast_current(state)
            if result.nonce_too_low and not result.accepted:
                self._reconcile_nonce_consumed(state)
            if not self._monitor(state):
                return

        log.info("Done")


def main() -> int:
    cfg = Config.load()
    with ProcessLock(cfg.lock_file):
        Sweeper(cfg).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        shutdown_handler()
        raise SystemExit(130)
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        raise SystemExit(1)
