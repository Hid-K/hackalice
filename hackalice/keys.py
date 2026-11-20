"""Хранилище SSH-ключей.

Ключи лежат именованными парами (приватный + .pub) в каталоге хранилища
(по умолчанию ~/.config/hackalice/keys, меняется --keys-dir / keys_dir).

Один ключ на несколько колонок или свой на каждую: маппинг
«метка колонки -> ключ» хранится в конфиге (config.speakers) и
заполняется визардом/командой add-key.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import keys_default_dir, save_config
from .console import ok
from .util import StepError, expand, run


def keys_dir(cfg) -> Path:
    raw = (cfg.keys_dir or "").strip()
    path = Path(expand(raw)) if raw else keys_default_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise StepError(f"не удалось создать каталог ключей {path}: {e}")
    return path


def key_path_for(cfg, name: str) -> Path:
    """Путь к приватному ключу по имени (валидация имени)."""
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise StepError("имя ключа: только латиница, цифры, '.', '_', '-'")
    return keys_dir(cfg) / name


def pub_for(priv: Path) -> Path:
    return Path(str(priv) + ".pub")


def _fingerprint(pub: Path) -> str:
    try:
        res = run(["ssh-keygen", "-lf", str(pub)],
                  check=False, capture=True, quiet=True)
    except StepError:
        return "(ssh-keygen недоступен)"
    out = (res.stdout or "").strip()
    return out.splitlines()[0] if out else "(файл не читается)"


def list_keys(cfg) -> list[dict]:
    """Все ключи хранилища: имя, путь, fingerprint, наличие .pub."""
    out: list[dict] = []
    for priv in sorted(keys_dir(cfg).iterdir()):
        if priv.name.endswith(".pub") or priv.name.startswith("."):
            continue
        if not priv.is_file():
            continue
        pub = pub_for(priv)
        out.append({
            "name": priv.name,
            "path": str(priv),
            "has_pub": pub.exists(),
            "fingerprint": _fingerprint(pub) if pub.exists() else "-",
        })
    return out


def create_key(cfg, name: str, force: bool = False) -> Path:
    """Сгенерировать новую пару ed25519 в хранилище."""
    path = key_path_for(cfg, name)
    if path.exists() and not force:
        raise StepError(f"ключ '{name}' уже существует: {path} — "
                        f"выберите другое имя или добавьте --force")
    res = run(["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "",
               "-C", f"hackalice-{path.name}"],
              check=False, capture=True)
    if res.returncode != 0:
        tail = ((res.stderr or "") + (res.stdout or "")).strip()
        raise StepError(f"ssh-keygen вернул код {res.returncode}: {tail[:400]}")
    if not path.exists() or not pub_for(path).exists():
        raise StepError(f"ssh-keygen отработал, но файлы не созданы: "
                        f"{path} / {pub_for(path)}")
    ok(f"Ключ создан: {path} (+ {path.name}.pub)")
    return path


def use_key(cfg, name: str) -> str:
    """Сделать ключ активным для последующих установок."""
    path = key_path_for(cfg, name)
    if not path.exists():
        raise StepError(f"ключ '{name}' не найден в {keys_dir(cfg)}")
    cfg.ssh_key = str(path)
    save_config(cfg)
    ok(f"Активный ключ: {cfg.ssh_key}")
    return str(path)
