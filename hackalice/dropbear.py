"""Сборка статического dropbearmulti под архитектуру модели (aarch64)."""
from __future__ import annotations

import os
import re
import shutil as _shutil
from pathlib import Path

from .console import info, ok, warn
from .models import ModelSpec
from .util import StepError, expand, run, which


def _default_src_line() -> str:
    """Стандартная deb-src строка по дистрибутиву (когда нечего зеркалить)."""
    try:
        data = Path("/etc/os-release").read_text(errors="replace")
    except OSError:
        data = ""
    m_id = re.search(r'^ID="?([^"\n]+)"?', data, re.M)
    m_code = re.search(r'^VERSION_CODENAME="?([^"\n]+)"?', data, re.M)
    os_id = (m_id.group(1) if m_id else "debian").strip()
    codename = (m_code.group(1) if m_code else "").strip()
    if os_id == "kali":
        return (f"deb-src http://http.kali.org/kali {codename or 'kali-rolling'} "
                f"main contrib non-free non-free-firmware")
    return (f"deb-src http://deb.debian.org/debian {codename or 'trixie'} main")


def _deb_src_command() -> str:
    """Команда ручного включения deb-src (запасной путь, если авто не сработал)."""
    return (f"echo '{_default_src_line()}' > "
            f"/etc/apt/sources.list.d/src.list && apt-get update")


def _deb_src_enabled() -> bool:
    """Есть ли deb-src в apt-источниках (классика или deb822)."""
    paths = ["/etc/apt/sources.list"]
    paths += [str(p) for p in Path("/etc/apt/sources.list.d").glob("*")] \
        if Path("/etc/apt/sources.list.d").is_dir() else []
    for path in paths:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"^\s*deb-src\b", text, re.M) or "deb-src" in text:
            return True
    return False


# --- автоматическое включение deb-src ---------------------------------------

SRC_LIST_FILE = Path("/etc/apt/sources.list.d/hackalice-deb-src.list")


def _iter_source_files():
    files = [Path("/etc/apt/sources.list")]
    d = Path("/etc/apt/sources.list.d")
    if d.is_dir():
        files += sorted(p for p in d.iterdir() if p.is_file())
    return [p for p in files if p.exists()]


def _merge_brackets(tokens: list[str]) -> list[str]:
    """Склеить токены вида '[opt with spaces]' в один элемент.

    apt-опции ([arch=amd64 signed-by=/x.gpg]) и cdrom-URI с пробелами
    (cdrom:[Debian 13]/) разбиваются split() на куски — собираем обратно.
    """
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if "[" in t and "]" not in t:
            while i + 1 < len(tokens) and "]" not in tokens[i + 1]:
                i += 1
                t += " " + tokens[i]
            if i + 1 < len(tokens):
                i += 1
                t += " " + tokens[i]
        out.append(t)
        i += 1
    return out


def _deb_src_lines_from(text: str) -> list[str]:
    """deb-src строки из one-line формата (sources.list / *.list)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = _merge_brackets(s.split())
        if parts[0] != "deb":        # deb-src уже источник — не дублируем
            continue
        rest = [p for p in parts[1:]
                if not (p.startswith("[") and p.endswith("]"))]  # [опции]
        if not rest or len(rest) < 3:
            continue
        if rest[0].startswith(("cdrom:", "file:")):
            continue
        out.append("deb-src " + " ".join(rest))
    return out


def _deb822_src_lines(text: str) -> list[str]:
    """deb-src строки из deb822 формата (*.sources)."""
    out: list[str] = []
    stanza: dict[str, str] = {}
    skip = False                # стanza уже содержит deb-src — не дублируем

    def flush():
        nonlocal skip
        if not skip and stanza:
            if stanza.get("Enabled", "").strip().lower() != "no":
                comps = stanza.get("Components", "").strip()
                for uri in _merge_brackets(
                        stanza.get("URIs", "").split()):
                    if uri.startswith(("cdrom:", "file:")):
                        continue
                    line = "deb-src {} {}".format(
                        uri, stanza.get("Suites", "").strip())
                    if comps:
                        line += " " + comps
                    out.append(line)
        stanza.clear()
        skip = False

    for line in text.splitlines():
        s = line.strip()
        if not s:                       # пустая строка = конец стanzi
            flush()
            continue
        if s.startswith("#") or ":" not in s:
            continue
        key, _, value = s.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "Types" and "deb-src" in value:
            skip = True
            stanza.clear()
            continue
        stanza[key] = value
    flush()
    return out


def ensure_deb_src() -> None:
    """Включить deb-src автоматически: зеркалируем существующие источники.

    Пишет /etc/apt/sources.list.d/hackalice-deb-src.list с deb-src-копиями
    всех deb-источников системы (one-line и deb822), затем apt-get update.
    """
    if _deb_src_enabled():
        return
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise StepError("deb-src не включён, а добавление требует root. "
                        "Один раз выполните:\n  " + _deb_src_command())

    lines: list[str] = []
    for p in _iter_source_files():
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        parser = _deb822_src_lines if p.suffix == ".sources" \
            else _deb_src_lines_from
        lines += parser(text)
    if not lines:
        lines = [_default_src_line()]
        warn("Не нашёл существующих deb-источников — добавляю стандартный "
             "deb-src для вашего дистрибутива")
    unique: list[str] = []
    for line in lines:
        if line not in unique:
            unique.append(line)
    try:
        SRC_LIST_FILE.write_text(
            "# deb-src для apt-get source (dropbear) — создано hackalice\n"
            + "\n".join(unique) + "\n", encoding="utf-8")
    except OSError as e:
        raise StepError(f"не удалось записать {SRC_LIST_FILE}: {e}\n"
                        "Включите deb-src вручную:\n  " + _deb_src_command())
    ok(f"deb-src включён автоматически: {SRC_LIST_FILE} "
       f"({len(unique)} источников)")
    info("apt-get update...")
    run("apt-get update", capture=False)
    if not _deb_src_enabled():
        raise StepError("apt не видит deb-src после включения — "
                        "сделайте вручную:\n  " + _deb_src_command())


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

    if not _deb_src_enabled():
        ensure_deb_src()

    info("Забираю исходники dropbear...")
    res = run("apt-get update && apt-get source dropbear",
              cwd=str(build), check=False, capture=False)
    if res.returncode != 0:
        raise StepError(
            "apt-get source не сработал. Команда включения deb-src для "
            "вашего дистрибутива:\n  " + _deb_src_command())

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
