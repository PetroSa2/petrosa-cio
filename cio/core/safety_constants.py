"""
Centralized safety constants for the Petrosa Strategist (CIO).
Changes to this file require MFA approval via GitHub branch protection.
"""

# Risk Guardrails
MAX_DRAWDOWN_PCT = 0.2
MAX_POSITION_SIZE_PCT = 0.1
VOLATILITY_SCALE_THRESHOLD = 0.03

# Network & Latency
HEARTBEAT_TIMEOUT_MS = 200
RETRY_SAFE_DELAY_MS = 1000

# Order Limits
MAX_ORDERS_GLOBAL = 100
MAX_ORDERS_PER_SYMBOL = 10
