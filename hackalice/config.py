"""Конфигурация приложения: сохранение выборов визарда, пути, константы."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# --- константы проекта -------------------------------------------------------

DEFAULT_REPO = "https://github.com/krevetkqqq/lite-kit.git"
FW_LITE_1 = "0.369.1.46.4778262442.20261013.297"   # прошивка с открытым adbd
DEFAULT_SSH_KEY = "~/.ssh/id_alice"
DEFAULT_BUILD_DIR = "/tmp/hackalice_build"
DEFAULT_LITE_KIT_DIR = "~/lite-kit"
DEFAULT_SSID = "AliceSetup"
DEFAULT_AP_PASSWORD = "password123"


def _config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "hackalice"


CONFIG_PATH = _config_dir() / "config.json"


def keys_default_dir() -> Path:
    """Хранилище ключей по умолчанию: рядом с конфигом."""
    return _config_dir() / "keys"


@dataclass
class AppConfig:
    """Выборы пользователя (визард сохраняет, CLI-команды читают)."""

    model: str = "lite-1"
    inet_iface: str = ""
    ap_iface: str = ""
    ssid: str = DEFAULT_SSID
    ap_password: str = DEFAULT_AP_PASSWORD
    lite_kit_dir: str = DEFAULT_LITE_KIT_DIR
    lite_kit_repo: str = DEFAULT_REPO
    firmware: str = ""            # пусто -> берётся из модели
    ssh_key: str = DEFAULT_SSH_KEY
    build_dir: str = DEFAULT_BUILD_DIR
    dropbear_bin: str = ""        # путь к готовому бинарнику ("" -> собирать)
    device_ip: str = ""           # запоминается после wait-device/install
    timeout: int = 900            # ожидание колонки, сек
    keys_dir: str = ""            # хранилище ключей ("" -> keys_default_dir)
    label: str = ""               # метка текущей колонки ("кухня" и т.п.)
    device_mac: str = ""          # MAC колонки (для поиска после смены IP)
    hotspot_static_ip: str = "10.42.0.200"  # статический IP колонки на хотспоте
    fw_source: str = "auto"       # источник прошивки: auto | torrent | mirror
    torrent_proxy: str = ""       # SOCKS5-прокси для P2P (HOST:PORT), "" = нет
    # маппинг «метка колонки» -> {"key": путь, "ip": адрес, "mac": MAC}
    speakers: dict = field(default_factory=dict)


def load_config() -> AppConfig:
    cfg = AppConfig()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    known = {f.name for f in fields(AppConfig)}
    for key, value in data.items():
        if key in known and value is not None:
            setattr(cfg, key, value)
    return cfg


def save_config(cfg: AppConfig) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        from .console import warn
        warn(f"не удалось сохранить конфиг {CONFIG_PATH}: {e}")


def firmware_for(cfg: AppConfig, model) -> str:
    """Актуальная версия прошивки: из конфига или из модели."""
    return cfg.firmware or getattr(model, "firmware", "")


def lite_kit_path(cfg: AppConfig) -> Path:
    from .util import expand
    return Path(expand(cfg.lite_kit_dir))
