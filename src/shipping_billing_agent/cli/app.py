"""Minimal B00 CLI entrypoint."""

from __future__ import annotations

import typer

from shipping_billing_agent import __version__

app = typer.Typer(help="多格式文档解析的航运智能账单助手 CLI", no_args_is_help=True)


@app.callback()
def main() -> None:
    """多格式文档解析的航运智能账单助手命令行入口。"""


@app.command()
def version() -> None:
    """打印当前版本号。"""
    typer.echo(__version__)
