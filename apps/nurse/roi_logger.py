"""Shadow ROI audit logger."""

from datetime import UTC, datetime
from typing import Any


class RoiLogger:
    """Async logger for audit_logs collection writes."""

    def __init__(self, collection: Any | None = None):
        self.collection = collection

    async def log_audit(self, document: dict[str, Any]) -> None:
        doc = dict(document)
        doc.setdefault("logged_at", datetime.now(UTC).isoformat())

        if self.collection is not None:
            await self.collection.insert_one(doc)
