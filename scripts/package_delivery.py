"""Build a clean source delivery ZIP from the project checkout."""

from __future__ import annotations

import argparse
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISLNK, S_ISREG
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "shipping-billing-agent-delivery.zip"

FIXED_FILES = {
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".env.example",
    ".gitignore",
    "docs/harness-design.md",
    "docs/deep-thinking.md",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/vite.config.ts",
}
SOURCE_DIRECTORIES = (
    "examples",
    "src",
    "tests",
    "scripts",
    "docs/workflow/contracts",
    "docs/workflow/diagrams",
    "frontend/src",
    "frontend/public",
)
REQUIRED_FILES = FIXED_FILES | {
    "examples/sample_debit_note.eml",
    "examples/existing_bills.json",
    "docs/workflow/contracts/billing-output.schema.json",
    "docs/workflow/contracts/openapi.yaml",
    "docs/workflow/contracts/schema.sql",
    "docs/workflow/diagrams/architecture.html",
    "docs/workflow/diagrams/user-billing-flow.html",
    "docs/workflow/diagrams/pipeline-sequence.html",
    "docs/workflow/diagrams/operation-decision-flow.html",
    "frontend/tsconfig.json",
    "frontend/tsconfig.app.json",
    "frontend/tsconfig.node.json",
    "frontend/src/main.tsx",
    "scripts/package_delivery.py",
    "scripts/verify_contracts.py",
    "src/shipping_billing_agent/cli/app.py",
    "tests/harness/test_b05_delivery_package.py",
}
EXCLUDED_NAMES = {
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
EXCLUDED_WORKFLOW_ENTRIES = {
    "features",
    "prototypes",
    "index.md",
    "backlog.md",
    "releases",
    "product-spec.md",
    "arch-contract.md",
}
SECRET_PATTERN = re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}")


def _is_excluded(name: str) -> bool:
    parts = PurePosixPath(name.casefold()).parts
    return (
        any(
            part in EXCLUDED_NAMES
            or part.startswith("ai_agent_")
            or part.endswith((".pyc", ".pyo", ".db", ".tsbuildinfo"))
            or ".sqlite" in part
            or (part.startswith(".env.") and name != ".env.example")
            for part in parts
        )
        or parts[:2] == ("docs", "workflow")
        and len(parts) > 2
        and parts[2] in EXCLUDED_WORKFLOW_ENTRIES
    )


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _raise_walk_error(error: OSError) -> None:
    raise error


def _collect_files() -> dict[str, Path]:
    candidates = {ROOT / name for name in FIXED_FILES}
    frontend = ROOT / "frontend"
    if frontend.is_dir() and not _is_link(frontend):
        candidates.update(
            path for path in frontend.iterdir() if fnmatchcase(path.name, "tsconfig*.json")
        )

    for name in SOURCE_DIRECTORIES:
        source = ROOT / name
        if not source.is_dir() or _is_link(source) or source.resolve() != source:
            continue
        for folder, directories, filenames in os.walk(source, onerror=_raise_walk_error):
            directory = Path(folder)
            directories[:] = [
                child
                for child in directories
                if not _is_excluded((directory / child).relative_to(ROOT).as_posix())
                and not _is_link(directory / child)
            ]
            candidates.update(directory / filename for filename in filenames)

    files = {}
    for path in candidates:
        name = path.relative_to(ROOT).as_posix()
        if _is_excluded(name) or not path.exists() or _is_link(path):
            continue
        if S_ISREG(path.lstat().st_mode) and path.resolve() == path:
            files[name] = path
    return files


def _require_files(names: set[str]) -> None:
    if missing := REQUIRED_FILES - names:
        raise ValueError(f"缺少交付必需文件：{', '.join(sorted(missing))}")


def _verify_zip(path: Path, expected: list[str]) -> None:
    with ZipFile(path) as archive:
        names = archive.namelist()
        _require_files(set(names))
        if names != expected or any(_is_excluded(name) for name in names):
            raise ValueError("ZIP 内的文件与交付白名单不一致")
        if invalid_entry := archive.testzip():
            raise ValueError(f"ZIP 条目未通过校验：{invalid_entry}")


def package_delivery(output: Path) -> int:
    files = _collect_files()
    _require_files(set(files))
    output = output.resolve()
    if output in files.values():
        raise ValueError("ZIP 输出路径不能覆盖交付源文件")

    names = sorted(files)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".delivery-", dir=output.parent) as scratch:
        staged_zip = Path(scratch) / "delivery.zip"
        with ZipFile(staged_zip, "w", compression=ZIP_DEFLATED) as archive:
            for name in names:
                content = files[name].read_bytes()
                if SECRET_PATTERN.search(content):
                    raise ValueError(f"发现疑似 API 密钥，打包中止：{name}")
                archive.writestr(ZipInfo(name), content, compress_type=ZIP_DEFLATED)
        _verify_zip(staged_zip, names)
        staged_zip.replace(output)
    return len(names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="ZIP output path")
    args = parser.parse_args()

    try:
        output = args.out.resolve()
        count = package_delivery(output)
    except (OSError, ValueError, BadZipFile) as exc:
        parser.exit(1, f"打包失败。{exc}\n")

    print(f"{output} | {count} 个条目 | {output.stat().st_size} 字节")


if __name__ == "__main__":
    main()
