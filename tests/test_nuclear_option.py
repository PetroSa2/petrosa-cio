"""Tests for standalone nuclear option canary scripts."""

import json
import os
import subprocess
import tempfile

import pytest

from canary.encrypt_keys import encrypt_payload
from canary.nuclear_option import (
    CloseAction,
    decrypt_keys,
    fetch_all_positions,
    main,
    market_close_all,
)


class FakeExchange:
    def __init__(self, *, balances=None, markets=None, positions=None):
        self._balances = balances or {}
        self._markets = markets or {}
        self._positions = positions or []
        self.orders = []

    def fetch_balance(self):
        return {"total": self._balances}

    def load_markets(self):
        return self._markets

    def fetch_positions(self):
        return self._positions

    def create_order(self, symbol, order_type, side, amount):
        self.orders.append((symbol, order_type, side, amount))
        return {"id": f"order-{len(self.orders)}"}


def test_encrypt_decrypt_roundtrip_keys_file():
    payload = {"apiKey": "k", "secret": "s", "password": "p", "testnet": True}
    passphrase = "strong-passphrase"

    with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
        path = tmp.name

    try:
        encrypt_payload(payload, passphrase, path)
        decrypted = decrypt_keys(path, passphrase)
    finally:
        os.remove(path)

    assert decrypted == payload


def test_decrypt_keys_raises_on_invalid_ciphertext():
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write("not encrypted")
        path = tmp.name

    try:
        with pytest.raises(ValueError):
            decrypt_keys(path, "bad")
    finally:
        os.remove(path)


def test_fetch_all_positions_collects_spot_and_futures_actions():
    spot = FakeExchange(
        balances={"BTC": 0.5, "USDT": 1200, "ETH": 0.0},
        markets={"BTC/USDT": {}, "ETH/USDT": {}},
    )
    futures = FakeExchange(
        positions=[
            {"symbol": "BTC/USDT:USDT", "contracts": 2},
            {"symbol": "ETH/USDT:USDT", "contracts": -3},
            {"symbol": "SOL/USDT:USDT", "contracts": 0},
        ]
    )

    actions = fetch_all_positions(spot, futures)

    assert CloseAction("spot", "BTC/USDT", "sell", 0.5) in actions
    assert CloseAction("futures", "BTC/USDT:USDT", "sell", 2.0) in actions
    assert CloseAction("futures", "ETH/USDT:USDT", "buy", 3.0) in actions


def test_market_close_all_dry_run_default_no_orders():
    spot = FakeExchange()
    futures = FakeExchange()
    actions = [CloseAction("spot", "BTC/USDT", "sell", 1.0)]

    result = market_close_all(
        actions,
        spot_exchange=spot,
        futures_exchange=futures,
        dry_run=True,
        batch_size=1,
        rate_limit_ms=0,
    )

    assert result[0]["status"] == "dry_run"
    assert spot.orders == []


def test_market_close_all_force_execute_places_orders():
    spot = FakeExchange()
    futures = FakeExchange()
    actions = [
        CloseAction("spot", "BTC/USDT", "sell", 1.0),
        CloseAction("futures", "ETH/USDT:USDT", "buy", 2.0),
    ]

    result = market_close_all(
        actions,
        spot_exchange=spot,
        futures_exchange=futures,
        dry_run=False,
        batch_size=10,
        rate_limit_ms=0,
    )

    assert result[0]["status"] == "executed"
    assert result[1]["status"] == "executed"
    assert spot.orders == [("BTC/USDT", "market", "sell", 1.0)]
    assert futures.orders == [("ETH/USDT:USDT", "market", "buy", 2.0)]


def test_main_requires_passphrase_env(monkeypatch):
    monkeypatch.delenv("NUCLEAR_PASSPHRASE", raising=False)

    code = main(["--keys-file", "nonexistent.json.enc"])
    assert code == 2


def test_encrypt_payload_invokes_openssl(monkeypatch):
    captured = {}

    def fake_run(cmd, check, capture_output, text):
        captured["cmd"] = cmd
        assert check is True
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)

    with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
        out = tmp.name

    try:
        encrypt_payload({"apiKey": "k", "secret": "s"}, "pw", out)
    finally:
        os.remove(out)

    assert captured["cmd"][0] == "openssl"
    assert "-aes-256-cbc" in captured["cmd"]
