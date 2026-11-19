"""FastAPI application scaffold exposing the minimal /api/v1/health probe."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import FastAPI


def _is_llm_configured() -> bool:
    """Return True when LLM credentials match configured or DeepSeek default endpoint."""
    llm_api_key = os.getenv("LLM_API_KEY", "").strip()
    if llm_api_key:
        return True

    llm_base_url = os.getenv("LLM_BASE_URL", "").strip()
    is_deepseek_endpoint = not llm_base_url or (
        (urlparse(llm_base_url).hostname or "").lower() == "api.deepseek.com"
    )
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    return is_deepseek_endpoint and bool(deepseek_api_key)


def create_app() -> FastAPI:
    """Create minimal B00 FastAPI application instance."""
    fastapi_app = FastAPI(
        title="Shipping Intelligent Billing Assistant API",
        version="0.1.0",
    )

    @fastapi_app.get("/api/v1/health")
    def get_health() -> dict[str, object]:
        """Return runtime health and capability configuration flags."""
        llm_model = os.getenv("LLM_MODEL", "").strip() or "deepseek-flash"
        ocr_gateway_url = (
            os.getenv("OCR_GATEWAY_URL", "").strip() or "https://gateway.pltplt.com/ai/process"
        )
        ocr_trust_env = os.getenv("OCR_TRUST_ENV", "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        ocr_direct_mode = (not ocr_trust_env) and not bool(
            os.getenv("OCR_PROXY_URL", "").strip()
        )
        return {
            "status": "ok",
            "llm_configured": _is_llm_configured(),
            "llm_model": llm_model,
            "ocr_gateway_url": ocr_gateway_url,
            "ocr_direct_mode": ocr_direct_mode,
            "public_relay_configured": bool(os.getenv("PUBLIC_RELAY_BASE_URL", "").strip()),
        }

    return fastapi_app


app = create_app()
