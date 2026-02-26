# Emergency Operations: Nuclear Option

## Purpose
`canary/nuclear_option.py` is a standalone emergency script to close all spot and USD-M futures positions when CIO/K8s/NATS are unavailable.

## Safety Model
- Default behavior is **dry-run**.
- Real execution requires explicit `--force-execute`.
- Credentials are read from encrypted `keys.json.enc`.
- Encryption/decryption uses OpenSSL AES-256-CBC (`openssl enc`).

## 1. Encrypt Keys Locally
```bash
python canary/encrypt_keys.py \
  --api-key "<BINANCE_API_KEY>" \
  --secret "<BINANCE_SECRET>" \
  --testnet \
  --output keys.json.enc
```

## 2. Dry-Run (Mandatory First Step)
```bash
export NUCLEAR_PASSPHRASE='<your-passphrase>'
python canary/nuclear_option.py --keys-file keys.json.enc
```

Expected behavior:
- Lists all spot and futures close actions.
- Does not submit orders.

## 3. Force Execute (Emergency Only)
```bash
export NUCLEAR_PASSPHRASE='<your-passphrase>'
python canary/nuclear_option.py \
  --keys-file keys.json.enc \
  --force-execute \
  --batch-size 5 \
  --rate-limit-ms 200
```

## Operational Notes
- Script has no internal `petrosa-*` imports.
- Exchange dependency: `ccxt` only.
- System dependency: `openssl` binary (for encrypted vault operations).
- Use Binance Testnet first before any production run.
