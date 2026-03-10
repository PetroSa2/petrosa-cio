# Remediation Log & Current Status (PROJECT PACT)

**Date**: 2026-03-10  
**Current Phase**: **[READY FOR FIX 1]**  
**Related Spec**: [004-pact-rest-implementation.md](../architecture/004-pact-rest-implementation.md)

---

## 1. Executive Damage Assessment
This log tracks the "Archaeology" results performed to identify why the CIO Intelligence layer was non-functional in production.

| Component | Status | Finding | Action |
| :--- | :--- | :--- | :--- |
| **Output Router** | 🔴 BROKEN | Firing NATS events instead of synchronous REST. | Fix 1 (Immediate) |
| **NurseEnforcer** | 🔴 MISSING | Broken imports in `mcp_server.py`; code is a shell. | Fix 2 |
| **Auth Headers** | 🔴 MISSING | `httpx` clients have zero authentication. | Fix 3 |
| **Code Engine** | 🟡 PRIMITIVE | Kelly (1/4 cap) and SL/TP are static lookups. | Fix 4 |
| **Model Pinning** | 🟡 WEAK | Versions are defaulting to env vars in the code. | Fix 5 |

## 2. The 5-Step Fix Plan
One piece at a time. No step begins until the previous one's declaration is approved and merged.

### **Fix 1: Output Router REST Transition**
- **Trigger**: Replace NATS fire-and-forget on `MODIFY_PARAMS` and `PAUSE_STRATEGY`.
- **Logic**: Use `httpx.AsyncClient` + `POST /api/v1/config`.
- **Failure**: Log `FAILED_TO_APPLY` to Vector DB if non-200 received.

### **Fix 2: NurseEnforcer Stabilization**
- Remove broken `ShadowROIEngine` import from `mcp_server.py`.
- Consolidate what exists on the `origin/cio-enforcer` branch.

### **Fix 3: Internal API Auth**
- Implement shared internal token header across all `httpx` fetches.

### **Fix 5: Model Pinning**
- Consolidate LLM model strings into a versioned constant file.

## 3. Pre-Implementation Checklist (APPROVED)
- **Strategy Service Mapping**: Resolved (Realtime-Strategies Registry vs TA-Bot fallback).
- **Audit Consistency**: Parameter freeze only on REST success. Cache failure is warn-only.
- **Header Structure**: `X-Petrosa-Issuer: CIO`.

---

*This document serves as the transient source of truth for handover between developers and AI agents.*
