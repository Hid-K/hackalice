"""CLI hackalice: интерактивный визард (по умолчанию) + пошаговые команды."""
from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path

from rich.table import Table

from . import (deps, device, dropbear, installer, keys, litekit, netenv,
               wizard)
from .console import console, dim, err, info, ok, panel, warn
from .config import (AppConfig, firmware_for, lite_kit_path, load_config,
                     save_config)
from .models import REGISTRY, get_model
from .models import METHOD_DNS
from .util import StepError, expand, is_linux, require_root, run

TROUBLESHOOTING = """\
ТИПОВЫЕ ПРОБЛЕМЫ (гайд + hackalice.me + живые запуски):
* колонка в hotspot пишет «нет интернета», а в server.log только
  generate_204 и нет get_sync_info -> DNAT udp/tcp 53 уже включён (фаза A);
  проверьте форвардинг: sudo iptables -vnL DOCKER-USER (счётчик правила
  AP->inet должен расти во время привязки) и что порт 53 занят именно
  dnsmasq NM: sudo ss -lunp | grep ':53 '
* adb: No route to host -> колонка отвалилась от hotspot:
    sudo hackalice net-up && ip neigh show dev <AP-интерфейс>
* «Что-то пошло не так» при привязке -> телефон подключён к точке доступа
  AliceSetup (правильно: Wi-Fi ВКЛЮЧЕН, но отключён от сетей — приложению
  нужно видеть список сетей; интернет — мобильные данные), либо не выдержан
  тайминг сброса (инструкция в визарде, для каждой модели своя)
* install-firmware.sh виснет -> команда fw (патченные --max-time + -p/-v)
* сборка dropbear: «apt-get source» не сработал -> hackalice сам включает
  deb-src (пишет /etc/apt/sources.list.d/hackalice-deb-src.list из ваших
  deb-источников); вручную: grep -r deb-src /etc/apt/sources.list*
* сборка dropbear: configure «C compiler cannot create executables» ->
  нет статической кросс-libc: sudo apt install -y libc6-dev-arm64-cross
  (новые версии build ставят её сами; подробности: config.log в build-каталоге)
* apt install рвётся «Failed to fetch ... Connection failed» -> зеркало
  Kali флапнуло: повторить команду (apt дотянет только недокачанное) или
  сменить зеркало: sudo kali-tweaks -> Network mirroring
* dropbear: crypt() error -> localoptions.h с DROPBEAR_SVR_PASSWORD_AUTH 0
  (уже делается в сборке автоматически)
* после reboot adb refused -> колонка успела обновиться: повторите сброс,
  до install-ssh не выпускайте колонку в WAN
* SSH не поднялся после reboot -> колонка сменила IP (DHCP): новый код
  ищет её по MAC; вручную: ip neigh show dev <AP> и ssh root@<новый IP>
* порты 80/443 заняты (Errno 98) -> sudo pkill -f equasar_server.py,
  проверка: sudo ss -ltnp | grep -E ':(80|443)\\b'
"""


# ---------------------------------------------------------------------------
# Конфиг из аргументов
# ---------------------------------------------------------------------------

def _cfg_from(args) -> AppConfig:
    cfg = load_config()
    for f in fields(AppConfig):
        value = getattr(args, f.name, None)
        if value is not None:
            setattr(cfg, f.name, value)
    return cfg


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def cmd_models(_args) -> int:
    table = Table(title="Реестр моделей Яндекс Станций")
    for col in ("ключ", "модель", "код", "платформа", "арх.", "метод",
                "статус", "прошивка отката"):
        table.add_column(col)
    for m in REGISTRY:
        fw = m.firmware or ("не нужна" if m.method == METHOD_DNS else "-")
        table.add_row(m.key, m.name, m.code or "-", m.platform, m.arch,
                      m.method_label, m.status_label, fw)
    console.print(table)
    dim("Добавление новых моделей — hackalice/models.py (ModelSpec).")
    dim("Источники методов: hackalice.me (Yandex Station Extended) и "
        "lite-kit; проект hackalice с ними не связан.")
    return 0


def cmd_interfaces(_args) -> int:
    if not is_linux():
        warn("Полный список интерфейсов доступен только на Linux")
        return 0
    table = Table(title="Сетевые интерфейсы")
    for col in ("интерфейс", "тип", "состояние", "connection", "IPv4",
                "phy", "режим AP"):
        table.add_column(col)
    for i in netenv.list_interfaces(with_ap_check=True):
        ap = {True: "да", False: "нет", None: "?"}[i.ap_support]
        table.add_row(i.name, i.kind, i.state or "-", i.conn or "-",
                      i.ipv4 or "-", i.phy or "-", ap)
    console.print(table)
    return 0


def cmd_check(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    ok("Запуск от root")
    ok("Системные утилиты на месте (гейт зависимостей отработал при запуске)")
    build_miss = deps.missing_build()
    if build_miss:
        dim("Для сборки dropbear не хватает: " + ", ".join(build_miss) +
            " — доустановятся автоматически при build-dropbear")

    if not is_linux():
        warn("Инструмент рассчитан на Linux (Debian 12/13)")
        return 0

    table = Table(title="Сетевые интерфейсы")
    for col in ("интерфейс", "тип", "состояние", "IPv4", "режим AP"):
        table.add_column(col)
    ifaces = netenv.list_interfaces(with_ap_check=True)
    for i in ifaces:
        ap = {True: "да", False: "нет", None: "?"}[i.ap_support]
        table.add_row(i.name, i.kind, i.state or "-", i.ipv4 or "-", ap)
    console.print(table)

    wifi = netenv.find_wifi_interfaces(ifaces)
    if len(wifi) < 2 and not (cfg.inet_iface and cfg.ap_iface
                              and cfg.inet_iface != cfg.ap_iface):
        warn("Нужно ДВА интерфейса: один интернет (можно Ethernet), "
             "второй Wi-Fi под точку доступа")
    if cfg.ap_iface and cfg.inet_iface:
        ok(f"План: интернет через {cfg.inet_iface}, AP на {cfg.ap_iface}")

    res = run(["ping", "-I", cfg.inet_iface, "-c", "1", "-W", "3", "8.8.8.8"],
              check=False, capture=True, quiet=True)
    if res.returncode == 0:
        ok(f"Интернет через '{cfg.inet_iface}' есть")
    else:
        warn(f"Нет интернета через '{cfg.inet_iface}' — прошивку не скачать")

    try:
        model = get_model(cfg.model)
    except KeyError as e:
        warn(str(e))
        return 0
    fw = firmware_for(cfg, model)
    info(f"Модель: {model.summary_line}")
    info(f"Метод: {model.method_label}")
    info(f"Платформа: {model.platform} / {model.arch}; прошивка: "
         f"{fw or ('(не нужна)' if model.method == METHOD_DNS else '(нет в реестре)')}")
    info(f"Каталог lite-kit: {lite_kit_path(cfg)}"
         f"{' (есть)' if lite_kit_path(cfg).exists() else ' (склонируется)'}")
    ok("Проверка завершена")
    return 0


def cmd_net_up(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    netenv.hotspot_up(cfg)
    return 0


def cmd_net_down(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    netenv.hotspot_down(cfg, flush_nat=args.flush_nat)
    ok("Готово")
    return 0


def cmd_fw(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    model = get_model(cfg.model)
    if model.method == METHOD_DNS:
        raise StepError(
            f"для {model.name} прошивка не нужна (метод «подмена DNS»): "
            f"сразу переходите к serve")
    if args.firmware_url:
        litekit.place_firmware_from_url(cfg, model, args.firmware_url)
    elif args.firmware_file:
        litekit.place_firmware_from_file(cfg, model, Path(expand(args.firmware_file)))
    else:
        litekit.download_firmware(cfg, model, source=cfg.fw_source or "auto")
    return 0


def cmd_serve(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    model = get_model(cfg.model)
    if args.foreground:
        litekit.start_server_fg(cfg, model)
        return 0  # недостижимо: execvp
    litekit.start_server_bg(cfg, model)
    dim("Останов: sudo hackalice cleanup (или kill $(cat "
        f"{lite_kit_path(cfg)}/run/server.pid))")
    return 0


def cmd_wait_device(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    model = get_model(cfg.model)
    info("Ищу колонку с root-ADB...")
    ip = device.wait_for_root(
        cfg, model,
        log_path=str(litekit.server_log_path(cfg)),
        on_status=dim,
        on_log=lambda line: dim("srv| " + line))
    save_config(cfg)
    ok(f"Колонка: {ip} — можно выполнять install-ssh")
    return 0


def cmd_build_dropbear(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    model = get_model(cfg.model)
    path = dropbear.build(cfg, model, force=args.force)
    ok(f"Бинарник: {path}")
    return 0


def cmd_install_ssh(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    _apply_label(cfg, args)
    model = get_model(cfg.model)
    path = dropbear.resolve_dropbear(cfg, model)
    installer.install_ssh(cfg, model, path)
    info("Дальше: переключите колонку на домашний Wi-Fi и закрепите IP "
         "в Static Leases роутера.")
    return 0


def cmd_verify(args) -> int:
    cfg = _cfg_from(args)
    _apply_label(cfg, args)
    installer.verify_ssh(cfg)
    return 0


def cmd_unbind(args) -> int:
    cfg = _cfg_from(args)
    _apply_label(cfg, args)
    warn("Аппаратный сброс при отвязке НЕ использовать — он сотрёт SSH "
         "и OTA-блок")
    installer.unbind_account(cfg)
    return 0


def cmd_cleanup(args) -> int:
    require_root()
    cfg = _cfg_from(args)
    litekit.stop_server(cfg)
    warn("Если колонка ещё НЕ переведена в домашнюю сеть — после выключения "
         "hotspot она останется без сети (SSH будет недоступен, пока хотспот "
         "не поднять снова: sudo hackalice net-up)")
    netenv.hotspot_down(cfg, flush_nat=args.flush_nat)
    ok("Осталось: переключить колонку на домашний Wi-Fi через приложение")
    return 0


def cmd_wizard(_args) -> int:
    return wizard.run_wizard()


def _apply_label(cfg, args) -> None:
    """--label <метка>: подставить ключ/IP/MAC конкретной колонки из конфига."""
    label = getattr(args, "label", None)
    if not label:
        return
    rec = (cfg.speakers or {}).get(label)
    if not isinstance(rec, dict):
        raise StepError("метка '{}' не найдена в конфиге "
                        "(список: hackalice keys list)".format(label))
    if rec.get("key"):
        cfg.ssh_key = rec["key"]
    if rec.get("ip"):
        cfg.device_ip = rec["ip"]
    if rec.get("mac"):
        cfg.device_mac = rec["mac"]


def cmd_find(args) -> int:
    """Инвентаризация: SSH-устройства в локальных сетях + наши колонки."""
    cfg = _cfg_from(args)
    info("Сканирую локальные подсети на порт 22...")
    entries = device.network_inventory(cfg)
    table = Table(title="Устройства с открытым SSH в локальных сетях")
    for col in ("IP", "MAC", "метка", "ключ", "статус"):
        table.add_column(col)
    found_ips = set()
    for e in entries:
        status = "НАША КОЛОНКА (root+SSH)" if e["key"] else \
            "SSH есть, ключ не наш (не прошита / другое устройство)"
        table.add_row(e["ip"], e["mac"] or "-", e["label"] or "-",
                      e["who"] or "-", status)
        found_ips.add(e["ip"])
    console.print(table)

    if not entries:
        dim("SSH-устройств не найдено. Нешитые колонки по SSH не видны — "
            "смотрите список клиентов роутера по MAC.")

    # известные колонки, которых не видно в сети
    for label, rec in sorted((cfg.speakers or {}).items()):
        if not isinstance(rec, dict):
            continue
        if rec.get("ip") in found_ips:
            ok("«{}»: {} (ключ {})".format(label, rec.get("ip"),
                                           rec.get("key")))
        else:
            warn("«{}»: не найдена в сети (последний IP {})".format(
                label, rec.get("ip") or "-"))

    if any(e["key"] for e in entries):
        info("Добавить ключ на найденную прошитую колонку: "
             "hackalice add-key --key-name <имя> --label <метка> "
             "--device-ip <IP>")
    return 0


def cmd_keys(args) -> int:
    cfg = _cfg_from(args)
    if args.keys_action == "list":
        info(f"Хранилище ключей: {keys.keys_dir(cfg)}")
        info("Активный ключ: " + (cfg.ssh_key or "(не задан)"))
        rows = keys.list_keys(cfg)
        if not rows:
            warn("Ключей пока нет — создайте: hackalice keys create --name main")
            return 0
        table = Table(title="Ключи")
        for col in ("имя", "fingerprint", ".pub", "путь"):
            table.add_column(col)
        for k in rows:
            table.add_row(k["name"], k["fingerprint"],
                          "да" if k["has_pub"] else "НЕТ", k["path"])
        console.print(table)
        if cfg.speakers:
            table2 = Table(title="Колонки (метка -> ключ/IP)")
            table2.add_column("метка")
            table2.add_column("ключ")
            table2.add_column("IP")
            for label, rec in sorted(cfg.speakers.items()):
                table2.add_row(label, str(rec.get("key", "-")),
                               str(rec.get("ip", "-")))
            console.print(table2)
        return 0
    if args.keys_action == "create":
        keys.create_key(cfg, args.name, force=args.force)
        return 0
    if args.keys_action == "use":
        keys.use_key(cfg, args.name)
        return 0
    raise StepError("неизвестное действие keys")


def cmd_add_key(args) -> int:
    cfg = _cfg_from(args)
    _apply_label(cfg, args)
    model = get_model(cfg.model)
    if args.key_name:
        pub = Path(str(keys.key_path_for(cfg, args.key_name)) + ".pub")
    elif args.key:
        pub = Path(expand(args.key))
    else:
        raise StepError("укажите новый ключ: --key-name <имя из хранилища> "
                        "или --key <путь к .pub>")
    installer.add_key(cfg, model, pub, label=args.label)
    info("Проверка/подключение: ssh -i <приватный ключ> root@{}".format(
        cfg.device_ip))
    return 0


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", help="ключ модели (см. hackalice models)")
    p.add_argument("--ap-iface", help="Wi-Fi интерфейс под точку доступа")
    p.add_argument("--inet-iface", help="интерфейс с интернетом")
    p.add_argument("--ssid", help="SSID точки доступа")
    p.add_argument("--ap-password", help="пароль точки доступа")
    p.add_argument("--lite-kit-dir", help="каталог lite-kit")
    p.add_argument("--lite-kit-repo", help="git-репозиторий lite-kit")
    p.add_argument("--firmware", help="версия прошивки для отката")
    p.add_argument("--ssh-key", help="приватный ключ для доступа к колонке")
    p.add_argument("--dropbear-bin", help="готовый статический dropbearmulti")
    p.add_argument("--build-dir", help="каталог сборки dropbear")
    p.add_argument("--device-ip", help="IP колонки")
    p.add_argument("--timeout", type=int,
                   help="таймаут ожидания колонки, сек")
    p.add_argument("--keys-dir",
                   help="каталог хранилища ключей (по умолчанию "
                        "~/.config/hackalice/keys)")
    p.add_argument("--device-mac",
                   help="MAC колонки — статический lease на хотспоте")
    p.add_argument("--hotspot-static-ip",
                   help="постоянный IP колонки на хотспоте "
                        "(по умолчанию 10.42.0.200)")
    p.add_argument("--fw-source", choices=("auto", "torrent", "mirror"),
                   help="источник прошивки отката: auto (торрент, если есть "
                        ".torrent, иначе зеркало) | torrent | mirror")
    p.add_argument("--torrent-proxy", metavar="HOST:PORT",
                   help="SOCKS5-прокси для P2P-загрузки прошивки")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hackalice",
        description="Root + постоянный SSH для Яндекс Станций "
                    "(инструмент для СВОИХ устройств).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Без аргументов запускается интерактивный мастер.\n"
               "Пошагово: check -> net-up -> fw -> build-dropbear -> serve\n"
               "          -> wait-device -> install-ssh -> cleanup")
    _add_common(p)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("wizard", help="интерактивный мастер (по умолчанию)")
    sp.set_defaults(func=cmd_wizard)

    sp = sub.add_parser("models", help="список поддерживаемых моделей")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("interfaces", help="список сетевых интерфейсов")
    sp.set_defaults(func=cmd_interfaces)

    sp = sub.add_parser("check", help="проверить окружение")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("net-up", help="поднять hotspot + DNS + NAT")
    sp.set_defaults(func=cmd_net_up)

    sp = sub.add_parser("net-down", help="выключить hotspot и убрать DNS")
    sp.add_argument("--flush-nat", action="store_true",
                    help="удалить и NAT-правила")
    sp.set_defaults(func=cmd_net_down)

    sp = sub.add_parser("fw", help="клон lite-kit, патчи, загрузка прошивки "
                                   "(--fw-source auto|torrent|mirror)")
    sp.add_argument("--fw-source", choices=("auto", "torrent", "mirror"),
                    default=argparse.SUPPRESS,
                    help="источник прошивки отката")
    sp.add_argument("--torrent-proxy", metavar="HOST:PORT",
                    default=argparse.SUPPRESS,
                    help="SOCKS5-прокси для P2P-загрузки")
    sp.add_argument("--firmware-url", metavar="URL",
                    help="скачать update.zip с произвольного зеркала/CDN")
    sp.add_argument("--firmware-file", metavar="PATH",
                    help="подложить уже скачанный update.zip в firmware/")
    sp.set_defaults(func=cmd_fw)

    sp = sub.add_parser("serve", help="запустить сервер отката (в фоне)")
    sp.add_argument("--foreground", action="store_true",
                    help="на переднем плане (Ctrl+C — стоп)")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("wait-device", help="ждать колонку с root-ADB")
    sp.set_defaults(func=cmd_wait_device)

    sp = sub.add_parser("build-dropbear", help="собрать статический dropbear")
    sp.add_argument("--force", action="store_true",
                    help="пересобрать даже если бинарник есть")
    sp.set_defaults(func=cmd_build_dropbear)

    sp = sub.add_parser("install-ssh",
                        help="установить SSH на колонку, reboot, проверка")
    sp.add_argument("--label", help="метка колонки (ключ/IP из конфига)")
    sp.set_defaults(func=cmd_install_ssh)

    sp = sub.add_parser("verify", help="проверить SSH и OTA-блок")
    sp.add_argument("--label", help="метка колонки (ключ/IP из конфига)")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("unbind",
                        help="отвязать от аккаунта по SSH (root остаётся)")
    sp.add_argument("--label", help="метка колонки (ключ/IP из конфига)")
    sp.set_defaults(func=cmd_unbind)

    sp = sub.add_parser("cleanup",
                        help="стоп сервера отката + выключить сеть")
    sp.add_argument("--flush-nat", action="store_true")
    sp.set_defaults(func=cmd_cleanup)

    sp = sub.add_parser("find",
                        help="инвентаризация сети: SSH-устройства и наши колонки")
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("keys",
                        help="хранилище SSH-ключей: list / create / use")
    ksub = sp.add_subparsers(dest="keys_action", required=True)
    ksub.add_parser("list", help="список ключей и колонок")
    spk = ksub.add_parser("create", help="сгенерировать новую пару ключей")
    spk.add_argument("--name", required=True, help="имя ключа (латиница)")
    spk.add_argument("--force", action="store_true",
                     help="перезаписать существующий")
    spk = ksub.add_parser("use", help="сделать ключ активным")
    spk.add_argument("--name", required=True)
    sp.set_defaults(func=cmd_keys)

    sp = sub.add_parser("add-key",
                        help="добавить ключ на уже прошитую колонку "
                             "(без переустановки)")
    sp.add_argument("--key-name", help="имя ключа из хранилища (его .pub)")
    sp.add_argument("--key", help="путь к публичному ключу (.pub)")
    sp.add_argument("--label", help="метка колонки (записывается в конфиг)")
    sp.set_defaults(func=cmd_add_key)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    cmd = getattr(args, "cmd", None)
    if not cmd:
        try:
            return wizard.run_wizard()
        except StepError as e:
            err(str(e))
            panel(TROUBLESHOOTING, title="Помощь", border="yellow")
            return 1
        except wizard._Abort as e:
            err(str(e) or "Отменено пользователем")
            return 1
        except KeyboardInterrupt:
            err("Прервано пользователем")
            return 130

    # Гейт зависимостей: на каждом запуске проверяем, что нужные команде
    # утилиты есть; недостающее автоматически доустанавливается через apt.
    need = deps.deps_for_command(cmd)
    if need:
        try:
            deps.ensure(need)
        except StepError as e:
            err(str(e))
            return 1

    try:
        return args.func(args)
    except StepError as e:
        err(str(e))
        panel(TROUBLESHOOTING, title="Помощь", border="yellow")
        return 1
    except wizard._Abort as e:
        err(str(e) or "Отменено пользователем")
        return 1
    except KeyboardInterrupt:
        err("Прервано пользователем")
        return 130
