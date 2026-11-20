from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
from decimal import Decimal
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from jsonschema import Draft202012Validator

from shipping_billing_agent.domain.models import loads_decimal_json

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "docs/workflow/contracts/billing-output.schema.json"
HOST = "127.0.0.1"
ENTITY = "NEPTUNE SHIPPING LIMITED"
EXTERNAL_CONFIG = {"LLM_API_KEY", "DEEPSEEK_API_KEY", "PUBLIC_RELAY_BASE_URL"}
SECRET_PATTERN = re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}")


def _run_packer(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "scripts/package_delivery.py", "--out", str(output)],
        cwd=source,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=os.environ | {"PYTHONUTF8": "1"},
        timeout=30,
        check=False,
    )


@pytest.fixture
def delivery_zip(tmp_path: Path) -> Path:
    output = tmp_path / "delivery.zip"
    result = _run_packer(ROOT, output)
    assert result.returncode == 0, result.stderr
    assert output.is_file(), result.stdout
    return output


@pytest.fixture
def delivery_copy(delivery_zip: Path, tmp_path: Path) -> tuple[Path, set[str]]:
    source = tmp_path / "delivery-copy"
    with ZipFile(delivery_zip) as archive:
        archive.extractall(source)
        names = set(archive.namelist())
    assert not (source / ".git").exists()
    return source, names


def test_delivery_zip_is_clean_and_complete(delivery_zip: Path) -> None:
    required = {
        "README.md",
        "docs/harness-design.md",
        "docs/deep-thinking.md",
        "examples/sample_debit_note.eml",
        "examples/existing_bills.json",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".env.example",
        ".gitignore",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/index.html",
        "frontend/src/main.tsx",
        "frontend/tsconfig.json",
        "frontend/tsconfig.app.json",
        "frontend/tsconfig.node.json",
        "frontend/vite.config.ts",
        "scripts/package_delivery.py",
        "scripts/verify_contracts.py",
        "src/shipping_billing_agent/cli/app.py",
        "tests/fixtures/b01_empty_columns.doc",
        "tests/fixtures/b01_encrypted.pdf",
        "tests/fixtures/b01_interleaved.docx",
        "tests/fixtures/b01_merged.xls",
        "tests/fixtures/b01_merged.xlsx",
        "tests/fixtures/b01_mixed.pdf",
        "tests/fixtures/table-merges.doc",
        SCHEMA,
        "docs/workflow/contracts/openapi.yaml",
        "docs/workflow/contracts/schema.sql",
        "docs/workflow/diagrams/architecture.html",
        "docs/workflow/diagrams/user-billing-flow.html",
        "docs/workflow/diagrams/pipeline-sequence.html",
        "docs/workflow/diagrams/operation-decision-flow.html",
    }
    forbidden_parts = {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "build",
        ".wf-tmp",
        ".worktree",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        ".env",
        "data",
        "笔试题aiagent",
        "agents.md",
    }
    forbidden_workflow_entries = {
        "features",
        "prototypes",
        "index.md",
        "backlog.md",
        "releases",
        "product-spec.md",
        "arch-contract.md",
    }
    with ZipFile(delivery_zip) as archive:
        names = archive.namelist()
        assert required <= set(names)
        assert names == sorted(set(names))
        assert archive.testzip() is None
        for entry in archive.infolist():
            name = entry.filename
            relative = PurePosixPath(name.casefold())
            assert not relative.is_absolute() and ".." not in relative.parts, name
            assert not forbidden_parts.intersection(relative.parts), name
            assert not any(part.startswith("ai_agent_") for part in relative.parts), name
            assert name == ".env.example" or not any(
                part.startswith(".env.") for part in relative.parts
            ), name
            assert not any(
                part.endswith((".db", ".pyc", ".pyo", ".tsbuildinfo")) or ".sqlite" in part
                for part in relative.parts
            ), name
            if relative.parts[:2] == ("docs", "workflow"):
                assert relative.parts[2] not in forbidden_workflow_entries, name
            assert entry.compress_type == ZIP_DEFLATED, name
            assert SECRET_PATTERN.search(archive.read(entry)) is None, name


def test_delivery_documents_cover_evidence_and_submission(delivery_zip: Path) -> None:
    with ZipFile(delivery_zip) as archive:
        readme = archive.read("README.md").decode("utf-8")
        harness = archive.read("docs/harness-design.md").decode("utf-8")
        thinking = archive.read("docs/deep-thinking.md").decode("utf-8")

    for text in ("LUKA@PLTPLT.COM", "pan@pltplt.com", "应聘渠道+姓名+应聘岗位"):
        assert text in readme
    for text in ("pass@k", "pass^k", "1.0", "150/150", "16/16", "0/69"):
        assert text in harness
    for doc in (readme, harness):
        assert "UNVERIFIED" in doc
        assert re.search(r"\b17\d\d\s+passed", doc)
    assert re.findall(r"^## 思考([一二三四五六])(?:\s|$)", thinking, re.MULTILINE) == list(
        "一二三四五六"
    )


@pytest.mark.parametrize("git_checkout", [False, True], ids=["clean-copy", "git-checkout"])
def test_delivery_packer_ignores_files_outside_the_allowlist(
    delivery_copy: tuple[Path, set[str]], tmp_path: Path, git_checkout: bool
) -> None:
    source, expected = delivery_copy
    for name in (
        "examples/extra_note.txt",
        "tests/fixtures/stray.pdf",
        "tests/fixtures/scratch.py",
        "scripts/scratch.txt",
        "src/shipping_billing_agent/temp_notes.md",
        "tests/unit/temp.note",
        "docs/workflow/contracts/scratch.yaml",
        "docs/workflow/diagrams/scratch.html",
        "frontend/tsconfig.scratch.json",
        "frontend/src/temp_notes.md",
    ):
        (source / name).write_text("ordinary local file\n", encoding="utf-8")
    if git_checkout:
        for args in (["init", "--quiet"], ["add", "--force", "--", "."]):
            subprocess.run(["git", *args], cwd=source, capture_output=True, check=True, timeout=15)
        (source / "src/shipping_billing_agent/untracked.py").write_text("# local only\n")

    output = tmp_path / "repacked.zip"
    result = _run_packer(source, output)
    assert result.returncode == 0, result.stderr
    with ZipFile(output) as archive:
        assert archive.namelist() == sorted(expected)


@pytest.mark.parametrize(
    "token",
    [
        "sk-" + "A" * 24,
        "sk-ant-" + "B" * 24,
        "AIza" + "C" * 27,
        "ghp_" + "D" * 30,
        "AKIA" + "E" * 16,
        "-----BEGIN " + "PRIVATE KEY-----",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        "LLM_API_KEY=" + "F" * 20,
        "DEEPSEEK_API_KEY='" + "G" * 20 + "'",
    ],
    ids=[
        "sk-token", "anthropic", "google", "github", "aws", "private-key", "openssh",
        "llm-env-value", "deepseek-quoted-env-value",
    ],
)
def test_delivery_packer_rejects_secret_in_whitelisted_text(
    delivery_copy: tuple[Path, set[str]], tmp_path: Path, token: str
) -> None:
    source, _ = delivery_copy
    name = "src/shipping_billing_agent/__init__.py"
    with (source / name).open("a", encoding="utf-8") as code:
        code.write(f'\n\n_LEAK_PROBE = """\n{token}\n"""\n')

    output = tmp_path / "rejected.zip"
    result = _run_packer(source, output)
    assert result.returncode != 0
    assert name in result.stderr
    assert token not in (result.stdout + result.stderr)
    assert not output.exists()


def _stop_server(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is not None:
        return
    if os.name == "nt":
        try:
            server.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            # A Windows runner without an attached console cannot send Ctrl+Break.
            server.terminate()
    else:
        server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)


def _check_health(extracted: Path, workspace: Path, env: dict[str, str]) -> None:
    with socket.socket() as listener:
        listener.bind((HOST, 0))
        free_port = listener.getsockname()[1]

    serve = [
        sys.executable,
        "-m",
        "shipping_billing_agent.cli.app",
        "serve",
        "--host",
        HOST,
        "--port",
        str(free_port),
        "--db-path",
        str(workspace / "server.sqlite"),
        "--ocr-mode",
        "REMOTE_WITH_LOCAL_FALLBACK",
    ]
    health_url = f"http://{HOST}:{free_port}/api/v1/health"
    client = build_opener(ProxyHandler({}))
    log_path = workspace / "server.log"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with (
        log_path.open("wb") as log,
        subprocess.Popen(
            serve,
            cwd=extracted,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        ) as server,
    ):
        try:
            deadline = monotonic() + 30
            while monotonic() < deadline and server.poll() is None:
                try:
                    with client.open(health_url, timeout=1) as response:
                        assert response.status == 200
                        data = json.load(response)
                        assert data["status"] == "ok"
                        assert data["llm_configured"] is False
                        assert data["public_relay_configured"] is False
                    return
                except (URLError, TimeoutError):
                    sleep(0.15)
            pytest.fail(
                f"服务健康检查失败。\n{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        finally:
            _stop_server(server)


def _check_official_parse(extracted: Path, workspace: Path, env: dict[str, str]) -> None:
    out_json = workspace / "output.json"
    parse = [
        sys.executable,
        "-m",
        "shipping_billing_agent.cli.app",
        "parse",
        "examples/sample_debit_note.eml",
        "--entity",
        ENTITY,
        "--bills",
        "examples/existing_bills.json",
        "--ocr-mode",
        "REMOTE_WITH_LOCAL_FALLBACK",
        "--output-mode",
        "official",
        "--out",
        str(out_json),
        "--db-path",
        str(workspace / "cli.sqlite"),
    ]
    result = subprocess.run(
        parse,
        cwd=extracted,
        env=env,
        timeout=45,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    items = loads_decimal_json(out_json.read_text(encoding="utf-8"))
    assert isinstance(items, list) and len(items) == 2
    schema = json.loads((extracted / SCHEMA).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(items)

    expected = [
        ("UPDATE", "Handling Charge", "Handling Fee", Decimal("40.00"), "PP"),
        ("CREATE", "Terminal Handling Charge", "THC", Decimal("120.50"), "CC"),
    ]
    for item, (operation, charge, raw_name, amount, payment_term) in zip(
        items, expected, strict=True
    ):
        assert item["operation"] == operation
        assert item["document_info"]["reference_no"] == "DN-202609-001"
        details = item["charge_details"]
        assert details["charge_item"] == charge
        assert details["raw_charge_name"] == raw_name
        assert isinstance(details["amount"], Decimal) and details["amount"] == amount
        assert details["currency"] == "USD"
        assert details["payment_term"] == payment_term
        assert item["financial_attributes"] == {"accounting_entity": ENTITY, "posting_type": "AR"}

    update = items[0]["audit_trail"]
    assert update["match_level"] == "L1_DOC_CHARGE"
    assert update["matched_bill_ref"] == "example-dn-202609-001-handling"


def test_delivery_zip_runs_in_empty_directory_outside_checkout(delivery_zip: Path) -> None:
    with TemporaryDirectory(prefix="shipping-billing-delivery-") as temporary:
        workspace = Path(temporary)
        extracted = workspace / "extracted"
        assert not extracted.resolve().is_relative_to(ROOT.resolve())
        extracted.mkdir()
        with ZipFile(delivery_zip) as archive:
            archive.extractall(extracted)

        env = {
            key: value for key, value in os.environ.items() if key.upper() not in EXTERNAL_CONFIG
        }
        env.update(
            PYTHONPATH=str(extracted / "src"),
            APP_DATA_DIR=str(workspace / "data"),
            PYTHONUTF8="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        _check_health(extracted, workspace, env)
        _check_official_parse(extracted, workspace, env)
