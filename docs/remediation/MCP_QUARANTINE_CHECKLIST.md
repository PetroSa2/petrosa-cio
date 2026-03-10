Status: QUARANTINED — do not attempt to use MCPServer until all items below are resolved.

- [ ] ShadowROIEngine — source package apps.nurse does not exist in this repo. Needs to be either implemented here or sourced from the Nurse service. Current stub location: cio/stubs/roi_engine.py.
- [ ] VectorMemoryClient — does not exist anywhere in codebase. InstitutionalMemoryService in cio/memory.py depends on it. Interface mismatch with existing QdrantVectorClient — needs either an adapter or rewrite of cio/memory.py to use QdrantVectorClient directly.
- [ ] ConfigManager — imported from core.config_manager which does not resolve. Needs path audit — likely cio.core.config_manager or similar.
- [ ] discover_schema_models / generate_tools — imported from core.utils.schema_parser which does not resolve. Same path audit needed as ConfigManager.

Quarantine implemented: Fix 2c — 2026-03-10. Revisit after Fix 3 and Fix 4 are complete.
