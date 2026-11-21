"""Verify optional access limits, daily heavy quota, expiration, and live-eval lock."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from shipping_billing_agent.api import security
from shipping_billing_agent.api.app import create_app
from shipping_billing_agent.api.security import ApiSettings
from shipping_billing_agent.domain.models import OcrStructuredPayload
from shipping_billing_agent.services import BatchService

API = "/api/v1"


class _OfflineOcr:
    def recognize_image_bytes(self, content: bytes, _filename: str) -> tuple[str, float]:
        return content.decode(), 0.99

    def recognize_url(self, _url: str) -> SimpleNamespace:
        return SimpleNamespace(
            payload=OcrStructuredPayload(items=[], has_charges=False, has_metadata=True),
            formatted_text="Invoice No. INV-1\nTotal USD 10.00",
        )


def _api_settings(tmp_path: Path, **overrides: object) -> ApiSettings:
    defaults: dict[str, object] = {
        "app_data_dir": tmp_path,
        "sqlite_db_path": tmp_path / "test.sqlite",
        "public_relay_base_url": "",
        "llm_api_key": "",
        "deepseek_api_key": "",
        "allowed_origins": (),
        "rate_limit_per_minute": 0,
        "heavy_rate_limit_per_minute": 0,
        "daily_heavy_quota": 0,
        "max_concurrent_heavy": 0,
        "disable_live_eval": False,
        "access_expires_at": "",
        "trust_proxy_headers": False,
    }
    defaults.update(overrides)
    return ApiSettings(**defaults)


def _make_service(tmp_path: Path, settings: ApiSettings) -> BatchService:
    ocr = _OfflineOcr()
    return BatchService(settings, remote_ocr=ocr, local_ocr=ocr, extractor_mode="rule_only")


def test_default_settings_allow_high_frequency_requests(tmp_path: Path) -> None:
    settings = _api_settings(tmp_path)
    service = _make_service(tmp_path, settings)
    try:
        with TestClient(create_app(settings=settings, batch_service=service)) as client:
            for _ in range(15):
                assert client.get(f"{API}/health").status_code == 200
                assert client.get(f"{API}/batches").status_code == 200
            for _ in range(4):
                assert client.post(f"{API}/batches/demo", json={}).status_code == 201
    finally:
        service.close()


def test_access_expiration_blocks_after_deadline_and_validates_format(
    tmp_path: Path, monkeypatch
) -> None:
    bad = _api_settings(tmp_path, access_expires_at="not-an-iso-timestamp")
    try:
        create_app(settings=bad)
    except ValueError as exc:
        assert "APP_ACCESS_EXPIRES_AT" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid APP_ACCESS_EXPIRES_AT")

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>Workbench</title>", encoding="utf-8")

    now = [datetime(2026, 11, 21, 10, 0, tzinfo=UTC)]
    monkeypatch.setattr(security, "_utc_now", lambda: now[0])

    settings = _api_settings(tmp_path, access_expires_at="2026-11-21T18:30:00+08:00")
    service = _make_service(tmp_path, settings)
    try:
        app = create_app(settings=settings, batch_service=service, frontend_dir=dist)
        with TestClient(app) as client:
            assert client.get(f"{API}/batches").status_code == 200
            assert client.get("/").status_code == 200

            now[0] = datetime(2026, 11, 21, 10, 30, tzinfo=UTC)
            api_resp = client.get(f"{API}/batches")
            assert api_resp.status_code == 403
            assert api_resp.json()["code"] == "ACCESS_EXPIRED"
            assert api_resp.headers["cache-control"] == "no-store"

            for doc_path in ("/docs", "/redoc", "/openapi.json"):
                doc_resp = client.get(doc_path)
                assert doc_resp.status_code == 403
                assert doc_resp.json()["code"] == "ACCESS_EXPIRED"

            page_resp = client.get("/")
            assert page_resp.status_code == 403
            assert "临时演示访问已到期" in page_resp.text
            assert page_resp.headers["cache-control"] == "no-store"
            assert "noindex" in page_resp.headers["x-robots-tag"]

            health = client.get(f"{API}/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
    finally:
        service.close()


def test_general_rate_limit_blocks_with_retry_after_and_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(security, "_monotonic", lambda: clock[0])

    settings = _api_settings(tmp_path, rate_limit_per_minute=3)
    service = _make_service(tmp_path, settings)
    try:
        with TestClient(create_app(settings=settings, batch_service=service)) as client:
            for _ in range(3):
                assert client.get(f"{API}/batches").status_code == 200

            blocked = client.get(f"{API}/batches")
            assert blocked.status_code == 429
            assert blocked.json()["code"] == "RATE_LIMITED"
            assert blocked.headers["retry-after"] == "60"

            clock[0] = 130.0
            blocked_mid = client.get(f"{API}/batches")
            assert blocked_mid.status_code == 429
            assert blocked_mid.headers["retry-after"] == "30"
            assert client.get(f"{API}/health").status_code == 200

            clock[0] = 160.1
            assert client.get(f"{API}/batches").status_code == 200
    finally:
        service.close()


def test_heavy_rate_limit_targets_only_heavy_posts(tmp_path: Path, monkeypatch) -> None:
    clock = [200.0]
    monkeypatch.setattr(security, "_monotonic", lambda: clock[0])

    settings = _api_settings(tmp_path, heavy_rate_limit_per_minute=2)
    service = _make_service(tmp_path, settings)
    try:
        with TestClient(create_app(settings=settings, batch_service=service)) as client:
            first = client.post(f"{API}/batches/demo", json={})
            second = client.post(f"{API}/batches/demo", json={})
            assert first.status_code == 201
            assert second.status_code == 201
            batch_id = first.json()["id"]

            blocked_demo = client.post(f"{API}/batches/demo", json={})
            assert blocked_demo.status_code == 429
            assert blocked_demo.json()["code"] == "RATE_LIMITED"
            assert blocked_demo.headers["retry-after"] == "60"

            blocked_eval = client.post(
                f"{API}/eval-runs", json={"run_mode": "OFFLINE_DETERMINISTIC", "k_trials": 1}
            )
            assert blocked_eval.status_code == 429
            assert blocked_eval.json()["code"] == "RATE_LIMITED"

            assert client.get(f"{API}/batches").status_code == 200
            assert client.get(f"{API}/batches/{batch_id}/export").status_code == 200
            assert (
                client.post(f"{API}/entities", json={"canonical_name": "TEST CO LTD"}).status_code
                == 201
            )
    finally:
        service.close()


def test_daily_heavy_quota_resets_daily_and_skips_forbidden_origin(
    tmp_path: Path, monkeypatch
) -> None:
    now = [datetime(2026, 11, 21, 23, 55, tzinfo=UTC)]
    monkeypatch.setattr(security, "_utc_now", lambda: now[0])

    settings = _api_settings(tmp_path, daily_heavy_quota=2)
    service = _make_service(tmp_path, settings)
    try:
        with TestClient(create_app(settings=settings, batch_service=service)) as client:
            forged = client.post(
                f"{API}/batches/demo",
                json={},
                headers={"Origin": "https://evil.example.com"},
            )
            assert forged.status_code == 403
            assert forged.json()["code"] == "FORBIDDEN_ORIGIN"

            assert client.post(f"{API}/batches/demo", json={}).status_code == 201
            assert client.post(f"{API}/batches/demo", json={}).status_code == 201

            exhausted = client.post(f"{API}/batches/demo", json={})
            assert exhausted.status_code == 429
            assert exhausted.json()["code"] == "DAILY_QUOTA_EXCEEDED"

            now[0] = now[0] + timedelta(minutes=10)
            assert client.post(f"{API}/batches/demo", json={}).status_code == 201
    finally:
        service.close()


def test_max_concurrent_heavy_rejects_overlap_and_releases_slot(tmp_path: Path) -> None:
    async def exercise() -> None:
        settings = _api_settings(tmp_path, max_concurrent_heavy=1)
        service = _make_service(tmp_path, settings)
        entered = asyncio.Event()
        release = asyncio.Event()
        should_fail = False

        async def stub_eval_runner(**options: object) -> dict[str, object]:
            entered.set()
            await release.wait()
            if should_fail:
                raise RuntimeError("simulated runner failure")
            return {
                "run_mode": options["run_mode"],
                "k_trials": options["k_trials"],
                "status": "COMPLETED",
                "model_name": "rule_only",
                "actual_trials": 1,
                "total_cases": 1,
                "report": {},
            }

        try:
            app = create_app(settings=settings, batch_service=service, eval_runner=stub_eval_runner)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                task1 = asyncio.create_task(
                    client.post(
                        f"{API}/eval-runs",
                        json={"run_mode": "OFFLINE_DETERMINISTIC", "k_trials": 1},
                    )
                )
                await entered.wait()

                concurrent = await client.post(f"{API}/batches/demo", json={})
                assert concurrent.status_code == 429
                assert concurrent.json()["code"] == "TOO_MANY_CONCURRENT_JOBS"
                assert concurrent.headers["retry-after"] == "5"

                release.set()
                resp1 = await task1
                assert resp1.status_code == 201

                entered.clear()
                release.clear()
                should_fail = True
                failing = asyncio.create_task(
                    client.post(
                        f"{API}/eval-runs",
                        json={"run_mode": "OFFLINE_DETERMINISTIC", "k_trials": 1},
                    )
                )
                await entered.wait()
                release.set()
                failed_resp = await failing
                assert failed_resp.status_code == 500

                after_error = await client.post(f"{API}/batches/demo", json={})
                assert after_error.status_code == 201
        finally:
            service.close()

    asyncio.run(exercise())


def test_disable_live_eval_blocks_live_modes_only(tmp_path: Path) -> None:
    calls: list[str] = []

    def stub_runner(**options: object) -> dict[str, object]:
        mode = str(options["run_mode"])
        calls.append(mode)
        return {
            "run_mode": mode,
            "k_trials": options["k_trials"],
            "status": "COMPLETED",
            "model_name": "rule_only",
            "actual_trials": 1,
            "total_cases": 1,
            "report": {},
        }

    settings = _api_settings(tmp_path, disable_live_eval=True)
    service = _make_service(tmp_path, settings)
    try:
        app = create_app(settings=settings, batch_service=service, eval_runner=stub_runner)
        with TestClient(app) as client:
            for live_mode in ("LIVE_LLM", "LIVE_E2E"):
                resp = client.post(
                    f"{API}/eval-runs", json={"run_mode": live_mode, "k_trials": 1}
                )
                assert resp.status_code == 403
                assert resp.json()["code"] == "LIVE_EVAL_DISABLED"

            offline = client.post(
                f"{API}/eval-runs", json={"run_mode": "OFFLINE_DETERMINISTIC", "k_trials": 1}
            )
            assert offline.status_code == 201
            assert calls == ["OFFLINE_DETERMINISTIC"]
    finally:
        service.close()


def test_trust_proxy_headers_controls_x_real_ip_bucketing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(security, "_monotonic", lambda: 500.0)

    trusted_dir = tmp_path / "trusted"
    trusted_settings = _api_settings(
        trusted_dir, rate_limit_per_minute=1, trust_proxy_headers=True
    )
    trusted_service = _make_service(trusted_dir, trusted_settings)
    try:
        with TestClient(
            create_app(settings=trusted_settings, batch_service=trusted_service)
        ) as client:
            assert (
                client.get(f"{API}/batches", headers={"X-Real-IP": "203.0.113.10"}).status_code
                == 200
            )
            assert (
                client.get(f"{API}/batches", headers={"X-Real-IP": "203.0.113.10"}).status_code
                == 429
            )
            assert (
                client.get(f"{API}/batches", headers={"X-Real-IP": "198.51.100.20"}).status_code
                == 200
            )
    finally:
        trusted_service.close()

    untrusted_dir = tmp_path / "untrusted"
    untrusted_settings = _api_settings(
        untrusted_dir, rate_limit_per_minute=1, trust_proxy_headers=False
    )
    untrusted_service = _make_service(untrusted_dir, untrusted_settings)
    try:
        with TestClient(
            create_app(settings=untrusted_settings, batch_service=untrusted_service)
        ) as client:
            assert (
                client.get(f"{API}/batches", headers={"X-Real-IP": "203.0.113.10"}).status_code
                == 200
            )
            assert (
                client.get(f"{API}/batches", headers={"X-Real-IP": "198.51.100.20"}).status_code
                == 429
            )
    finally:
        untrusted_service.close()
