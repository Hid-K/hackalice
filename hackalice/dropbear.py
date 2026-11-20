"""Сборка статического dropbearmulti под архитектуру модели (aarch64)."""
from __future__ import annotations

import os
import shutil as _shutil
from pathlib import Path

from .console import info, ok, warn
from .models import ModelSpec
from .util import StepError, expand, run, which


def build_dir(cfg) -> Path:
    return Path(expand(cfg.build_dir))


def target_path(cfg) -> Path:
    return build_dir(cfg) / "dropbearmulti"


# ---------------------------------------------------------------------------
# Проверка бинарника
# ---------------------------------------------------------------------------

def verify_binary(path: str | Path) -> None:
    """Бинарник должен быть ELF нужной архитектуры и статическим."""
    path = Path(path)
    if not path.exists():
        raise StepError(f"файл не найден: {path}")
    if which("file"):
        out = run(["file", "-b", str(path)], capture=True,
                  quiet=True).stdout.strip()
        ok(f"file: {out}")
        if "aarch64" not in out.lower():
            raise StepError(f"бинарник не aarch64: {out}")
        if "statically linked" not in out.lower():
            raise StepError(
                "бинарник НЕ статический — на колонке нет библиотек ноутбука")
    elif which("readelf"):
        out = run(["readelf", "-h", str(path)], capture=True,
                  quiet=True).stdout
        if "AArch64" not in out:
            raise StepError("бинарник не AArch64")
        seg = run(["readelf", "-l", str(path)], capture=True,
                  quiet=True).stdout
        if "INTERP" in seg:
            raise StepError("бинарник динамический (есть INTERP-сегмент)")
        ok("readelf: AArch64, статический")
    else:
        warn("Нет ни file, ни readelf — пропускаю проверку архитектуры")


# ---------------------------------------------------------------------------
# Сборка
# ---------------------------------------------------------------------------

def build(cfg, model: ModelSpec, force: bool = False) -> Path:
    """Собрать статический dropbearmulti; вернуть путь к бинарнику."""
    if model.arch == "unknown":
        raise StepError(f"архитектура для {model.name} не определена — "
                        f"заполните ModelSpec")
    cross_gcc = f"{model.arch}-linux-gnu-gcc"   # aarch64-linux-gnu-gcc
    cross_host = f"{model.arch}-linux-gnu"

    if not which(cross_gcc):
        info(f"Ставлю кросс-компилятор ({cross_gcc}) и зависимости...")
        run("apt-get update", capture=False)
        run("apt-get install -y gcc-aarch64-linux-gnu make zlib1g-dev "
            "dpkg-dev", capture=False)

    build = build_dir(cfg)
    build.mkdir(parents=True, exist_ok=True)
    target = build / "dropbearmulti"

    if target.exists() and not force:
        try:
            verify_binary(target)
            ok(f"Готовый бинарник уже есть: {target} (пересобрать: --force)")
            return target
        except StepError:
            warn("Старый бинарник невалиден — пересобираю")

    info("Забираю исходники dropbear (нужен deb-src в apt-источниках)...")
    res = run("apt-get update && apt-get source dropbear",
              cwd=str(build), check=False, capture=False)
    if res.returncode != 0:
        raise StepError(
            "apt-get source не сработал. Добавьте deb-src, например:\n"
            "  echo 'deb-src http://deb.debian.org/debian trixie main' > \\\n"
            "    /etc/apt/sources.list.d/src.list && apt-get update\n"
            "(подставьте свой релиз: bookworm/trixie)")

    src_dirs = sorted(p for p in build.glob("dropbear-*") if p.is_dir())
    if not src_dirs:
        raise StepError(f"не нашёл распакованный каталог dropbear-* в {build}")
    src = src_dirs[-1]

    (src / "localoptions.h").write_text(
        "#define DROPBEAR_SVR_PASSWORD_AUTH 0\n"
        "#define DROPBEAR_CLI_PASSWORD_AUTH 0\n")
    ok("localoptions.h создан (парольная авторизация выключена — только ключи)")

    env = dict(os.environ)
    env["LDFLAGS"] = "-static"  # форсируем статическую линковку
    run(["./configure", f"--host={cross_host}", "--disable-zlib",
         "--enable-static"], cwd=str(src), env=env, capture=False)
    run(["make", "PROGRAMS=dropbear dbclient dropbearkey scp", "MULTI=1",
         "-j", str(os.cpu_count() or 2)], cwd=str(src), env=env,
        capture=False)

    verify_binary(src / "dropbearmulti")
    _shutil.copy2(src / "dropbearmulti", target)
    ok(f"Готово: {target}")
    return target


def resolve_dropbear(cfg, model: ModelSpec) -> Path:
    """Бинарник из конфига (--dropbear-bin) или свежесобранный."""
    if cfg.dropbear_bin:
        path = expand(cfg.dropbear_bin)
        verify_binary(path)
        return Path(path)
    return build(cfg, model)
