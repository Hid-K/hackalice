"""Скачивание прошивок по BitTorrent (libtorrent + DHT + opentrackr).

.torrent-файлы лежат в firmware_torrents/ репозитория (хеши совпадают с
данными). Пользовательский каталог ~/.config/hackalice/torrents/ — куда
можно положить свои .torrent. Для загрузки нужен python-модуль libtorrent:
ensure_libtorrent() доустанавливает его автоматически (apt на системном
Python, pip в venv).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from .config import _config_dir
from .console import info, ok, warn
from .util import StepError, is_linux, run

DHT_BOOTSTRAP = ("dht.libtorrent.org:25401,"
                 "router.bittorrent.com:6881,"
                 "router.utorrent.com:6881")
LISTEN_IFACES = "0.0.0.0:6881"

# торрент считается «зависшим», если прогресс не меняется столько минут
STALL_MIN_DEFAULT = 10

LIBTORRENT_HINT = (
    "  sudo apt install python3-libtorrent   # системный Python (Kali/Debian)\n"
    "  pip install libtorrent                # venv")


def repo_torrents_dir() -> Path:
    """firmware_torrents/ в корне репозитория (рядом с пакетом hackalice)."""
    return Path(__file__).resolve().parent.parent / "firmware_torrents"


def user_torrents_dir() -> Path:
    """Пользовательские .torrent: ~/.config/hackalice/torrents/."""
    return _config_dir() / "torrents"


def find_torrent_file(model, firmware: str) -> Path | None:
    """Найти .torrent по платформе/версии прошивки (точно или по подстроке)."""
    if not firmware:
        return None
    exact_name = "{}-{}.torrent".format(model.platform, firmware)
    for d in (repo_torrents_dir(), user_torrents_dir()):
        exact = d / exact_name
        if exact.exists():
            return exact
        loose = sorted(d.glob("*{}.torrent".format(firmware)))
        if loose:
            return loose[0]
    return None


def parse_proxy(spec: str) -> tuple[str, int]:
    """'127.0.0.1:10808' -> ('127.0.0.1', 10808)."""
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        raise StepError(
            "прокси указывается как HOST:PORT, например 127.0.0.1:10808")
    return host, int(port)


def ensure_libtorrent():
    """Импортировать libtorrent, при отсутствии — доустановить и повторить."""
    try:
        import libtorrent as lt  # noqa: пользователь мог поставить заранее
        return lt
    except ImportError:
        pass

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if is_linux() and not in_venv:
        # системный Python на целевых Debian/Kali: ставим пакет apt
        from . import deps
        try:
            deps.apt_install(["python3-libtorrent"])
        except StepError as e:
            raise StepError(
                "не удалось доустановить python3-libtorrent ({}). "
                "Поставьте вручную:\n{}".format(e, LIBTORRENT_HINT))
        try:
            import libtorrent as lt
            ok("Установлен python3-libtorrent")
            return lt
        except ImportError:
            raise StepError(
                "apt отчитался об успехе, но модуль libtorrent не "
                "импортируется. Поставьте вручную:\n" + LIBTORRENT_HINT)

    # venv (или не-Linux): пробуем pip текущего интерпретатора
    info("Ставлю libtorrent через pip (может занять минуту)...")
    res = run([sys.executable, "-m", "pip", "install", "libtorrent"],
              check=False, capture=True, quiet=True)
    try:
        import libtorrent as lt
        ok("Установлен libtorrent (pip)")
        return lt
    except ImportError:
        tail = ((res.stderr or "") + (res.stdout or "")).strip()[-400:]
        raise StepError("не удалось установить libtorrent: {}\n"
                        "Поставьте вручную:\n{}".format(tail or "pip failed",
                                                        LIBTORRENT_HINT))


def download_torrent(torrent_path: Path, save_dir: Path, proxy: str = "",
                     stall_min: int = STALL_MIN_DEFAULT):
    """Скачать и проверить торрент. Возвращает каталог с содержимым.

    save_dir/... — libtorrent сам восстановит структуру из .torrent
    (для наших прошивок: <версия>/update.zip). При зависании прогресса
    дольше stall_min минут — StepError (нет сидов).
    """
    lt = ensure_libtorrent()
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "listen_interfaces": LISTEN_IFACES,
        "enable_dht": 1,
        "dht_bootstrap_nodes": DHT_BOOTSTRAP,
        "announce_to_all_trackers": 1,
        "active_downloads": -1,
        "active_seeds": -1,
        "active_limit": -1,
    }
    if proxy:
        phost, pport = parse_proxy(proxy)
        settings.update({
            "proxy_hostname": phost,
            "proxy_port": pport,
            "proxy_type": 2,               # SOCKS5
            "proxy_hostnames": 1,          # трекеры/DHT через прокси
            "proxy_peer_connections": 1,   # пиры через прокси
        })
        info("P2P через SOCKS5-прокси {}:{}".format(phost, pport))
    else:
        warn("P2P БЕЗ прокси — ваш IP-адрес будет виден пирам и трекерам")

    ses = lt.session(settings)
    ses.add_dht_node(("router.bittorrent.com", 6881))
    try:
        ti = lt.torrent_info(str(torrent_path))
        atp = lt.add_torrent_params()
        atp.ti = ti
        atp.save_path = str(save_dir)
        atp.flags = lt.torrent_flags.auto_managed
        h = ses.add_torrent(atp)
        info("Торрент: {} ({} МБ) — ищем сидов/пиров...".format(
            ti.name(), ti.total_size() // (1024 * 1024)))

        last_progress = -1.0
        last_change = time.monotonic()
        last_notify = 0.0
        while not h.status().is_seeding:
            st = h.status()
            now = time.monotonic()
            if st.progress != last_progress:
                last_progress = st.progress
                last_change = now
            if now - last_notify >= 30:
                last_notify = now
                info("Торрент: {:.1f}% | пиры {} | {:.0f} КиБ/с".format(
                    st.progress * 100, st.num_peers,
                    st.download_payload_rate / 1024))
            if now - last_change >= stall_min * 60:
                raise StepError(
                    "торрент стоит на месте {} мин (нет сидов?) — попробуйте "
                    "зеркало: sudo hackalice fw --fw-source mirror".format(
                        stall_min))
            time.sleep(2)

        payload = save_dir / ti.name()
        ok("Торрент скачан и проверен (хеши совпали): {}".format(payload))
        return payload
    finally:
        # закрываем сессию: порт 6881 освобождаем, раздачу не держим —
        # прошивка нужна только для сервера отката
        try:
            ses.pause()
        except Exception:
            pass
