"""Интерактивный мастер: проводит пользователя от настройки сети до root-SSH."""
from __future__ import annotations

from questionary import Choice
from rich.table import Table

from . import (deps, device, dropbear, installer, keys, litekit, netenv)
from .console import (banner, dim, err, info, ok, panel, wait_enter,
                      warn)
from .config import (AppConfig, CONFIG_PATH, DEFAULT_AP_PASSWORD,
                     DEFAULT_SSID, firmware_for, load_config, save_config)
from .models import (METHOD_DNS, METHOD_DOWNGRADE, RESET_GENERIC,
                     choose_model)
from .util import StepError, expand, require_root

DISCLAIMER = """\
Инструмент предназначен для СВОИХ устройств.
Откат прошивки, аппаратный сброс и блокировка OTA:
  * стирают настройки колонки;
  * скорее всего лишают гарантии;
  * выполняются на ваш риск.
Физические шаги (кнопки на колонке, телефон) скрипт сделать не может —
он покажет инструкцию и дождётся подтверждения.
"""

BINDING_INSTRUCTIONS = """\
ПРИВЯЗКА НА ТЕЛЕФОНЕ:
 1. Wi-Fi на телефоне НЕ выключайте — отключитесь от текущей сети
    (приложению нужно видеть список доступных сетей). Интернет у телефона
    должен остаться: включите мобильные данные.
 2. «Дом с Алисой» -> «+» -> «Устройство с Алисой» -> выберите станцию.
 3. На этапе выбора сети укажите SSID «{ssid}», пароль «{password}».
 4. {extra}
"""


class _Abort(Exception):
    """Пользователь отменил диалог (или диалог невозможен)."""


# ---------------------------------------------------------------------------
# Диалоговые помощники
# ---------------------------------------------------------------------------

def _select(message, choices, default=None):
    import questionary
    try:
        ans = questionary.select(message, choices=choices,
                                 default=default).ask()
    except Exception:
        raise _Abort("не удалось открыть интерактивный диалог — "
                     "запустите в терминале (TTY)")
    if ans is None:
        raise _Abort("отменено")
    return ans


def _text(message, default=""):
    import questionary
    try:
        ans = questionary.text(message, default=default).ask()
    except Exception:
        raise _Abort("не удалось открыть интерактивный диалог — "
                     "запустите в терминале (TTY)")
    if ans is None:
        raise _Abort("отменено")
    return (ans or "").strip()


def _confirm(message, default=True):
    import questionary
    try:
        ans = questionary.confirm(message, default=default).ask()
    except Exception:
        raise _Abort("не удалось открыть интерактивный диалог — "
                     "запустите в терминале (TTY)")
    if ans is None:
        raise _Abort("отменено")
    return ans


def _pick_interface(title, ifaces, exclude=(), default=""):
    while True:
        choices = [
            Choice(
                "{}  ({}, {}{}{})".format(
                    i.name, i.kind, i.state or "state?",
                    ", conn: " + i.conn if i.conn else "",
                    ", IP " + i.ipv4 if i.ipv4 else ""),
                value=i.name)
            for i in ifaces if i.name not in exclude
        ]
        choices.append(Choice("Ввести имя интерфейса вручную...",
                              value="__manual__"))
        ans = _select(title, choices,
                      default=default if default else None)
        if ans == "__manual__":
            return _text("Имя интерфейса (например, wlp2s0):", default=default)
        return ans


def _step(num, title):
    banner(f"Шаг {num}. {title}")


def _phase(tag, title):
    banner(f"Фаза {tag}. {title}")


# ---------------------------------------------------------------------------
# Шаги визарда (диалоговая часть)
# ---------------------------------------------------------------------------

def _step_env() -> None:
    _step(1, "Проверка окружения")
    _, installed = deps.ensure(deps.CORE_BINS + deps.BUILD_BINS)
    if installed:
        ok("Автоматически доустановлены пакеты: " + ", ".join(installed))
    ok("Системные зависимости на месте")


def _step_model(cfg: AppConfig):
    _step(2, "Модель устройства")
    model = choose_model(cfg.model)
    cfg.model = model.key
    panel(
        "{}\nПлатформа: {}   Архитектура: {}\n"
        "Прошивка для отката: {}\n\n{}".format(
            model.summary_line, model.platform, model.arch,
            firmware_for(cfg, model) or "(в реестре пока нет — см. notes)",
            model.notes or "-"),
        title="Выбрана модель")
    return model


def _step_interfaces(cfg: AppConfig) -> None:
    _step(3, "Сетевые интерфейсы")
    ifaces = netenv.list_interfaces(with_ap_check=True)
    wifi = netenv.find_wifi_interfaces(ifaces)
    if not wifi and not cfg.ap_iface:
        raise StepError(
            "Wi-Fi адаптеры не найдены. Нужен ВТОРОЙ адаптер под точку "
            "доступа (основной остаётся для интернета).")

    cfg.inet_iface = _pick_interface(
        "Какой интерфейс подключён к интернету?", ifaces,
        default=cfg.inet_iface)
    cfg.ap_iface = _pick_interface(
        "Какой интерфейс отдать под точку доступа для колонки?",
        ifaces, exclude={cfg.inet_iface}, default=cfg.ap_iface)

    ap_info = next((i for i in ifaces if i.name == cfg.ap_iface), None)
    if ap_info is None:
        warn(f"Интерфейс {cfg.ap_iface} не найден в системе — проверю при "
             f"запуске")
    elif ap_info.kind != "wifi":
        warn(f"{cfg.ap_iface} — не Wi-Fi адаптер; точка доступа требует Wi-Fi")
        if not _confirm("Всё равно продолжить?", default=False):
            raise _Abort()
    elif ap_info.ap_support is False:
        warn(f"{cfg.ap_iface}: `iw` не показывает режим AP — hotspot может "
             f"не подняться")
        if not _confirm("Всё равно продолжить?", default=False):
            raise _Abort()
    else:
        ok(f"{cfg.ap_iface}: режим AP поддерживается")


def _step_hotspot(cfg: AppConfig) -> None:
    _step(4, "Точка доступа")
    cfg.ssid = _text("SSID точки доступа:", default=cfg.ssid or DEFAULT_SSID)
    while True:
        cfg.ap_password = _text("Пароль точки доступа (мин. 8 символов):",
                                default=cfg.ap_password or DEFAULT_AP_PASSWORD)
        if len(cfg.ap_password) >= 8:
            break
        warn("Пароль слишком короткий — WPA2 требует минимум 8 символов")

    # Статический lease на хотспоте: колонка всегда получает один и тот же IP
    # (не нужно искать её в ip neigh после каждого ребута/перепрошивки).
    # MAC запоминается автоматически после установки; можно задать сразу.
    mac = _text("MAC колонки для постоянного IP на хотспоте "
                "(Enter — пропустить; запомнится после установки):",
                default=cfg.device_mac)
    if mac:
        cfg.device_mac = netenv.normalize_mac(mac)
        cfg.hotspot_static_ip = _text(
            "Статический IP колонки на хотспоте:",
            default=cfg.hotspot_static_ip or "10.42.0.200")
        ok("На хотспоте колонка всегда получит: {} -> {}".format(
            cfg.device_mac, cfg.hotspot_static_ip))


def _step_key(cfg: AppConfig) -> None:
    """Шаг 5: выбор/генерация ключа + метка колонки (маппинг ключ->колонка)."""
    import time as _time
    from pathlib import Path
    from questionary import Choice

    _step(5, "SSH-ключ")
    stored = keys.list_keys(cfg)
    choices = []
    if Path(expand(cfg.ssh_key)).exists():
        choices.append(Choice("Активный ключ: " + cfg.ssh_key, value="active"))
    for k in stored:
        choices.append(Choice("{}  ({})".format(k["name"], k["fingerprint"]),
                              value="stored:" + k["name"]))
    choices.append(Choice("Создать новый ключ (в хранилище ключей)",
                          value="new"))
    choices.append(Choice("Указать путь к ключу вручную", value="manual"))
    ans = _select("Ключ для доступа к колонке:", choices,
                  default=choices[0] if choices else None)

    if ans == "active":
        pass
    elif ans == "new":
        default_name = "key_{}".format(_time.strftime("%Y%m%d_%H%M"))
        name = _text("Имя нового ключа (латиница/цифры):",
                     default=default_name)
        path = keys.create_key(cfg, name)
        cfg.ssh_key = str(path)
    elif isinstance(ans, str) and ans.startswith("stored:"):
        cfg.ssh_key = str(keys.key_path_for(cfg, ans.split(":", 1)[1]))
    else:
        cfg.ssh_key = _text("Путь к приватному ключу:",
                            default=cfg.ssh_key)
    ok("Ключ для колонки: " + cfg.ssh_key)

    # метка колонки: один ключ на несколько колонок или свой на каждую —
    # соответствие хранится в config.speakers
    cfg.label = _text("Метка колонки (например 'кухня'; Enter — пропустить):",
                      default=cfg.label)


def _step_dropbear_source(cfg: AppConfig) -> None:
    _step(6, "SSH-сервер (dropbear)")
    src = _select(
        "Откуда взять dropbearmulti (статический бинарник для колонки)?",
        [
            Choice("Собрать на этом ПК (нужен deb-src в apt-источниках)",
                   value="build"),
            Choice("Указать путь к готовому бинарнику", value="prebuilt"),
        ],
        default="prebuilt" if cfg.dropbear_bin else "build",
    )
    if src == "prebuilt":
        cfg.dropbear_bin = _text(
            "Путь к статическому dropbearmulti:",
            default=cfg.dropbear_bin)
    else:
        cfg.dropbear_bin = ""


def _step_review(cfg: AppConfig, model) -> None:
    _step(7, "Подтверждение")
    _fw = firmware_for(cfg, model)
    if _fw:
        fw = _fw
    elif model.method == METHOD_DNS:
        fw = "не требуется (DNS-метод)"
    else:
        fw = "(нет в реестре)"
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Модель", model.summary_line)
    table.add_row("Платформа / архитектура", f"{model.platform} / {model.arch}")
    table.add_row("Метод", model.method_label)
    table.add_row("Прошивка отката", fw)
    table.add_row("Интернет-интерфейс", cfg.inet_iface)
    table.add_row("Интерфейс точки доступа", cfg.ap_iface)
    table.add_row("SSID / пароль", f"{cfg.ssid} / {cfg.ap_password}")
    table.add_row("lite-kit", cfg.lite_kit_dir)
    table.add_row("Репозиторий lite-kit", cfg.lite_kit_repo)
    table.add_row("dropbear", cfg.dropbear_bin or "собрать на этом ПК")
    table.add_row("SSH-ключ", cfg.ssh_key)
    from .console import console
    console.print(table)
    if not _confirm("Запускаем? Сеть будет перенастроена, колонка уйдёт "
                    "в сброс", default=False):
        raise _Abort()
    save_config(cfg)


# ---------------------------------------------------------------------------
# Основной поток
# ---------------------------------------------------------------------------

def run_wizard() -> int:
    banner("hackalice — интерактивный мастер рутирования Яндекс Станций")
    panel(DISCLAIMER, title="Важно", border="yellow")
    if not _confirm("Подтверждаю: устройство принадлежит мне, риски понятны",
                    default=False):
        err("Отменено")
        return 1

    require_root()

    cfg = load_config()
    if cfg.inet_iface or cfg.ap_iface or cfg.device_ip:
        if not _confirm(
                f"Найдены сохранённые настройки ({CONFIG_PATH}) — "
                f"использовать их как значения по умолчанию?", default=True):
            cfg = AppConfig()

    try:
        _step_env()
        model = _step_model(cfg)
        _step_interfaces(cfg)
        _step_hotspot(cfg)
        _step_key(cfg)
        _step_dropbear_source(cfg)
        _step_review(cfg, model)
    except _Abort as e:
        err(str(e) or "Отменено пользователем")
        return 1

    server_started = False
    try:
        # ---- Фаза A: сеть ----
        _phase("A", "Сеть: точка доступа + DNS + NAT")
        netenv.hotspot_up(cfg)

        # ---- Фаза B: lite-kit (прошивка — только для downgrade-метода) ----
        _phase("B", "lite-kit: сервер разблокировки")
        cfg.lite_kit_repo = _text("Git-репозиторий lite-kit:",
                                  default=cfg.lite_kit_repo)
        save_config(cfg)
        if model.method == METHOD_DOWNGRADE:
            litekit.download_firmware(cfg, model)
        else:
            info("Метод «подмена DNS» — прошивка не требуется; сервер будет "
                 "работать в обычном режиме (активация ADB).")
            litekit.ensure_repo(cfg)

        # ---- Фаза C: dropbear ----
        _phase("C", "Сборка dropbear для колонки")
        dropbear_path = dropbear.resolve_dropbear(cfg, model)

        # ---- Фаза D: сервер отката ----
        _phase("D", "Сервер отката в фоне")
        litekit.start_server_bg(cfg, model)
        server_started = True

        # ---- Фазы E/F: ручные шаги (пропускаются при повторном запуске) ----
        if _confirm("Колонка уже сброшена, привязана к этой сети и доступна "
                    "(повторный запуск после сбоя)?", default=False):
            info("Пропускаю сброс и привязку — сразу жду колонку по ADB")
        else:
            _phase("E", "РУЧНОЙ ШАГ: аппаратный сброс колонки")
            panel(model.reset_instructions or RESET_GENERIC,
                  title="Аппаратный сброс", border="yellow")
            wait_enter("Выполните сброс по инструкции выше; когда колонка "
                       "готова к настройке — Enter")

            _phase("F", "РУЧНОЙ ШАГ: привязка в приложении «Дом с Алисой»")
            extra = ("Колонка скачает прошивку (~67 МБ) и перезагрузится "
                     "(3-5 мин) — НЕ выключайте питание." if
                     model.method == METHOD_DOWNGRADE else
                     "Колонка подключится, и сервер автоматически активирует "
                     "её сетевой ADB.")
            panel(BINDING_INSTRUCTIONS.format(ssid=cfg.ssid,
                                              password=cfg.ap_password,
                                              extra=extra),
                  border="yellow")
            wait_enter("Когда начнёте привязку в приложении — Enter")

        # ---- Фаза G: ожидание колонки ----
        _phase("G", "Ожидание колонки (root по ADB)")
        device.wait_for_root(
            cfg, model,
            log_path=str(litekit.server_log_path(cfg)),
            on_status=dim,
            on_log=lambda line: dim("srv| " + line))
        save_config(cfg)

        # ---- Фаза H: установка SSH ----
        _phase("H", "Установка SSH и блокировка OTA")
        installer.install_ssh(cfg, model, dropbear_path)
        if cfg.label:
            cfg.speakers[cfg.label] = {"key": cfg.ssh_key,
                                       "ip": cfg.device_ip,
                                       "mac": cfg.device_mac}
            save_config(cfg)
            ok("Записано в конфиг: «{}» -> ключ {}, IP {}{}".format(
                cfg.label, cfg.ssh_key, cfg.device_ip,
                ", MAC " + cfg.device_mac if cfg.device_mac else ""))
    except _Abort as e:
        if server_started:
            litekit.stop_server(cfg)
        err(str(e) or "Отменено пользователем")
        return 1
    except StepError:
        if server_started:
            litekit.stop_server(cfg)
        raise
    except KeyboardInterrupt:
        if server_started:
            litekit.stop_server(cfg)
        raise
    except Exception:
        # любой неожиданный crash (баг и т.п.): не оставляем висеть сервер
        if server_started:
            litekit.stop_server(cfg)
        raise

    # ---- Фаза I: очистка ----
    _phase("I", "Очистка")
    litekit.stop_server(cfg)
    if _confirm("Выключить hotspot и убрать DNS-конфиг? (колонка, если она "
                "ещё не переведена в домашнюю сеть, останется БЕЗ СЕТИ — "
                "поднять обратно: sudo hackalice net-up)", default=True):
        netenv.hotspot_down(cfg)

    banner("ГОТОВО")
    ota_warn = ""
    if model.platform == "cucumber":
        ota_warn = ("\nВАЖНО (Миди): блок OTA «заглушкой+hosts» на этой "
                    "модели ненадёжен — резолвер unbound обходит /etc/hosts. "
                    "Перед переводом в домашнюю сеть заблокируйте "
                    "update-домены на РОУТЕРЕ для MAC колонки "
                    "(update.quasar.yandex.net, ota.quasar.yandex.net, "
                    "static-ota.quasar.yandex.net, update-yandex.s3.yandex.net), "
                    "иначе прошивка может обновиться и закрыть доступ.\n")
    panel(
        "Колонка рутирована, SSH закреплён{}.\n"
        "\n"
        "{}\n"
        "Возврат в домашнюю сеть:\n"
        " 1. Зажмите центральную кнопку на 5 сек (режим настройки Wi-Fi).\n"
        " 2. В «Дом с Алисой» привяжите к домашнему Wi-Fi.\n"
        " 3. Закрепите IP колонки в Static Leases роутера.\n"
        " 4. Подключение: ssh -i {} root@<домашний_IP>\n"
        "\n"
        "Отвязка от аккаунта без потери root:\n"
        "  sudo hackalice unbind".format(
            " и OTA заблокирована" if not ota_warn else " закреплён",
            ota_warn, cfg.ssh_key),
        title="Следующие шаги", border="green")
    return 0
