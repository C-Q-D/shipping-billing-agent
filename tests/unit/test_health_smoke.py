"""Smoke tests for the health response and runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shipping_billing_agent.api.app import create_app
from shipping_billing_agent.config.settings import load_settings
from shipping_billing_agent.gateways.llm_client import (
    LlmNotConfiguredError,
    OpenAICompatibleLlmClient,
)


@pytest.fixture
def health_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    settings_prefixes = (
        "APP_",
        "ALLOW_REMOTE_BIND",
        "DEEPSEEK_",
        "LLM_",
        "OCR_",
        "PUBLIC_RELAY_",
        "RELAY_PRESIGN_",
        "SQLITE_",
    )
    for name in tuple(os.environ):
        if name.upper().startswith(settings_prefixes):
            monkeypatch.delenv(name)
    with TestClient(create_app()) as client:
        yield client


def test_health_endpoint_contract(health_client: TestClient) -> None:
    response = health_client.get("/api/v1/health")
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
    assert payload["llm_configured"] is False
    assert payload["llm_model"] == "deepseek-flash"
    assert payload["ocr_gateway_url"] == "https://gateway.pltplt.com/ai/process"
    assert payload["ocr_direct_mode"] is True
    assert payload["public_relay_configured"] is False


@pytest.mark.parametrize("base_url", ["https://example.com/v1", "http://api.deepseek.com/v1"])
def test_health_deepseek_key_requires_https_and_exact_host(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("LLM_BASE_URL", base_url)

    payload = health_client.get("/api/v1/health").json()
    assert payload["llm_configured"] is False

    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    payload_deepseek = health_client.get("/api/v1/health").json()
    assert payload_deepseek["llm_configured"] is True


@pytest.mark.parametrize(
    ("base_url", "key"),
    [
        ("https://other.example/v1", "synthetic-deepseek-key"),
        ("https://user:pass@example.test/v1", "synthetic-provider-key"),
        ("https://@example.test/v1", "synthetic-provider-key"),
        ("https://example.test:70000/v1", "synthetic-provider-key"),
        ("https://exa mple.test/v1", "synthetic-provider-key"),
        ("https:///v1", "synthetic-provider-key"),
        ("ftp://example.test/v1", "synthetic-provider-key"),
    ],
)
def test_health_and_llm_client_reject_the_same_invalid_credentials(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch, base_url: str, key: str
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-deepseek-key")
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("LLM_BASE_URL", base_url)

    assert health_client.get("/api/v1/health").json()["llm_configured"] is False
    with pytest.raises(LlmNotConfiguredError):
        OpenAICompatibleLlmClient(load_settings())


def test_health_reads_dotenv_and_observes_environment_override(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "LLM_MODEL=快照模型\n"
        "DEEPSEEK_API_KEY=test-deepseek-key\n"
        "OCR_GATEWAY_URL=https://ocr.example.com/process\n"
        "OCR_PROXY_URL=http://ocr-proxy.example.com:8080\n"
        "UNRELATED_FLAG=ignored\n",
        encoding="utf-8",
    )

    response = health_client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["llm_model"] == "快照模型"
    assert payload["llm_configured"] is True
    assert payload["ocr_gateway_url"] == "https://ocr.example.com/process"
    assert payload["ocr_direct_mode"] is False

    monkeypatch.setenv("LLM_MODEL", "override-model")
    monkeypatch.setenv("OCR_PROXY_URL", "")
    overridden = health_client.get("/api/v1/health").json()
    assert overridden["llm_model"] == "override-model"
    assert overridden["ocr_direct_mode"] is True


def test_health_relay_url_alone_does_not_advertise_ready(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PUBLIC_RELAY_BASE_URL", "https://relay.example.com")

    payload = health_client.get("/api/v1/health").json()

    assert payload["public_relay_configured"] is False


def test_health_reports_registered_relay_as_ready(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RelayPublisher:
        def publish(self, _content: bytes, _filename: str, *, ttl_seconds: int) -> str:
            pytest.fail("Health checks must not publish files")

    monkeypatch.setenv("PUBLIC_RELAY_BASE_URL", "https://relay.example.com")
    with TestClient(create_app(relay_publisher=RelayPublisher())) as client:
        assert client.get("/api/v1/health").json()["public_relay_configured"] is True


def test_health_masks_ocr_gateway_query(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "OCR_GATEWAY_URL", "https://gateway.pltplt.com/ai/process?token=synthetic-gateway-token"
    )

    response = health_client.get("/api/v1/health")

    assert response.json()["ocr_gateway_url"] == "https://gateway.pltplt.com/ai/process?[REDACTED]"
    assert "synthetic-gateway-token" not in response.text
