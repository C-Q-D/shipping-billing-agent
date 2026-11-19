"""FastAPI application scaffold exposing the minimal /api/v1/health probe."""

from __future__ import annotations

from fastapi import FastAPI

from shipping_billing_agent.config.settings import load_settings


def create_app() -> FastAPI:
    """Create the FastAPI application instance."""
    fastapi_app = FastAPI(
        title="Shipping Intelligent Billing Assistant API",
        version="0.1.0",
    )

    @fastapi_app.get("/api/v1/health")
    def get_health() -> dict[str, object]:
        """Return runtime health and capability configuration flags."""
        settings = load_settings()
        return {
            "status": "ok",
            "llm_configured": settings.is_llm_configured(),
            "llm_model": settings.llm_model,
            "ocr_gateway_url": settings.ocr_gateway_url,
            "ocr_direct_mode": settings.is_ocr_direct_mode(),
            "public_relay_configured": settings.is_public_relay_ready(),
        }

    return fastapi_app


app = create_app()
