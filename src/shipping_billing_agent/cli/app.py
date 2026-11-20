"""Command-line entry points for billing, workbench hosting, and evaluation."""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, redirect_stdout
from enum import StrEnum
from importlib import import_module
from itertools import islice
from pathlib import Path
from sqlite3 import DatabaseError
from stat import FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_REPARSE_POINT, FILE_ATTRIBUTE_SYSTEM
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import TYPE_CHECKING, Annotated, Any, cast

import httpx
import typer
import uvicorn

from shipping_billing_agent import __version__

from ..config.limits import ALLOWED_UPLOAD_EXTENSIONS, ResourceLimits
from ..config.settings import AppSettings, load_settings
from ..domain import ExportMode, OperationType, ReviewStatus, dumps_decimal_json
from ..domain.models import BatchStage, BatchStatus, OcrMode, loads_decimal_json
from ..domain.serializer import serialize_records_json
from ..services import (
    BatchService,
    ResourceNotFoundError,
    ServiceValidationError,
    export_review_bundle,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

_IGNORED_FILE_ATTRIBUTES = (
    FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_REPARSE_POINT
)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
EvalRunner = Callable[..., object]

app = typer.Typer(help="多格式文档解析的航运智能账单助手 CLI", no_args_is_help=True)


class _OutputMode(StrEnum):
    OFFICIAL = "official"
    FULL_STANDARD = "full_standard"
    AUDIT = "audit"


class _EvalMode(StrEnum):
    OFFLINE_DETERMINISTIC = "OFFLINE_DETERMINISTIC"
    LIVE_LLM = "LIVE_LLM"
    LIVE_E2E = "LIVE_E2E"


@app.callback()
def main() -> None:
    """多格式文档解析的航运智能账单助手命令行入口。"""


@app.command()
def version() -> None:
    """打印当前版本号。"""
    typer.echo(__version__)


def _visible_entry(path: Path) -> bool:
    if path.name.startswith((".", "~$")) or path.is_symlink():
        return False
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return not attributes & _IGNORED_FILE_ATTRIBUTES


def _raise_walk_error(error: OSError) -> None:
    raise error


def _iter_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file():
            yield path
        elif path.is_dir():
            for directory, subdirectories, filenames in os.walk(path, onerror=_raise_walk_error):
                root = Path(directory)
                subdirectories[:] = sorted(
                    name for name in subdirectories if _visible_entry(root / name)
                )
                for name in sorted(filenames):
                    candidate = root / name
                    if (
                        candidate.suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS
                        and _visible_entry(candidate)
                        and candidate.is_file()
                    ):
                        yield candidate
        else:
            raise ServiceValidationError("INVALID_PATH", f"无法读取文件或目录 {path}")


def _check_output_paths(outputs: list[Path | None], protected: list[Path | None]) -> None:
    destinations = [path.resolve() for path in outputs if path is not None]
    if len(destinations) != len(set(destinations)):
        raise ValueError("--out 和 --bundle-out 须指向不同文件")
    if set(destinations).intersection(path.resolve() for path in protected if path is not None):
        raise ValueError("输出文件不能覆盖输入、历史账单文件或 SQLite 数据库")


def _emit_json(payload: str, destination: Path | None) -> None:
    if destination is None:
        typer.echo(payload.encode("utf-8"))
        return
    staged_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False
        ) as staged_file:
            staged_path = Path(staged_file.name)
            staged_file.write(payload + "\n")
        staged_path.replace(destination)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def _cli_batch_service(settings: AppSettings) -> BatchService:
    # One-shot commands may share a database with a running workbench.
    storage = import_module("shipping_billing_agent.storage")
    database = storage.DatabaseManager(settings.sqlite_db_path, recover_orphans=False)
    return BatchService(settings, repository=storage.BillingRepository(database))


def _report_batch(batch: Mapping[str, Any]) -> bool:
    records = batch["records"]
    pending = [
        record
        for record in records
        if record.operation is OperationType.PENDING_REVIEW
        or record.review_status is ReviewStatus.NEEDS_REVIEW
    ]
    lines_by_doc = Counter(record.source_doc_id for record in records)
    pending_by_doc = Counter(record.source_doc_id for record in pending)
    for document in batch["documents"]:
        doc_id = document["id"]
        typer.echo(
            f"{document['original_filename']} [{document['extraction_channel']}] "
            f"{lines_by_doc[doc_id]} 条，待复核 {pending_by_doc[doc_id]} 条",
            err=True,
        )
    for alert in batch["failed_document_alerts"]:
        typer.echo(f"失败单据 {alert.filename}：{alert.error_message}", err=True)
    typer.echo(
        f"批次 {batch['short_code']} [{batch['status']}/{batch['stage']}] "
        f"{len(records)} 条，failed_files={batch['failed_files']}，"
        f"pending_review_count={len(pending)}。{batch['stage_message']}",
        err=True,
    )
    return bool(pending or batch["failed_document_alerts"])


@app.command()
def parse(
    paths: Annotated[list[Path] | None, typer.Argument(help="文件或目录，可传多项")] = None,
    *,
    entity: Annotated[str, typer.Option(help="记账主体")],
    url: Annotated[list[str] | None, typer.Option("--url", help="公网文件 URL，可重复")] = None,
    bills: Annotated[Path | None, typer.Option(help="仅用于本批的历史账单 JSON")] = None,
    ocr_mode: Annotated[OcrMode, typer.Option()] = OcrMode.REMOTE_ONLY,
    output_mode: Annotated[_OutputMode, typer.Option()] = _OutputMode.OFFICIAL,
    out: Annotated[Path | None, typer.Option(help="标准 JSON 输出文件")] = None,
    bundle_out: Annotated[Path | None, typer.Option(help="工作台复核包输出文件")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="有失败或待复核项时返回非零")] = False,
    db_path: Annotated[Path | None, typer.Option(help="批次 SQLite 文件，默认使用临时库")] = None,
) -> None:
    """解析账单并输出标准 JSON 数组。"""
    urls = url or []
    limit = ResourceLimits().max_batch_files
    try:
        local_files = list(islice(_iter_files(paths or []), max(0, limit - len(urls)) + 1))
        if len(local_files) + len(urls) > limit:
            raise ServiceValidationError("TOO_MANY_FILES", f"单批最多 {limit} 项输入")
        settings = load_settings()
        _check_output_paths(
            [out, bundle_out], [*local_files, bills, db_path, settings.sqlite_db_path]
        )
        # BatchService decodes the JSON with loads_decimal_json and persists the snapshot.
        baseline = bills.read_text(encoding="utf-8-sig") if bills is not None else "[]"
        with redirect_stdout(sys.stderr), ExitStack() as stack:
            if db_path is None:
                directory = Path(stack.enter_context(TemporaryDirectory(prefix="billing-cli-")))
                db_path = directory / "billing.sqlite"
            settings = settings.model_copy(
                update={"sqlite_db_path": db_path, "app_data_dir": db_path.parent}
            )
            files = [(path.name, stack.enter_context(path.open("rb"))) for path in local_files]
            service = stack.enter_context(_cli_batch_service(settings))
            created = service.create_batch(
                accounting_entity=entity,
                files=files,
                public_urls=urls,
                existing_bills_json=baseline,
                ocr_mode=ocr_mode,
                source_channel="CLI",
            )
            with service.repository.transaction() as repository:
                batch = repository.get_batch(created["id"])
                if batch is None:
                    raise ResourceNotFoundError("未找到刚解析的批次")
                standard_json = serialize_records_json(
                    batch["records"], cast(ExportMode, output_mode.value)
                )
                review_json = (
                    dumps_decimal_json(export_review_bundle(repository, batch["id"]))
                    if bundle_out is not None
                    else None
                )
        needs_attention = _report_batch(batch)
        if review_json is not None and bundle_out is not None:
            _emit_json(review_json, bundle_out)
            typer.echo(
                f"复核包已写入 {bundle_out}。请在工作台选择「导入复核包」，"
                "或将该文件作为 JSON POST 到 /api/v1/batches/import-review-bundle。",
                err=True,
            )
        _emit_json(standard_json, out)
    except ServiceValidationError as exc:
        typer.echo(f"{exc.code} {exc.message}", err=True)
        for issue in exc.details.get("issues", []):
            if issue["filename_or_url"]:
                typer.echo(f"{issue['filename_or_url']}：{issue['message']}", err=True)
        raise typer.Exit(code=2) from exc
    except (OSError, DatabaseError, ValueError, ResourceNotFoundError) as exc:
        typer.echo(f"解析或导出失败。{exc}", err=True)
        raise typer.Exit(code=1) from exc

    if batch["stage"] != BatchStage.COMPLETED or (
        strict and (needs_attention or batch["status"] != BatchStatus.COMPLETED)
    ):
        raise typer.Exit(code=1)


def _create_api_app(**options: Any) -> FastAPI:
    # Compose the sibling API entry point on demand.
    return import_module("shipping_billing_agent.api.app").create_app(**options)


class _ServerBatchService(BatchService):
    def __init__(self, settings: AppSettings, ocr_mode: OcrMode) -> None:
        super().__init__(settings)
        self._server_ocr_mode = ocr_mode

    def create_batch(self, **options: Any) -> dict[str, Any]:
        return super().create_batch(**(options | {"ocr_mode": self._server_ocr_mode}))


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="监听地址，默认 127.0.0.1")] = None,
    port: Annotated[int | None, typer.Option(min=0, max=65535, help="端口，默认 8000")] = None,
    db_path: Annotated[Path | None, typer.Option()] = None,
    ocr_mode: Annotated[OcrMode | None, typer.Option(help="统一设置新批次的 OCR 模式")] = None,
) -> None:
    """启动工作台与 API。"""
    settings = load_settings()
    bind_host = settings.app_host if host is None else host
    bind_port = settings.app_port if port is None else port
    if bind_host.lower() not in _LOOPBACK_HOSTS and not settings.allow_remote_bind:
        typer.echo(f"拒绝监听 {bind_host}。非回环地址需要 ALLOW_REMOTE_BIND=true。", err=True)
        raise typer.Exit(code=2)
    if db_path is not None:
        settings = settings.model_copy(update={"sqlite_db_path": db_path})
    with ExitStack() as stack:
        service = (
            stack.enter_context(_ServerBatchService(settings, ocr_mode))
            if ocr_mode is not None
            else None
        )
        application = _create_api_app(settings=settings, batch_service=service)
        uvicorn.run(application, host=bind_host, port=bind_port, workers=1)


def _resolve_eval_runner(
    context: typer.Context, mode: _EvalMode, settings: AppSettings
) -> EvalRunner | None:
    # An embedded caller can pass its eval_runner as the Typer context object.
    if context.obj is None:
        return None
    if mode is not _EvalMode.OFFLINE_DETERMINISTIC and (
        settings.llm_base_url is None
        or settings.llm_api_key is None
        or not settings.is_llm_configured()
    ):
        return None
    if not callable(context.obj):
        raise ValueError("注入的 eval_runner 须可调用")
    return context.obj


async def _post_eval_run(
    service: BatchService, runner: EvalRunner | None, mode: _EvalMode, k_trials: int
) -> str:
    options = {} if runner is None else {"eval_runner": runner}
    application = _create_api_app(batch_service=service, **options)
    # In-process transport shares the API's result validation and persistence.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/eval-runs", json={"run_mode": mode.value, "k_trials": k_trials}
        )
    if response.status_code != 201:
        raise ValueError(response.json()["message"])
    return response.text


def _print_eval_differences(failures: object) -> None:
    for failure in failures if isinstance(failures, list) else [failures]:
        if not isinstance(failure, dict):
            typer.echo(f"失败用例 {dumps_decimal_json(failure)}", err=True)
            continue
        line = f"失败用例 {failure.get('case_id', '未知')}"
        if (trial := failure.get("trial")) is not None:
            line += f"（第 {trial} 轮）"
        if reason := failure.get("reason"):
            line += f" · {reason}"
        if "expected" in failure and "actual" in failure:
            expected = dumps_decimal_json(failure["expected"])
            actual = dumps_decimal_json(failure["actual"])
            line += f" · expected {expected} | actual {actual}"
        typer.echo(line, err=True)


@app.command("eval")
def evaluate(
    context: typer.Context,
    mode: Annotated[_EvalMode, typer.Option()] = _EvalMode.OFFLINE_DETERMINISTIC,
    k_trials: Annotated[int, typer.Option(min=1, max=10)] = 3,
    out: Annotated[Path | None, typer.Option(help="评测 JSON 报告文件")] = None,
    db_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """运行评测并保存当次结果。"""
    try:
        settings = load_settings()
        _check_output_paths([out], [settings.sqlite_db_path, db_path])
        if db_path is not None:
            settings = settings.model_copy(update={"sqlite_db_path": db_path})
        with redirect_stdout(sys.stderr), _cli_batch_service(settings) as service:
            runner = _resolve_eval_runner(context, mode, settings)
            report_json = asyncio.run(_post_eval_run(service, runner, mode, k_trials))
        report = loads_decimal_json(report_json)
        _emit_json(report_json, out)
    except (OSError, DatabaseError, ValueError) as exc:
        typer.echo(f"评测失败。{exc}", err=True)
        raise typer.Exit(code=1) from exc

    status = report["status"]
    pass_pow_k = report["pass_pow_k"]
    failures = report["report"].get("failing_cases")
    below_threshold = pass_pow_k is not None and pass_pow_k < 1
    if status == "COMPLETED":
        if not failures and not below_threshold:
            return
        typer.echo("评测未达标。", err=True)
    else:
        typer.echo(f"{status}。{report['error_message'] or '评测未完成。'}", err=True)
    if status in {"COMPLETED", "FAILED"}:
        if below_threshold:
            typer.echo(f"pass^k expected 1 | actual {pass_pow_k}", err=True)
        if failures:
            _print_eval_differences(failures)
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
