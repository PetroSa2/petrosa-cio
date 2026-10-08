"""Confidence calibration report endpoint."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/calibration", tags=["calibration"])


@router.get("/confidence")
async def get_confidence_calibration(request: Request) -> dict:
    service = getattr(request.app.state, "confidence_calibration", None)
    if service is None:
        raise HTTPException(status_code=503, detail="confidence calibration unavailable")
    try:
        return await service.report()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="confidence calibration source unavailable") from exc
