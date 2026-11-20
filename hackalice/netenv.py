"""Сетевое окружение: список интерфейсов, точка доступа, DNS, NAT."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .console import info, ok, warn
from .util import StepError, is_linux, run, which

AP_CON_NAME = "AliceHotspot"
AP_SUBNET = "10.42.0."

DNS_CONF_PATH = Path("/etc/NetworkManager/dnsmasq-shared.d/alice-dns.conf")
DNS_CONF_BODY = """\
# hackalice: локальный DNS для рутирования Станции
dhcp-option=6,10.42.0.1
server=8.8.8.8
address=/quasar.yandex.net/10.42.0.1
address=/quasar.yandex.ru/10.42.0.1
address=/scbh.yandex.net/10.42.0.1
address=/iot.quasar.yandex.net/10.42.0.1
"""

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")


def normalize_mac(mac: str) -> str:
    """Привести MAC к виду aa:bb:cc:dd:ee:ff или бросить StepError."""
    mac = (mac or "").strip().lower().replace("-", ":")
    if not _MAC_RE.match(mac):
        raise StepError(
            "MAC-адрес в неверном формате: '{}' (пример: aa:bb:cc:dd:ee:01)"
            .format(mac))
    return mac


def hotspot_leases(cfg) -> list[tuple[str, str]]:
    """Статические lease хотспота: [(mac, ip), ...].

    Основная колонка: cfg.device_mac -> cfg.hotspot_static_ip (10.42.0.200).
    Дополнительные (из config.speakers, если там есть MAC) получают
    10.42.0.201, 202, ... — можно держать на хотспоте несколько колонок.
    """
    leases: list[tuple[str, str]] = []
    used: set[str] = set()
    mac = normalize_mac(cfg.device_mac or "") if cfg.device_mac else ""
    ip = (cfg.hotspot_static_ip or "").strip()
    if mac and ip:
        leases.append((mac, ip))
        used.add(ip)
    for _label, rec in sorted((cfg.speakers or {}).items()):
        if not isinstance(rec, dict):
            continue
        try:
            m = normalize_mac(rec.get("mac") or "") if rec.get("mac") else ""
        except StepError:
            continue
        if not m or m in [l[0] for l in leases]:
            continue
        for n in range(201, 255):
            cand = "{}{}".format(AP_SUBNET, n)
            if cand not in used:
                leases.append((m, cand))
                used.add(cand)
                break
    return leases


# ---------------------------------------------------------------------------
# Перечисление интерфейсов
# ---------------------------------------------------------------------------

@dataclass
class IfaceInfo:
    name: str
    kind: str = "other"          # wifi | ethernet | other
    state: str = ""
    conn: str = ""
    ipv4: str = ""
    phy: str = ""
    ap_support: bool | None = None   # None — неизвестно (нет iw)

    @property
    def is_wifi(self) -> bool:
        return self.kind == "wifi"


def _sysfs_interfaces() -> dict[str, IfaceInfo]:
    out: dict[str, IfaceInfo] = {}
    base = Path("/sys/class/net")
    if not base.exists():
        return out
    for dev in base.iterdir():
        name = dev.name
        kind = "wifi" if ((dev / "wireless").exists()
                          or (dev / "phy80211").exists()) else "other"
        phy_path = dev / "phy80211" / "name"
        phy = phy_path.read_text().strip() if phy_path.exists() else ""
        out[name] = IfaceInfo(name=name, kind=kind, phy=phy)
    return out


def _nmcli_details(ifaces: dict[str, IfaceInfo]) -> None:
    try:
        res = run("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status",
                  check=False, capture=True, quiet=True)
    except StepError:
        return  # nmcli отсутствует — остаётся sysfs-информация
    type_map = {"wifi": "wifi", "ethernet": "ethernet"}
    for line in (res.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 4:
            name, ntype, state, conn = parts[0], parts[1], parts[2], parts[3]
            entry = ifaces.setdefault(name, IfaceInfo(name=name))
            if ntype in type_map:
                entry.kind = type_map[ntype]
            entry.state = state
            entry.conn = conn
    # IPv4-адреса
    try:
        res = run("ip -o -4 addr show", check=False, capture=True, quiet=True)
    except StepError:
        return
    for m in re.finditer(r"^\d+: (\S+)\s+inet (\S+)", res.stdout or "", re.M):
        if m.group(1) in ifaces:
            ifaces[m.group(1)].ipv4 = m.group(2)


def _check_ap_support(iface: IfaceInfo) -> None:
    """Поддержка режима AP по `iw phy <phy> info`."""
    if iface.kind != "wifi" or not iface.phy or not which("iw"):
        iface.ap_support = None if iface.kind == "wifi" else False
        return
    res = run(["iw", "phy", iface.phy, "info"],
              check=False, capture=True, quiet=True)
    out = res.stdout or ""
    m = re.search(r"Supported interface modes:\n((?:\s+\* .*\n)+)", out)
    iface.ap_support = bool(m and re.search(r"^\s+\* AP\b", m.group(1), re.M))


def list_interfaces(with_ap_check: bool = False) -> list[IfaceInfo]:
    """Список сетевых интерфейсов (Linux; на других ОС — пусто)."""
    if not is_linux():
        return []
    ifaces = _sysfs_interfaces()
    _nmcli_details(ifaces)
    result = sorted(ifaces.values(), key=lambda i: (not i.is_wifi, i.name))
    if with_ap_check:
        for i in result:
            _check_ap_support(i)
    return result


def find_wifi_interfaces(ifaces: list[IfaceInfo] | None = None) -> list[IfaceInfo]:
    ifaces = ifaces if ifaces is not None else list_interfaces()
    return [i for i in ifaces if i.is_wifi]


# ---------------------------------------------------------------------------
# Точка доступа + DNS + NAT
# ---------------------------------------------------------------------------

def hotspot_up(cfg) -> None:
    """Поднять hotspot, локальный DNS с перехватом доменов Яндекса и NAT."""
    if not cfg.ap_iface or not cfg.inet_iface:
        raise StepError("не выбраны интерфейсы (ap_iface/inet_iface)")

    info(f"Поднимаю точку доступа '{cfg.ssid}' на {cfg.ap_iface}...")
    res = run(["nmcli", "dev", "wifi", "hotspot", "ifname", cfg.ap_iface,
               "con-name", AP_CON_NAME, "ssid", cfg.ssid,
               "password", cfg.ap_password],
              check=False, capture=True)
    if res.returncode != 0:
        tail = ((res.stderr or "") + (res.stdout or "")).strip()[:500]
        raise StepError(
            f"nmcli не смог поднять hotspot на {cfg.ap_iface}.\n{tail}\n"
            f"Проверьте, что интерфейс Wi-Fi и поддерживает режим AP "
            f"(iw phy <phy> info -> '* AP').")

    DNS_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    conf = DNS_CONF_BODY
    leases = hotspot_leases(cfg)
    if leases:
        conf += "\n# статические lease (hackalice): MAC колонок -> постоянные IP\n"
        for mac, ip in leases:
            conf += "dhcp-host={},hackalice,{}\n".format(mac, ip)
        ok("Статический IP на хотспоте: " + ", ".join(
            "{} -> {}".format(m, i) for m, i in leases))
    DNS_CONF_PATH.write_text(conf)
    ok(f"Записан {DNS_CONF_PATH}")

    info("Перезапускаю hotspot для применения DNS-конфига...")
    run(["nmcli", "connection", "down", AP_CON_NAME], check=False, quiet=True)
    run(["nmcli", "connection", "up", AP_CON_NAME])

    _setup_nat(cfg)

    res = run(["ping", "-I", cfg.inet_iface, "-c", "2", "-W", "3", "8.8.8.8"],
              check=False, capture=True, quiet=True)
    if res.returncode == 0:
        ok(f"Интернет через {cfg.inet_iface} доступен")
    else:
        warn(f"Нет пинга через {cfg.inet_iface} — колонка не сможет скачать "
             f"прошивку/NTP. Проверьте, что ноутбук сам в интернете.")
    ok(f"Сеть готова. SSID: {cfg.ssid}  пароль: {cfg.ap_password}")


def _setup_nat(cfg) -> None:
    run("sysctl -w net.ipv4.ip_forward=1")
    run(f"iptables -t nat -C POSTROUTING -o {cfg.inet_iface} -j MASQUERADE "
        f"2>/dev/null || iptables -t nat -A POSTROUTING -o {cfg.inet_iface} "
        f"-j MASQUERADE")

    # Перехват ВЕСЬ DNS-трафика клиентов точки доступа (udp/tcp 53) на наш
    # dnsmasq: у Станций свой резолвер unbound на 127.0.0.1:53, который
    # пересылает запросы не только DHCP-DNS, но и напрямую к DNS Яндекса
    # (77.88.8.x). Без DNAT sync может уйти в реальное облако мимо сервера
    # активации (wiki hackalice.me -> internals/network).
    for proto in ("udp", "tcp"):
        run(f"iptables -t nat -C PREROUTING -i {cfg.ap_iface} -p {proto} "
            f"--dport 53 -j DNAT --to-destination 10.42.0.1 2>/dev/null || "
            f"iptables -t nat -A PREROUTING -i {cfg.ap_iface} -p {proto} "
            f"--dport 53 -j DNAT --to-destination 10.42.0.1")
    ok("DNS-трафик клиентов AP перехвачен полностью (DNAT udp/tcp 53)")

    if run("iptables -nL DOCKER-USER", check=False, capture=True,
           quiet=True).returncode == 0:
        run(f"iptables -C DOCKER-USER -i {cfg.ap_iface} -o {cfg.inet_iface} "
            f"-j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 "
            f"-i {cfg.ap_iface} -o {cfg.inet_iface} -j ACCEPT")
        run(f"iptables -C DOCKER-USER -i {cfg.inet_iface} -o {cfg.ap_iface} "
            f"-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null "
            f"|| iptables -I DOCKER-USER 1 -i {cfg.inet_iface} "
            f"-o {cfg.ap_iface} -m conntrack --ctstate RELATED,ESTABLISHED "
            f"-j ACCEPT")
        ok("Правила DOCKER-USER добавлены (Docker найден)")
    else:
        info("DOCKER-USER отсутствует (Docker не установлен) — пропускаю")


def hotspot_down(cfg, flush_nat: bool = False) -> None:
    """Выключить hotspot и убрать DNS-конфиг (NAT — по желанию)."""
    if DNS_CONF_PATH.exists():
        DNS_CONF_PATH.unlink()
        ok(f"Удалён {DNS_CONF_PATH}")
    # DNAT-перехват 53 порта — наш, снимаем всегда
    for proto in ("udp", "tcp"):
        run(f"iptables -t nat -D PREROUTING -i {cfg.ap_iface} -p {proto} "
            f"--dport 53 -j DNAT --to-destination 10.42.0.1 2>/dev/null",
            check=False)
    run(["nmcli", "connection", "down", AP_CON_NAME], check=False, quiet=True)
    ok(f"Hotspot {cfg.ap_iface} выключен (коннекшн '{AP_CON_NAME}' сохранён)")
    if flush_nat:
        run(f"iptables -t nat -D POSTROUTING -o {cfg.inet_iface} -j MASQUERADE "
            f"2>/dev/null", check=False)
        run(f"iptables -D DOCKER-USER -i {cfg.ap_iface} -o {cfg.inet_iface} "
            f"-j ACCEPT 2>/dev/null", check=False)
        run(f"iptables -D DOCKER-USER -i {cfg.inet_iface} -o {cfg.ap_iface} "
            f"-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null",
            check=False)
        ok("NAT-правила удалены")
