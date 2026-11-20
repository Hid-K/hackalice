"""Общие помощники: запуск процессов, пути, права, UTF-8 вывод."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

from .console import dim

# Кириллица в сообщениях: на не-UTF консолях заменяем символы вместо падения
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class StepError(Exception):
    """Ошибка шага — показывается пользователю без traceback."""


def run(cmd, *, cwd=None, check=True, capture=False, input_text=None,
        quiet=False, timeout=None, env=None):
    """Запуск команды: строка — через shell, список — без shell."""
    is_shell = isinstance(cmd, str)
    printable = cmd if is_shell else " ".join(shlex.quote(str(c)) for c in cmd)
    if not quiet:
        dim("$ " + printable)
    try:
        res = subprocess.run(cmd, cwd=cwd, shell=is_shell, text=True,
                             capture_output=capture, input=input_text,
                             timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise StepError(f"не найден исполняемый файл: {e}")
    except subprocess.TimeoutExpired:
        raise StepError(f"таймаут выполнения: {printable}")
    if check and res.returncode != 0:
        tail = ((res.stderr or "") + (res.stdout or "")).strip()
        raise StepError(
            f"команда вернула код {res.returncode}: {printable}\n{tail[:1500]}")
    return res


def which(name: str):
    import shutil
    return shutil.which(name)


def real_home() -> str:
    """Домашний каталог реального пользователя даже под sudo."""
    su = os.environ.get("SUDO_USER")
    if su:
        try:
            import pwd  # noqa: только Linux
            return pwd.getpwnam(su).pw_dir
        except Exception:
            pass
    return os.path.expanduser("~")


def expand(path: str) -> str:
    if path.startswith("~/"):
        return os.path.join(real_home(), path[2:])
    return os.path.expanduser(path)


def require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise StepError(
            "нужны права root: sudo hackalice ... "
            "(или sudo python3 -m hackalice ...)")


def is_linux() -> bool:
    return sys.platform.startswith("linux")
