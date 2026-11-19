"""B00 smoke tests for /api/v1/health and credential isolation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from shipping_billing_agent.api.app import create_app


def test_health_endpoint_contract(monkeypatch) -> None:
    """Health endpoint returns all 6 required HealthStatus fields and types."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OCR_PROXY_URL", raising=False)
    monkeypatch.setenv("OCR_TRUST_ENV", "false")

    client = TestClient(create_app())
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()

    expected_keys = {
        "status",
        "llm_configured",
        "llm_model",
        "ocr_gateway_url",
        "ocr_direct_mode",
        "public_relay_configured",
    }
    assert set(payload.keys()) == expected_keys
    assert payload["status"] == "ok"
    assert isinstance(payload["llm_configured"], bool)
    assert payload["llm_configured"] is False
    assert isinstance(payload["llm_model"], str)
    assert payload["ocr_gateway_url"] == "https://gateway.pltplt.com/ai/process"
    assert payload["ocr_direct_mode"] is True
    assert payload["public_relay_configured"] is False


def test_health_credential_isolation_custom_base_url(monkeypatch) -> None:
    """DEEPSEEK_API_KEY must NOT mark custom third-party LLM_BASE_URL as configured."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")

    client = TestClient(create_app())
    payload = client.get("/api/v1/health").json()
    assert payload["llm_configured"] is False

    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    payload_deepseek = client.get("/api/v1/health").json()
    assert payload_deepseek["llm_configured"] is True
