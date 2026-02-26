"""Encrypt Binance API credentials into keys.json.enc using OpenSSL AES-256-CBC."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import tempfile


def encrypt_payload(payload: dict[str, str], passphrase: str, output_path: str) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as plain_file:
        json.dump(payload, plain_file)
        plain_path = plain_file.name

    with tempfile.NamedTemporaryFile("w", delete=False) as pass_file:
        pass_file.write(passphrase)
        pass_path = pass_file.name

    try:
        subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-in",
                plain_path,
                "-out",
                output_path,
                "-pass",
                f"file:{pass_path}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        os.remove(plain_path)
        os.remove(pass_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Encrypt Binance credentials")
    parser.add_argument("--output", default="keys.json.enc")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--password", default="")
    parser.add_argument("--testnet", action="store_true")
    args = parser.parse_args(argv)

    passphrase = getpass.getpass("Passphrase: ")
    confirm = getpass.getpass("Confirm passphrase: ")
    if passphrase != confirm:
        raise ValueError("Passphrases do not match")

    payload = {
        "apiKey": args.api_key,
        "secret": args.secret,
        "password": args.password,
        "testnet": args.testnet,
    }

    encrypt_payload(payload, passphrase, args.output)
    print(f"Encrypted keys saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
