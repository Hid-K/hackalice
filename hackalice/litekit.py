"""Развёртывание lite-kit: клон, патчи, загрузка прошивки, сервер отката."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .config import lite_kit_path
from .console import info, ok, warn
from .models import METHOD_DOWNGRADE, METHOD_DNS, ModelSpec
from .util import StepError, expand, run, which

# Патчи install-firmware.sh из гайда: таймауты curl + прогресс-бар
# (для свежеклонированного репозитория — сразу с 6 ретраями)
CURL_PATCHES = [
    ("curl -fsSL --retry 3 --connect-timeout 10",
     "curl -fL --progress-bar --retry 6 --connect-timeout 10 --max-time 60"),
    ("curl -fsSL --retry 3 --connect-timeout 15",
     "curl -fL --progress-bar --retry 6 --connect-timeout 15 --max-time 180"),
]

# Для уже пропатченного (--max-time есть, но ретраи старые) — поднимаем до 6
CURL_RETRY_UPGRADES = [
    ("--retry 2 --connect-timeout 10", "--retry 6 --connect-timeout 10"),
    ("--retry 2 --connect-timeout 15", "--retry 6 --connect-timeout 15"),
]


def repo_path(cfg) -> Path:
    return lite_kit_path(cfg)


def _confirm_retry() -> bool:
    """Спросить пользователя, повторять ли скачивание (Ctrl-C/нет — стоп)."""
    try:
        import questionary
        return bool(questionary.confirm(
            "Зеркало не ответило. Повторить попытку скачивания?",
            default=True).ask())
    except KeyboardInterrupt:
        raise
    except Exception:
        return False  # неинтерактивный режим — не зацикливаемся


def ensure_repo(cfg) -> Path:
    """Склонировать lite-kit, если каталога ещё нет."""
    if not which("git") or not which("curl"):
        raise StepError("нужны git и curl")
    lk = repo_path(cfg)
    if lk.exists():
        info(f"lite-kit уже в {lk} (обновить вручную: git -C {lk} pull)")
        return lk
    if not cfg.lite_kit_repo:
        raise StepError("репозиторий lite-kit не указан (--lite-kit-repo)")
    info(f"Клонирую {cfg.lite_kit_repo} -> {lk}")
    run(["git", "clone", cfg.lite_kit_repo, str(lk)], capture=False)
    return lk


def patch_install_script(lk: Path) -> int:
    """Таймауты curl + 6 ретраев в install-firmware.sh. Возвращает число замен."""
    script = lk / "install-firmware.sh"
    if not script.exists():
        warn(f"{script} не найден — структура репозитория отличается?")
        return 0
    text = script.read_text(errors="replace")
    if "--max-time" in text:
        # уже патчен таймаутами — возможно, стоит поднять число ретраев
        upgraded = 0
        for old, new in CURL_RETRY_UPGRADES:
            if old in text:
                text = text.replace(old, new)
                upgraded += 1
        if upgraded:
            script.write_text(text)
            ok(f"install-firmware.sh: ретраи curl подняты до 6 ({upgraded}/2)")
        else:
            info("install-firmware.sh: таймауты и ретраи уже настроены")
        return -1
    replaced = 0
    for old, new in CURL_PATCHES:
        if old in text:
            text = text.replace(old, new)
            replaced += 1
    if replaced:
        script.write_text(text)
        ok(f"install-firmware.sh пропатчен ({replaced}/2 замен curl)")
    else:
        warn("Паттерны curl в install-firmware.sh не найдены — "
             "проверьте скрипт вручную")
    return replaced


def find_firmware_zip(lk: Path, model: ModelSpec, firmware: str) -> Path | None:
    fw_dir = lk / "firmware"
    if not fw_dir.exists():
        return None
    exact = list(fw_dir.glob(f"{model.platform}/{firmware}.zip"))
    if exact:
        return exact[0]
    loose = list(fw_dir.glob(f"**/*{firmware}*.zip"))
    return loose[0] if loose else None


def download_firmware(cfg, model: ModelSpec) -> Path | None:
    """Скачать проверенную прошивку (только для метода downgrade)."""
    if model.method == METHOD_DNS:
        raise StepError(
            f"для {model.name} прошивка не нужна (метод «подмена DNS»): "
            f"сервер работает в обычном режиме — выполните serve")
    lk = ensure_repo(cfg)
    patch_install_script(lk)
    firmware = cfg.firmware or model.firmware
    if not firmware:
        raise StepError(
            f"для модели {model.name} версия прошивки для отката ещё не "
            f"добавлена в реестр (hackalice/models.py)")
    if model.platform == "unknown":
        raise StepError(
            f"для модели {model.name} платформа ещё не определена — "
            f"заполните ModelSpec в hackalice/models.py")

    # уже скачана ранее (или перенесена с другой машины) — не качаем
    existing = find_firmware_zip(lk, model, firmware)
    if existing:
        size = existing.stat().st_size
        if size >= 50 * 1024 * 1024:
            ok(f"Прошивка уже на месте ({size // (1024 * 1024)} МБ) — "
               f"скачивание пропущено: {existing}")
            ensure_downgrade_marker(lk, needed=True)
            return existing
        warn(f"Найденный файл подозрительно мал ({size} б) — перекачаю")

    info(f"Загружаю прошивку {firmware} для {model.platform} "
         f"(зеркало может быть медленным; при фейле спросим про повтор)...")
    attempt = 0
    while True:
        attempt += 1
        if attempt > 1:
            info(f"Повторная попытка скачивания #{attempt}...")
        res = run(["bash", "./install-firmware.sh", "-p", model.platform,
                   "-v", firmware],
                  cwd=str(lk), check=False, capture=False)
        if res.returncode == 0:
            break
        warn(f"Скачивание не удалось (попытка {attempt}).")
        if not _confirm_retry():
            raise StepError(
                "зеркало недоступно/файл не скачался. Варианты: повторить "
                "позже (команда fw), либо положить update.zip вручную в "
                f"{lk}/firmware/{model.platform}/<версия>/update.zip "
                "(скачав на любой сети с доступом к зеркалу)")
    zip_path = find_firmware_zip(lk, model, firmware)
    if zip_path:
        ok(f"Прошивка на месте: {zip_path}")
    else:
        warn(f"Установщик отчитался об успехе, но .zip не найден в "
             f"{lk}/firmware — сервер может не найти прошивку, проверьте "
             f"каталог вручную")
    ensure_downgrade_marker(lk, needed=True)
    return zip_path


def _firmware_dest(cfg, model: ModelSpec) -> Path:
    firmware = cfg.firmware or model.firmware
    dest_dir = repo_path(cfg) / "firmware" / model.platform / firmware
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / "update.zip"


def place_firmware_from_url(cfg, model: ModelSpec, url: str) -> Path:
    """Скачать update.zip сторонним curl в правильный layout firmware/.

    Зеркало lite-kit может быть недоступно — если URL прошивки известен
    (другое зеркало/официальный CDN/локальная копия), качаем напрямую.
    """
    dest = _firmware_dest(cfg, model)
    info(f"Качаю {url} -> {dest}")
    res = run(["curl", "-fL", "--progress-bar", "--retry", "6",
               "--connect-timeout", "15", "--max-time", "3600",
               "-o", str(dest), url], check=False, capture=False)
    if res.returncode != 0 or not dest.exists():
        raise StepError(f"скачивание не удалось (curl код {res.returncode})")
    size = dest.stat().st_size
    if size < 50 * 1024 * 1024:
        raise StepError(f"файл подозрительно мал ({size} байт) — "
                        f"это не update.zip?")
    ok(f"Прошивка уложена: {dest} (~{size // (1024 * 1024)} МБ)")
    ensure_downgrade_marker(repo_path(cfg), needed=True)
    return dest


def place_firmware_from_file(cfg, model: ModelSpec, src: Path) -> Path:
    """Подложить уже скачанный update.zip в правильный layout firmware/."""
    src = Path(expand(str(src)))
    if not src.exists():
        raise StepError(f"файл не найден: {src}")
    dest = _firmware_dest(cfg, model)
    import shutil as _shutil
    _shutil.copy2(src, dest)
    size = dest.stat().st_size
    if size < 50 * 1024 * 1024:
        warn(f"файл мал ({size} байт) — проверьте, что это именно update.zip")
    ok(f"Прошивка уложена: {dest} (~{size // (1024 * 1024)} МБ)")
    ensure_downgrade_marker(repo_path(cfg), needed=True)
    return dest


def ensure_downgrade_marker(lk: Path, needed: bool) -> None:
    """Маркер режима downgrade: нужен только методу METHOD_DOWNGRADE.

    Без маркера equasar_server работает в обычном режиме — отвечает на
    get_sync_info активацией ADB (метод «подмена DNS»).
    """
    (lk / "run").mkdir(parents=True, exist_ok=True)
    marker = lk / "run" / "downgrade"
    if needed:
        marker.touch()
    else:
        marker.unlink(missing_ok=True)


def server_log_path(cfg) -> Path:
    return repo_path(cfg) / "run" / "server.log"


def start_server_bg(cfg, model: ModelSpec) -> int:
    """Запустить equasar_server.py в фоне; вернуть PID.

    Перед запуском останавливает предыдущий инстанс (если был — например,
    после падения на более ранней фазе), чтобы не получить два сервера,
    дерущихся за порты 53/80/443.
    """
    lk = repo_path(cfg)
    script = lk / "scripts" / "equasar_server.py"
    if not script.exists():
        raise StepError(f"не найден {script} — сначала выполните команду fw")
    stop_server(cfg)  # на случай висящего предыдущего инстанса
    ensure_downgrade_marker(lk, needed=(model.method == METHOD_DOWNGRADE))
    (lk / "run").mkdir(parents=True, exist_ok=True)
    log = open(server_log_path(cfg), "w")
    proc = subprocess.Popen(["python3", "-u", str(script)], cwd=str(lk),
                            stdout=log, stderr=subprocess.STDOUT)
    (lk / "run" / "server.pid").write_text(str(proc.pid))

    # Health-check: если порты 80/443 заняты (старый инстанс и т.п.) —
    # сервер не сможет отвечать колонке; падаем сразу с понятной ошибкой.
    time.sleep(2)
    try:
        log_head = server_log_path(cfg).read_text(errors="replace")[:4000]
    except OSError:
        log_head = ""
    if "Address already in use" in log_head:
        stop_server(cfg)
        raise StepError(
            "Порты 80/443 заняты другим процессом — колонке некому отвечать.\n"
            "Освободите и перезапустите:\n"
            "  sudo pkill -f equasar_server.py\n"
            "  sudo ss -ltnp | grep -E ':(80|443)\\b'   # должно быть пусто")

    mode = "downgrade (отдача прошивки)" if model.method == METHOD_DOWNGRADE \
        else "обычный (активация ADB через get_sync_info)"
    ok(f"Сервер отката запущен в фоне: PID {proc.pid}, режим «{mode}», "
       f"лог {server_log_path(cfg)}")
    return proc.pid


def start_server_fg(cfg, model: ModelSpec) -> None:
    """Запустить сервер на переднем плане (до Ctrl+C)."""
    lk = repo_path(cfg)
    script = lk / "scripts" / "equasar_server.py"
    if not script.exists():
        raise StepError(f"не найден {script} — сначала выполните команду fw")
    ensure_downgrade_marker(lk, needed=(model.method == METHOD_DOWNGRADE))
    info("Запуск на переднем плане (Ctrl+C — остановить):")
    os.execvp("python3", ["python3", "-u", str(script)])


def stop_server(cfg) -> None:
    """Остановить сервер(ы) lite-kit: по pid-файлу + добить все оставшиеся."""
    lk = repo_path(cfg)
    pid_file = lk / "run" / "server.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)
            ok(f"Сервер lite-kit остановлен (PID {pid})")
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)
    # Страховка: pid-файл мог указывать на уже мёртвый процесс, пока жив
    # другой инстанс (перезаписи при падениях). Добиваем ВСЕ equasar_server.
    res = run("pkill -f equasar_server.py", check=False, capture=True, quiet=True)
    if res.returncode == 0:
        ok("Остановлены дополнительные инстансы equasar_server (pkill)")
