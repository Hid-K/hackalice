"""Системные зависимости: проверка при каждом запуске и авто-установка (apt).

Пользователь ничего не ставит руками: перед выполнением любой команды
cli.main() вызывает deps.ensure() с набором утилит, нужным именно этой
команде (COMMAND_DEPS). Недостающее доустанавливается через apt-get
(DEBIAN_FRONTEND=noninteractive). Если запустить без root при отсутствии
пакетов — понятная ошибка с готовой командой.
"""
from __future__ import annotations

import os

from .console import info, ok, warn
from .util import StepError, is_linux, run, which

# бинарник -> пакет apt
BIN_TO_PKG = {
    "adb": "adb",
    "nmcli": "network-manager",
    "iptables": "iptables",
    "ip": "iproute2",
    "ping": "iputils-ping",
    "git": "git",
    "curl": "curl",
    "python3": "python3",
    "ssh": "openssh-client",
    "ssh-keygen": "openssh-client",
    "iw": "iw",
    "dnsmasq": "dnsmasq-base",   # бинарник для NM-hotspot, без системного демона
    "openssl": "openssl",
    "dig": "bind9-dnsutils",
    "file": "file",
    "make": "make",
    "aarch64-linux-gnu-gcc": "gcc-aarch64-linux-gnu",
    "dpkg-source": "dpkg-dev",
}

# ядро инструмента — без него ничего не работает
CORE_BINS = ["adb", "nmcli", "iptables", "ip", "ping", "git", "curl",
             "python3", "ssh", "ssh-keygen", "iw", "dnsmasq", "openssl"]

# нужны только для сборки dropbear на этом ПК
BUILD_BINS = ["make", "aarch64-linux-gnu-gcc", "dpkg-source", "file"]

# dev-пакеты без исполняемых файлов (проверить presence нельзя — доустанавливаем
# вместе со сборочными)
BUILD_EXTRA_PKGS = ["zlib1g-dev"]

# желательно: dig используется только собственными скриптами lite-kit
# (start.sh), hackalice его не вызывает; readelf — запасная проверка бинарника
OPTIONAL_BINS = ["dig", "readelf"]

# какие утилиты нужны каждой команде ("" -> CORE)
COMMAND_DEPS: dict[str, list[str]] = {
    "wizard": CORE_BINS + BUILD_BINS,
    "check": CORE_BINS,
    "net-up": CORE_BINS,
    "net-down": CORE_BINS,
    "fw": CORE_BINS,
    "serve": CORE_BINS,
    "wait-device": CORE_BINS,
    "build-dropbear": BUILD_BINS,
    "install-ssh": CORE_BINS + BUILD_BINS,
    "verify": ["ssh"],
    "unbind": ["ssh"],
    "cleanup": CORE_BINS,
    "keys": [],
    "add-key": CORE_BINS,
    "find": ["ip", "ping", "ssh"],
    "models": [],
    "interfaces": [],
}


def deps_for_command(cmd: str | None) -> list[str]:
    if not cmd:
        return CORE_BINS  # без подкоманды запускается визард
    return COMMAND_DEPS.get(cmd, CORE_BINS)


def missing(bins: list[str]) -> list[str]:
    return [b for b in bins if not which(b)]


def missing_core() -> list[str]:
    return missing(CORE_BINS)


def missing_build() -> list[str]:
    return missing(BUILD_BINS)


def packages_for(bins: list[str]) -> list[str]:
    out: list[str] = []
    for b in bins:
        pkg = BIN_TO_PKG.get(b)
        if pkg and pkg not in out:
            out.append(pkg)
    return out


def apt_install(pkgs: list[str]) -> None:
    """Установить пакеты apt без интерактивных вопросов."""
    if not pkgs:
        return
    if not is_linux():
        raise StepError("авто-установка зависимостей поддерживается только "
                        "на Linux (apt)")
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    info(f"Ставлю недостающие пакеты: {', '.join(pkgs)}")
    run(["apt-get", "update"], capture=False, env=env)
    run(["apt-get", "install", "-y", *pkgs], capture=False, env=env)


def ensure(bins: list[str], *, auto: bool = True) -> tuple[list[str], list[str]]:
    """Гейт зависимостей: проверить, недостающее доустановить.

    Возвращает (missing_before, installed_pkgs).
    Правила:
      * не Linux и есть недостающее -> предупреждение, не ошибка
        (на целевой системе Linux);
      * Linux без root и есть недостающее -> ошибка с готовой командой;
      * Linux под root -> apt-get update + install автоматически.
    """
    miss = missing(bins)
    if not miss:
        return [], []

    if not is_linux():
        warn("Отсутствуют утилиты: {} (авто-установка только на Linux/apt)"
             .format(", ".join(miss)))
        return miss, []

    if auto:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            pkgs = packages_for(miss)
            raise StepError(
                "не хватает утилит ({}), а apt требует root. Один раз "
                "выполните: sudo apt install -y {} — или перезапустите "
                "команду через sudo".format(", ".join(miss), " ".join(pkgs)))
        pkgs = packages_for(miss)
        if any(b in BUILD_BINS for b in miss) and "zlib1g-dev" not in pkgs:
            pkgs.append("zlib1g-dev")
        apt_install(pkgs)
        still = missing(bins)
        if still:
            raise StepError("после apt install отсутствуют: {} — проверьте "
                            "вывод apt-get выше".format(", ".join(still)))
        ok("Установлены недостающие утилиты: {}".format(", ".join(miss)))
        return miss, pkgs

    return miss, []
