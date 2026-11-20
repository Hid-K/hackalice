"""Единая точка вывода: rich-консоль и помощники печати.

Все текстовые помощники экранируют markup во входной строке, чтобы имена
интерфейсов/пути с квадратными скобками не ломали вывод rich.
"""
from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

console = Console(highlight=False)


def info(msg: str) -> None:
    console.print(f"[cyan]*[/cyan] {escape(str(msg))}")


def ok(msg: str) -> None:
    console.print(f"[bold green]OK[/bold green] {escape(str(msg))}")


def warn(msg: str) -> None:
    console.print(f"[bold yellow]![/bold yellow] {escape(str(msg))}")


def err(msg: str) -> None:
    console.print(f"[bold red]X[/bold red] {escape(str(msg))}")


def dim(msg: str) -> None:
    console.print(Text(str(msg), style="dim"))


def banner(title: str) -> None:
    console.rule(f"[bold cyan]{escape(title)}")


def panel(text: str, title: str = "", border: str = "cyan") -> None:
    console.print(Panel(Text(text.strip("\n")), title=title or None,
                        border_style=border, padding=(0, 1)))


def wait_enter(msg: str = "нажмите Enter, чтобы продолжить") -> None:
    console.input(f"[bold magenta]?[/bold magenta] {escape(msg)}")
