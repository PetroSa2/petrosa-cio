"""Standalone emergency close-all script for Binance spot + futures.

This file intentionally avoids any internal petrosa imports.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CloseAction:
    market: str
    symbol: str
    side: str
    amount: float


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emergency close-all command")
    parser.add_argument(
        "--keys-file",
        default="keys.json.enc",
        help="Path to encrypted credentials file",
    )
    parser.add_argument(
        "--passphrase-env",
        default="NUCLEAR_PASSPHRASE",
        help="Environment variable containing decryption passphrase",
    )
    parser.add_argument(
        "--rate-limit-ms",
        type=int,
        default=200,
        help="Delay between order submissions",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Orders to submit per batch before sleeping",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="List actions without placing orders (default behavior)",
    )
    parser.add_argument(
        "--force-execute",
        action="store_true",
        help="Actually place close-all market orders",
    )
    return parser.parse_args(argv)


def decrypt_keys(keys_file: str, passphrase: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", delete=False) as pass_file:
        pass_file.write(passphrase)
        pass_file_path = pass_file.name

    try:
        result = subprocess.run(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-in",
                keys_file,
                "-pass",
                f"file:{pass_file_path}",
            ],
            capture_output=True,
            check=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError("Failed to decrypt keys.json.enc") from exc
    finally:
        os.remove(pass_file_path)

    return json.loads(result.stdout)


def build_exchange(credentials: dict[str, Any], market: str):
    import ccxt  # Imported lazily so dry unit tests do not require exchange setup.

    options = {
        "apiKey": credentials["apiKey"],
        "secret": credentials["secret"],
        "enableRateLimit": True,
    }

    if credentials.get("password"):
        options["password"] = credentials["password"]

    if market == "futures":
        options["options"] = {"defaultType": "future"}

    exchange = ccxt.binance(options)
    if credentials.get("testnet"):
        exchange.set_sandbox_mode(True)
    return exchange


def fetch_all_positions(spot_exchange: Any, futures_exchange: Any) -> list[CloseAction]:
    actions: list[CloseAction] = []

    balances = spot_exchange.fetch_balance()
    markets = spot_exchange.load_markets()
    for asset, amount_info in balances.get("total", {}).items():
        amount = float(amount_info or 0)
        if amount <= 0:
            continue
        symbol = f"{asset}/USDT"
        if symbol not in markets or asset == "USDT":
            continue
        actions.append(
            CloseAction(market="spot", symbol=symbol, side="sell", amount=amount)
        )

    futures_positions = futures_exchange.fetch_positions()
    for pos in futures_positions:
        contracts = float(pos.get("contracts") or 0)
        if contracts == 0:
            continue
        symbol = pos["symbol"]
        side = "sell" if contracts > 0 else "buy"
        actions.append(
            CloseAction(
                market="futures",
                symbol=symbol,
                side=side,
                amount=abs(contracts),
            )
        )

    return actions


def market_close_all(
    actions: list[CloseAction],
    *,
    spot_exchange: Any,
    futures_exchange: Any,
    dry_run: bool,
    batch_size: int,
    rate_limit_ms: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for idx, action in enumerate(actions, start=1):
        exchange = spot_exchange if action.market == "spot" else futures_exchange

        if dry_run:
            results.append(
                {
                    "status": "dry_run",
                    "market": action.market,
                    "symbol": action.symbol,
                    "side": action.side,
                    "amount": action.amount,
                }
            )
        else:
            order = exchange.create_order(
                action.symbol,
                "market",
                action.side,
                action.amount,
            )
            results.append(
                {
                    "status": "executed",
                    "market": action.market,
                    "symbol": action.symbol,
                    "side": action.side,
                    "amount": action.amount,
                    "order_id": order.get("id"),
                }
            )

        if idx % max(batch_size, 1) == 0:
            time.sleep(max(rate_limit_ms, 0) / 1000.0)

    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    execute = args.force_execute
    dry_run = not execute

    passphrase = os.getenv(args.passphrase_env)
    if not passphrase:
        print(
            f"Missing passphrase env var: {args.passphrase_env}",
            file=sys.stderr,
        )
        return 2

    credentials = decrypt_keys(args.keys_file, passphrase)
    spot_exchange = build_exchange(credentials, market="spot")
    futures_exchange = build_exchange(credentials, market="futures")

    actions = fetch_all_positions(spot_exchange, futures_exchange)
    print(f"Discovered {len(actions)} close actions")

    results = market_close_all(
        actions,
        spot_exchange=spot_exchange,
        futures_exchange=futures_exchange,
        dry_run=dry_run,
        batch_size=args.batch_size,
        rate_limit_ms=args.rate_limit_ms,
    )

    print(json.dumps(results, indent=2))

    if dry_run:
        print("Dry-run mode completed. Use --force-execute to place orders.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
