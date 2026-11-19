"""Verify gateway behavior with synthetic documents and loopback-only HTTP."""

from __future__ import annotations

import json
import logging
import socket
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread

import httpx
import pytest
from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith.run_trees import RunTree
from langsmith.utils import get_env_var
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from shipping_billing_agent.config.settings import AppSettings
from shipping_billing_agent.gateways import llm_client, local_ocr, ocr_client
from shipping_billing_agent.gateways.text_sanitizer import sanitize_for_llm
from shipping_billing_agent.gateways.url_guard import (
    SsrfBlockedError,
    redact_url_query,
    validate_public_url,
)

PUBLIC_FILE = "https://93.184.216.34/invoice.pdf?Signature=test-signature-value"
LLM_KEY = "synthetic-gateway-test-key"
DEEPSEEK_KEY = "synthetic-deepseek-fallback-key"
OCR_BANK_FIELDS = {
    "accountNo": "123456789012",
    "routing_number": "021000021",
    "ach": "026009593",
    "swiftCode": "BOFAUS3NXXX",
    "iban": "GB82WEST12345698765432",
}


def _unexpected_dns(host: str, port: int) -> list[tuple]:
    raise AssertionError(f"Unexpected DNS lookup for {host}:{port}")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://files.example/invoice.pdf",
        "https://user@files.example/invoice.pdf",
        "https://user:pass@files.example/invoice.pdf",
        "https://localhost./invoice.pdf",
        "https://files.LOCALHOST/invoice.pdf",
        "https://files.local/invoice.pdf",
        "https://files.internal/invoice.pdf",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.0.1/",
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://169.254.1.2/",
        "http://100.64.0.1/",
        "http://100.127.255.254/",
        "http://224.1.2.3/",
        "http://240.1.2.3/",
        "http://0.0.0.0/",
        "http://[::]/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[fec0::1]/",
        "http://[ff02::1]/",
        "http://[::ffff:192.168.1.1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:10.0.0.1]/",
    ],
)
def test_public_url_rejects_disallowed_schemes_hosts_and_addresses(url: str) -> None:
    with pytest.raises(SsrfBlockedError) as error:
        validate_public_url(url, resolver=_unexpected_dns)

    assert error.value.code == "SSRF_BLOCKED"


def _ipv4_answer(address: str, port: int = 443) -> tuple:
    return socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port)


@pytest.mark.parametrize(
    "answers",
    [[], [_ipv4_answer("93.184.216.34"), _ipv4_answer("10.30.1.2")]],
)
def test_public_url_rejects_empty_dns_or_any_nonpublic_answer(answers: list[tuple]) -> None:
    with pytest.raises(SsrfBlockedError, match="DNS|公网"):
        validate_public_url("https://files.example:443/bill", resolver=lambda *_: answers)


def test_public_url_preserves_public_presigned_url() -> None:
    url = "https://files.example:8443/invoice.pdf?Signature=a%2Bb&Key=01"

    def resolve(host: str, port: int) -> list[tuple]:
        assert (host, port) == ("files.example", 8443)
        ipv6 = (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:4700::1111", port, 0, 0))
        return [_ipv4_answer("93.184.216.34", port), ipv6]

    assert validate_public_url(f"  {url}  ", resolver=resolve) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/a.pdf",
        "https://[2606:4700::1111]/a.pdf",
        "http://[::ffff:93.184.216.34]/a.pdf",
    ],
)
def test_public_ip_literal_needs_no_dns_lookup(url: str) -> None:
    assert validate_public_url(url, resolver=_unexpected_dns) == url


def test_redact_url_query_hides_values_but_keeps_url_path() -> None:
    redacted = redact_url_query(PUBLIC_FILE)

    assert redacted.startswith("https://93.184.216.34/invoice.pdf")
    assert "test-signature-value" not in redacted
    assert redact_url_query("https://files.example/a.pdf") == "https://files.example/a.pdf"


def test_sanitizer_removes_bank_details_and_preserves_shipping_fields() -> None:
    business_details = (
        "Invoice No: DC-108478\n"
        "Bill of Lading: ORFR1618731; Booking: 9023456701; Container: MSKU1234567\n"
        "Shipper: Pacific Account Services Ltd.; Consignee: Example Bank Shipping Ltd.\n"
        "Date: 2026-02-04; Total USD 1,310.40; Rate CNY 65.00\n"
    )
    bank_details = (
        "Account No: 123456789012; Routing Number: 021000021; ABA: 026009593\n"
        "SWIFT/BIC: BOFAUS3NXXX; IBAN: GB82 WEST 1234 5698 7654 32 USD\n"
        "IBAN No.: AT61 1904 3002 3457 3201 Rate USD 10.00\n"
        "Routing No.: 021000021 1310.40 USD\n"
        "Remit To: Harbor Bank\n"
        "4000 5678 9012\n"
        "Invoice No:\n"
        "968523147002\n"
    )

    assert sanitize_for_llm(business_details + bank_details) == business_details + (
        "Account No: [REDACTED_BANK_ACCT]; Routing Number: [REDACTED_BANK_ACCT]; "
        "ABA: [REDACTED_BANK_ACCT]\n"
        "SWIFT/BIC: [REDACTED_BANK_ACCT]; IBAN: [REDACTED_BANK_ACCT] USD\n"
        "IBAN No.: [REDACTED_BANK_ACCT] Rate USD 10.00\n"
        "Routing No.: [REDACTED_BANK_ACCT] 1310.40 USD\n"
        "Remit To: Harbor Bank\n"
        "[REDACTED_BANK_ACCT]\n"
        "Invoice No:\n"
        "968523147002\n"
    )


def test_sanitizer_hides_presigned_credentials_and_keeps_invoice_query() -> None:
    text = (
        "Invoice link: https://files.example/scan?"
        "X-Amz-Signature=secret-signature&%74oken=secret-token&invoice=DC-108478"
    )

    sanitized = sanitize_for_llm(text)

    assert "secret-signature" not in sanitized
    assert "secret-token" not in sanitized
    assert "invoice=DC-108478" in sanitized
    assert sanitize_for_llm(sanitized) == sanitized


@pytest.mark.parametrize(
    "label",
    [
        "ABA #",
        "ABA/ROUTING WIRE#:",
        "ACH:",
        "WIRE / ACH ##:",
        "ACCT / ACCOUNT #:",
        "Account Number",
        "ROUTING: / WIRE #",
    ],
)
def test_sanitizer_masks_pdf_remittance_label_variations(label: str) -> None:
    text = f"{label} 012345678\n245.0000 USD  CMDUCHN4053601"

    assert sanitize_for_llm(text) == f"{label} [REDACTED_BANK_ACCT]\n245.0000 USD  CMDUCHN4053601"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("accountNo", "000987654321"),
        ("accountNumber", "000987654321"),
        ("bankAccount", "000987654321"),
        ("bank_account", "000987654321"),
        ("account_number", "000987654321"),
        ("accountno", "000987654321"),
        ("routing", "021000021"),
        ("routingNumber", "021000021"),
        ("aba", "021000021"),
        ("abaRouting", "021000021"),
        ("aba_routing", 21000021),
        ("ach", "026009593"),
        ("swift", "BOFAUS3NXXX"),
        ("swiftCode", "BOFAUS3NXXX"),
        ("swift_code", "BOFAUS3NXXX"),
        ("bic", "BOFAUS3NXXX"),
        ("iban", "GB82WEST12345698765432"),
    ],
)
def test_sanitizer_preserves_json_and_masks_bank_fields(key: str, value: int | str) -> None:
    charges = {"amount": "1310.40", "unitPrice": "655.20", "quantity": 2, "currency": "USD"}
    business = {"invoiceNumber": "DC-108478", "mblNumber": "ORFR1618731", "total": "1310.40"}
    wire = json.dumps({**business, "other": {key: value}, "chargeList": [charges]}, indent=2)

    sanitized = sanitize_for_llm(wire)

    assert json.loads(sanitized) == {
        **business,
        "other": {key: "[REDACTED_BANK_ACCT]"},
        "chargeList": [charges],
    }
    assert sanitize_for_llm(sanitized) == sanitized


def _ocr_settings(**overrides) -> AppSettings:
    options = dict(
        ocr_gateway_url="http://127.0.0.1:1/ai/process",
        ocr_proxy_url="",
        ocr_trust_env=False,
        llm_api_key=LLM_KEY,
    )
    return AppSettings(_env_file=None, **(options | overrides))


def test_ocr_post_contract_preserves_charges_and_document_fields() -> None:
    requests: list[httpx.Request] = []
    ocr_response = {
        "success": True,
        "data": {
            "pagesSplit": [[3]],
            "list": [
                {
                    "pageNo": 3,
                    "invoiceNumber": "DC-108478",
                    "chargeList": [{"name": "Handling", "amount": "1310.40", "currency": "USD"}],
                    "suspicious": {"reason": "duplicate fee"},
                    "hold": ["pending inspection"],
                    "other": {"reference": "additional reference"},
                }
            ],
        },
    }

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=ocr_response)

    settings = _ocr_settings(ocr_task="test-task")
    client = ocr_client.PltPltOcrClient(settings, transport=httpx.MockTransport(gateway))

    result = client.recognize_url(f" {PUBLIC_FILE} ")

    (request,) = requests
    assert (request.method, str(request.url)) == ("POST", settings.ocr_gateway_url)
    assert "authorization" not in request.headers
    assert LLM_KEY not in request.content.decode()
    assert json.loads(request.content) == {
        "file": PUBLIC_FILE,
        "task": "test-task",
        "callBack": "",
        "taskType": "",
    }
    assert result.payload.has_charges is True
    assert result.payload.has_metadata is True
    (item,) = result.payload.items
    assert item.invoice_number == "DC-108478"
    assert item.charge_list == [{"name": "Handling", "amount": "1310.40", "currency": "USD"}]
    assert item.model_extra == {
        "pageNo": 3,
        "suspicious": {"reason": "duplicate fee"},
        "hold": ["pending inspection"],
    }
    assert item.other == {"reference": "additional reference"}
    assert "[Page 3]" in result.formatted_text
    assert "1310.40" in result.formatted_text
    assert "duplicate fee" in result.formatted_text
    assert "pending inspection" in result.formatted_text
    assert "additional reference" in result.formatted_text
    assert result.pages_split == [[3]]
    assert result.raw_payload == ocr_response


@pytest.mark.asyncio
async def test_ocr_async_preserves_metadata_without_inventing_charges() -> None:
    async def gateway(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "data": {"list": [{"mblNumber": "MBL-42"}]}}
        )

    client = ocr_client.PltPltOcrClient(
        _ocr_settings(), async_transport=httpx.MockTransport(gateway)
    )

    result = await client.arecognize_url(PUBLIC_FILE)

    assert result.payload.has_charges is False
    assert result.payload.has_metadata is True
    (item,) = result.payload.items
    assert item.mbl_number == "MBL-42"
    assert item.charge_list == []


@pytest.mark.parametrize(
    ("status", "response", "expected_error"),
    [
        (502, {}, "HTTP 502"),
        (200, ["not an object"], "JSON"),
        (200, {"success": False, "data": {"list": [{"invoiceNumber": "INV-1"}]}}, "失败"),
        (200, {"data": {}}, "无有效单据内容"),
        (200, {"data": {"list": {}}}, "无有效单据内容"),
        (200, {"data": {"list": []}}, "无有效单据内容"),
        (200, {"data": {"list": [{"chargeList": [], "invoiceNumber": " "}]}}, "无有效单据内容"),
    ],
)
def test_ocr_raises_on_failure_or_missing_content(status, response, expected_error) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json=response))
    client = ocr_client.PltPltOcrClient(_ocr_settings(), transport=transport)

    with pytest.raises(ocr_client.OcrGatewayError, match=expected_error):
        client.recognize_url(PUBLIC_FILE)


def test_ocr_rejects_blocked_file_before_gateway_post() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        pytest.fail("A blocked file URL reached the OCR gateway")

    client = ocr_client.PltPltOcrClient(
        _ocr_settings(), transport=httpx.MockTransport(unexpected_request)
    )

    with pytest.raises(SsrfBlockedError):
        client.recognize_url("http://169.254.169.254/latest/")


@pytest.mark.parametrize("query", ["", "?Signature=synthetic-signed-file"])
def test_batch_and_ocr_reject_url_credentials_with_the_same_code(query: str) -> None:
    from shipping_billing_agent.parsers.dispatcher import validate_batch_inputs

    url = f"http://user:pass@example.com/a.pdf{query}"
    client = ocr_client.PltPltOcrClient(
        _ocr_settings(), transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected POST"))
    )

    (issue,) = validate_batch_inputs([], [url], url_validator=validate_public_url)
    with pytest.raises(SsrfBlockedError) as error:
        client.recognize_url(url)

    assert issue.code == error.value.code == "SSRF_BLOCKED"
    assert issue.filename_or_url == "http://[REDACTED]@example.com/a.pdf" + (
        "?[REDACTED]" if query else ""
    )
    assert "user:pass" not in str(error.value)
    assert "synthetic-signed-file" not in str(error.value)


def test_ocr_masks_presigned_query_in_request_errors() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"Timed out while processing {PUBLIC_FILE}", request=request)

    client = ocr_client.PltPltOcrClient(_ocr_settings(), transport=httpx.MockTransport(fail))

    with pytest.raises(ocr_client.OcrGatewayError, match="ReadTimeout") as error:
        client.recognize_url(PUBLIC_FILE)

    assert "test-signature-value" not in "".join(traceback.format_exception(error.value))


@pytest.fixture(scope="module")
def rapid_ocr() -> local_ocr.RapidOcrFallback:
    return local_ocr.RapidOcrFallback()


def _text_image(text: str) -> Image.Image:
    image = Image.new("RGB", (700, 150), "white")
    ImageDraw.Draw(image).text((24, 28), text, font=ImageFont.load_default(size=58), fill="black")
    return image


@pytest.mark.parametrize(("image_format", "filename"), [("PNG", "bill.png"), ("JPEG", "bill.jpg")])
def test_local_ocr_recognizes_synthetic_image(
    rapid_ocr: local_ocr.RapidOcrFallback, image_format: str, filename: str
) -> None:
    with _text_image("INVOICE") as image, BytesIO() as buffer:
        image.save(buffer, format=image_format)
        text, confidence = rapid_ocr.recognize_image_bytes(buffer.getvalue(), filename)

    assert text.splitlines() == ["[Page 1]", "INVOICE"]
    assert 0.8 <= confidence <= 1.0


def test_local_ocr_preserves_tiff_frame_order(rapid_ocr: local_ocr.RapidOcrFallback) -> None:
    with _text_image("FIRST PAGE") as first, _text_image("SECOND PAGE") as second, BytesIO() as buf:
        first.save(buf, format="TIFF", save_all=True, append_images=[second])
        text, confidence = rapid_ocr.recognize_image_bytes(buf.getvalue(), "two-pages.tiff")

    assert text.split() == ["[Page", "1]", "FIRST", "PAGE", "[Page", "2]", "SECOND", "PAGE"]
    assert 0.8 <= confidence <= 1.0


@pytest.mark.parametrize("content", [b"", b"broken PNG"])
def test_local_ocr_rejects_empty_or_damaged_bytes(content: bytes) -> None:
    with pytest.raises(local_ocr.LocalOcrError):
        local_ocr.RapidOcrFallback().recognize_image_bytes(content, "invalid.png")


def test_local_ocr_rejects_a_blank_image(rapid_ocr: local_ocr.RapidOcrFallback) -> None:
    with Image.new("RGB", (150, 100), "white") as image:
        with pytest.raises(local_ocr.LocalOcrError, match="未识别到文字"):
            rapid_ocr.recognize_pil_image(image)


def test_local_ocr_uses_engine_order_and_mean_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    import rapidocr_onnxruntime

    initialized = []

    class FakeRapidOcr:
        def __init__(self) -> None:
            initialized.append(True)

        def __call__(self, _image):
            lower_box = [[0, 60], [30, 60], [30, 80], [0, 80]]
            upper_box = [[0, 0], [30, 0], [30, 20], [0, 20]]
            return [[lower_box, "first", 0.7], [upper_box, "second", 0.9]], []

    monkeypatch.setattr(rapidocr_onnxruntime, "RapidOCR", FakeRapidOcr)
    fallback = local_ocr.RapidOcrFallback()
    assert initialized == []

    with _text_image("INVOICE") as image:
        text, confidence = fallback.recognize_pil_image(image, page=7)

    assert initialized == [True]
    assert text.splitlines() == ["[Page 7]", "first", "second"]
    assert confidence == pytest.approx(0.8)


class _ExtractedInvoice(BaseModel):
    invoice_number: str


@pytest.fixture
def loopback_gateway(monkeypatch: pytest.MonkeyPatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body, self.headers.get("Authorization")))
            if self.path.partition("?")[0] == "/ai/process":
                response = {"data": {"list": [{"invoiceNumber": "DC-108478"}]}}
                self._reply(response)
                return

            if self.path == "/ocr-bank-fields":
                self._reply(
                    {
                        "data": {
                            "list": [
                                {
                                    **OCR_BANK_FIELDS,
                                    "invoiceNumber": "DC-108478",
                                    "mblNumber": "ORFR1618731",
                                    "chargeList": [{"amount": "1310.40", "currency": "USD"}],
                                }
                            ]
                        }
                    }
                )
                return

            if self.path == "/error/chat/completions":
                self._reply({"error": {"message": f"Key {LLM_KEY} rejected"}}, status=401)
                return

            content = '{"invoice_number":"DC-108478"}'
            if self.path == "/bad-json/chat/completions":
                content = "not a JSON invoice"
            if self.path == "/signed-bad-json/chat/completions":
                content = f"not a JSON invoice: {PUBLIC_FILE} and {LLM_KEY}"

            self._reply(
                {
                    "id": "chatcmpl-local-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "loopback-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                }
            )

        def _reply(self, payload: dict, *, status: int = 200) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, socket.socket() as bad_proxy:
        # Reserve an unused port without listening so the proxy connection fails locally.
        bad_proxy.bind(("127.0.0.1", 0))
        proxy_url = f"http://127.0.0.1:{bad_proxy.getsockname()[1]}"
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.asyncio
async def test_ocr_bypasses_bad_env_proxy_and_injects_real_transport_timeouts(
    loopback_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, requests = loopback_gateway
    sync_send = httpx.HTTPTransport.handle_request
    async_send = httpx.AsyncHTTPTransport.handle_async_request
    sent_timeouts = []

    def record_sync(transport, request: httpx.Request) -> httpx.Response:
        sent_timeouts.append(("sync", request.extensions.get("timeout")))
        return sync_send(transport, request)

    async def record_async(transport, request: httpx.Request) -> httpx.Response:
        sent_timeouts.append(("async", request.extensions.get("timeout")))
        return await async_send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", record_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", record_async)
    client = ocr_client.PltPltOcrClient(
        _ocr_settings(
            ocr_gateway_url=f"{origin}/ai/process",
            ocr_connect_timeout_seconds=0.75,
            ocr_read_timeout_seconds=1.25,
        )
    )

    sync_result = client.recognize_url(PUBLIC_FILE)
    async_result = await client.arecognize_url(PUBLIC_FILE)

    assert sync_result.payload.items[0].invoice_number == "DC-108478"
    assert async_result.payload.items[0].invoice_number == "DC-108478"
    assert [path for path, _, _ in requests] == ["/ai/process", "/ai/process"]
    timeout = {"connect": 0.75, "read": 1.25, "write": 30.0, "pool": 10.0}
    assert sent_timeouts == [("sync", timeout), ("async", timeout)]


def test_bad_env_proxy_actually_prevents_loopback_connection(loopback_gateway) -> None:
    origin, requests = loopback_gateway

    with httpx.Client(trust_env=True, timeout=0.2) as client, pytest.raises(httpx.RequestError):
        client.post(f"{origin}/ai/process", json={})

    assert requests == []


@pytest.mark.asyncio
async def test_ocr_keeps_token_on_wire_but_masks_it_in_http_debug_logs(
    loopback_gateway, caplog: pytest.LogCaptureFixture
) -> None:
    origin, requests = loopback_gateway
    target = "/ai/process?token=synthetic-ocr-token"
    caplog.set_level(logging.DEBUG)
    settings = _ocr_settings(ocr_gateway_url=origin + target)

    ocr_client.PltPltOcrClient(settings).recognize_url(PUBLIC_FILE)
    await ocr_client.PltPltOcrClient(settings).arecognize_url(PUBLIC_FILE)

    assert [path for path, _, _ in requests] == [target, target]
    assert caplog.text.count(f"{origin}/ai/process?[REDACTED]") == 2
    assert "synthetic-ocr-token" not in caplog.text


def _llm_settings(base_url: str, **overrides) -> AppSettings:
    options = dict(
        llm_base_url=base_url,
        llm_api_key=LLM_KEY,
        deepseek_api_key=DEEPSEEK_KEY,
        llm_model="loopback-model",
        llm_proxy_url="",
        llm_trust_env=False,
        llm_timeout_seconds=1.5,
        llm_max_retries=0,
    )
    return AppSettings(_env_file=None, **(options | overrides))


@pytest.mark.asyncio
async def test_llm_sync_and_async_bypass_env_proxy_and_sanitize_outgoing_messages(
    loopback_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, requests = loopback_gateway
    build_chat = llm_client.ChatOpenAI
    chat_options = {}

    def record_chat_options(**options):
        chat_options.update(options)
        return build_chat(**options)

    monkeypatch.setattr(llm_client, "ChatOpenAI", record_chat_options)
    client = llm_client.OpenAICompatibleLlmClient(_llm_settings(f"{origin}/v1"))
    messages = [
        ("system", "Return a JSON invoice. Do not send Routing: 021000021."),
        ("human", "Account No: 123456789012; Total USD 1,310.40; MBL ORFR1618731"),
    ]
    try:
        sync_result = client.invoke_structured(messages, _ExtractedInvoice)
        async_result = await client.ainvoke_structured(messages, _ExtractedInvoice)
    finally:
        await client.aclose()

    assert isinstance(chat_options["http_client"], httpx.Client)
    assert isinstance(chat_options["http_async_client"], httpx.AsyncClient)
    assert chat_options["http_client"].timeout == httpx.Timeout(1.5)
    assert chat_options["http_async_client"].timeout == httpx.Timeout(1.5)
    assert [path for path, _, _ in requests] == ["/v1/chat/completions"] * 2
    assert [auth for _, _, auth in requests] == [f"Bearer {LLM_KEY}"] * 2
    assert [body["response_format"] for _, body, _ in requests] == [{"type": "json_object"}] * 2
    expected_messages = [
        {
            "role": "system",
            "content": "Return a JSON invoice. Do not send Routing: [REDACTED_BANK_ACCT].",
        },
        {
            "role": "user",
            "content": "Account No: [REDACTED_BANK_ACCT]; Total USD 1,310.40; MBL ORFR1618731",
        },
    ]
    assert [body["messages"] for _, body, _ in requests] == [expected_messages] * 2
    assert sync_result == async_result
    assert sync_result.parsed == _ExtractedInvoice(invoice_number="DC-108478")
    assert sync_result.raw_text == '{"invoice_number":"DC-108478"}'
    assert sync_result.parsing_error is None
    assert sync_result.model_name == "loopback-model"
    assert sync_result.usage["total_tokens"] == 17


@pytest.mark.asyncio
async def test_ocr_formatted_invoice_is_sanitized_at_both_llm_receivers(loopback_gateway) -> None:
    from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

    origin, requests = loopback_gateway
    ocr = ocr_client.PltPltOcrClient(_ocr_settings(ocr_gateway_url=f"{origin}/ocr-bank-fields"))
    dispatcher = DocumentParserDispatcher(remote_ocr=ocr, url_validator=validate_public_url)
    (document,) = dispatcher.parse_batch([], [PUBLIC_FILE])
    raw_fields = json.loads(document.raw_text)
    assert {key: raw_fields[key] for key in OCR_BANK_FIELDS} == OCR_BANK_FIELDS

    client = llm_client.OpenAICompatibleLlmClient(_llm_settings(f"{origin}/v1"))
    try:
        sync = client.invoke_structured(document.raw_text, _ExtractedInvoice)
        async_ = await client.ainvoke_structured(document.raw_text, _ExtractedInvoice)
    finally:
        await client.aclose()

    expected_invoice = _ExtractedInvoice(invoice_number="DC-108478")
    assert sync.parsed == async_.parsed == expected_invoice
    assert [path for path, _, _ in requests] == [
        "/ocr-bank-fields",
        "/v1/chat/completions",
        "/v1/chat/completions",
    ]
    for mode, (_, request, _) in zip(("sync", "async"), requests[1:], strict=True):
        (message,) = request["messages"]
        content = message["content"]
        assert all(value not in content for value in OCR_BANK_FIELDS.values()), mode
        assert json.loads(content) == {
            **dict.fromkeys(OCR_BANK_FIELDS, "[REDACTED_BANK_ACCT]"),
            "invoiceNumber": "DC-108478",
            "mblNumber": "ORFR1618731",
            "chargeList": [{"amount": "1310.40", "currency": "USD"}],
        }, mode


@pytest.mark.asyncio
async def test_structured_llm_blocks_host_langsmith_tracing_on_both_paths(
    loopback_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, requests = loopback_gateway
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-fake")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", f"{origin}/telemetry")
    posts = []
    patches = []
    monkeypatch.setattr(RunTree, "post", lambda run, **_: posts.append(run.run_type))
    monkeypatch.setattr(RunTree, "patch", lambda run, **_: patches.append(run.run_type))
    client = llm_client.OpenAICompatibleLlmClient(_llm_settings(f"{origin}/v1"))
    get_env_var.cache_clear()
    try:
        sync = client.invoke_structured("Extract DC-108478", _ExtractedInvoice)
        async_ = await client.ainvoke_structured("Extract DC-108478", _ExtractedInvoice)
        wait_for_all_tracers()
    finally:
        get_env_var.cache_clear()
        await client.aclose()

    assert sync.parsed == async_.parsed == _ExtractedInvoice(invoice_number="DC-108478")
    assert [path for path, _, _ in requests] == ["/v1/chat/completions"] * 2
    assert posts == []
    assert patches == []


@pytest.mark.parametrize(
    ("base_url", "explicit_key"),
    [
        ("http://api.deepseek.com/v1", ""),
        ("https://another.example/v1", ""),
        ("https://api.deepseek.com.example/v1", ""),
        ("http://api.deepseek.com/v1", DEEPSEEK_KEY),
        ("https://another.example/v1", DEEPSEEK_KEY),
    ],
)
def test_llm_refuses_to_use_deepseek_key_at_unapproved_endpoint(
    base_url: str, explicit_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_chat(**_kwargs):
        pytest.fail("A disallowed credential reached the model constructor")

    monkeypatch.setattr(llm_client, "ChatOpenAI", unexpected_chat)
    with pytest.raises(llm_client.LlmNotConfiguredError):
        llm_client.OpenAICompatibleLlmClient(_llm_settings(base_url, llm_api_key=explicit_key))


@pytest.mark.asyncio
async def test_llm_keeps_parsing_error_and_raw_text(loopback_gateway) -> None:
    origin, _ = loopback_gateway
    client = llm_client.OpenAICompatibleLlmClient(_llm_settings(f"{origin}/bad-json"))
    try:
        result = await client.ainvoke_structured("Extract a JSON invoice", _ExtractedInvoice)
    finally:
        await client.aclose()

    assert result.parsed is None
    assert isinstance(result.parsing_error, Exception)
    assert result.raw_text == "not a JSON invoice"


@pytest.mark.asyncio
async def test_llm_masks_presigned_query_and_key_in_both_raw_replies_and_parse_errors(
    loopback_gateway,
) -> None:
    origin, _ = loopback_gateway
    client = llm_client.OpenAICompatibleLlmClient(_llm_settings(f"{origin}/signed-bad-json"))
    try:
        sync = client.invoke_structured("Extract a JSON invoice", _ExtractedInvoice)
        async_ = await client.ainvoke_structured("Extract a JSON invoice", _ExtractedInvoice)
    finally:
        await client.aclose()

    masked = "https://93.184.216.34/invoice.pdf?[REDACTED]"
    for result in (sync, async_):
        assert result.parsed is None
        assert result.raw_text == f"not a JSON invoice: {masked} and [REDACTED]"
        assert isinstance(result.parsing_error, Exception)
        trace = "".join(traceback.format_exception(result.parsing_error))
        assert masked in trace
        assert "test-signature-value" not in trace
        assert LLM_KEY not in trace


def test_llm_errors_and_logs_hide_provider_keys(loopback_gateway, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    origin, requests = loopback_gateway
    client = llm_client.OpenAICompatibleLlmClient(_llm_settings(f"{origin}/error"))
    try:
        with pytest.raises(llm_client.LlmGatewayError, match="401") as error:
            client.invoke_structured("Extract a JSON invoice", _ExtractedInvoice)
    finally:
        client.close()

    assert [path for path, _, _ in requests] == ["/error/chat/completions"]
    assert LLM_KEY not in "".join(traceback.format_exception(error.value))
    assert LLM_KEY not in caplog.text


@pytest.mark.asyncio
async def test_fake_llm_records_sync_and_async_invocations() -> None:
    response = llm_client.StructuredLlmResponse(
        parsed=_ExtractedInvoice(invoice_number="DC-108478"),
        raw_text="fixture",
        parsing_error=None,
        model_name="fake-model",
    )
    client = llm_client.FakeStructuredLlmClient(response)

    assert client.invoke_structured("first prompt", _ExtractedInvoice) == response
    assert await client.ainvoke_structured("second prompt", _ExtractedInvoice) == response
    assert client.sync_calls == [("first prompt", _ExtractedInvoice)]
    assert client.async_calls == [("second prompt", _ExtractedInvoice)]
