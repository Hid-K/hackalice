"""Работа с устройством: ADB-обёртка, поиск колонки в сети, ожидание root."""
from __future__ import annotations

import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from .console import info, ok, warn
from .models import ModelSpec
from .netenv import AP_SUBNET, normalize_mac
from .util import StepError, expand, run


# ---------------------------------------------------------------------------
# ADB
# ---------------------------------------------------------------------------

def serial(ip: str, port: int = 5555) -> str:
    return f"{ip}:{port}"


def start_server() -> None:
    run(["adb", "start-server"], check=False, capture=True, quiet=True)


def connect(ip: str, port: int = 5555) -> bool:
    res = run(["adb", "connect", serial(ip, port)],
              check=False, capture=True, quiet=True)
    out = ((res.stdout or "") + (res.stderr or "")).lower()
    return "connected" in out and "cannot connect" not in out


def shell(sr: str, cmd: str, timeout: int = 30) -> str:
    res = run(["adb", "-s", sr, "shell", cmd],
              check=False, capture=True, timeout=timeout)
    return res.stdout or ""


def push(sr: str, local, remote: str) -> None:
    run(["adb", "-s", sr, "push", str(local), remote])


def has_root(sr: str) -> tuple[bool, str]:
    out = shell(sr, "id").strip()
    return ("uid=0" in out), out


def reboot(sr: str) -> None:
    run(["adb", "-s", sr, "reboot"], check=False, capture=True, quiet=True)


def try_adb_root(ip: str, port: int = 5555) -> tuple[bool, str]:
    sr = serial(ip, port)
    connect(ip, port)
    time.sleep(0.5)
    return has_root(sr)


# ---------------------------------------------------------------------------
# Поиск кандидатов в сети точки доступа
# ---------------------------------------------------------------------------

def _from_ip_neigh(ap_iface: str) -> set[str]:
    res = run(["ip", "neigh", "show", "dev", ap_iface],
              check=False, capture=True, quiet=True)
    return set(re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+dev\s", res.stdout or ""))


def _from_leases() -> set[str]:
    ips: set[str] = set()
    patterns = [
        "/var/lib/misc/dnsmasq.leases",
        "/var/lib/NetworkManager/dnsmasq*.leases",
        "/run/NetworkManager/dnsmasq*.leases",
    ]
    import glob as _glob
    for pat in patterns:
        for path in _glob.glob(pat):
            try:
                with open(path, "r", errors="replace") as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) >= 3 and parts[2].startswith(AP_SUBNET):
                            ips.add(parts[2])
            except OSError:
                pass
    return ips


def ping_sweep(cidr: str) -> list[str]:
    """ICMP-скан подсети (заодно заполняет ARP/neigh). Отвечавшие IP."""
    import ipaddress
    try:
        net = ipaddress.ip_interface(cidr).network
    except ValueError:
        return []
    if net.prefixlen < 22:
        warn(f"Сеть {net} слишком большая для скана — пропускаю")
        return []
    hosts = [str(h) for h in net.hosts()]

    def _ping(ip: str):
        r = __import__("subprocess").run(["ping", "-c", "1", "-W", "1", ip],
                                         capture_output=True)
        return ip if r.returncode == 0 else None

    with ThreadPoolExecutor(max_workers=96) as ex:
        return [ip for ip in ex.map(_ping, hosts) if ip]


def _ping_sweep() -> set[str]:
    return set(ping_sweep("{}0/24".format(AP_SUBNET)))


def find_ip_by_mac(mac: str, ifaces: list[str] | None = None,
                   sweep: bool = True) -> list[str]:
    """Найти текущие IP устройства по MAC в локальных сетях.

    1) то, что уже известно (ip neigh + lease-файлы dnsmasq);
    2) если пусто и разрешён sweep — ICMP-скан подсетей всех интерфейсов
       (заполняет ARP-таблицу) и повторный просмотр neigh.
    Работает и в домашней сети — роутер не нужен.
    """
    ips: set[str] = set()
    try:
        ips.update(ips_for_mac(mac))
    except StepError:
        pass
    if ips or not sweep:
        return sorted(ips)

    for iface, cidr in local_subnets():
        if ifaces and iface not in ifaces:
            continue
        info(f"Ищу MAC {mac} сканом {cidr} ({iface})...")
        ping_sweep(cidr)
        try:
            ips.update(ips_for_mac(mac, iface))
        except StepError:
            pass
        if ips:
            break
    return sorted(ips)


def find_candidates(ap_iface: str) -> list[str]:
    ips = _from_ip_neigh(ap_iface) | _from_leases()
    if not ips:
        ips = _ping_sweep()
    return sorted(ips)


# ---------------------------------------------------------------------------
# Ожидание колонки с root-ADB
# ---------------------------------------------------------------------------

def wait_for_root(cfg, model: ModelSpec, *, log_path: str | None = None,
                  on_status=None, on_log=None) -> str:
    """Ждать колонку с root-ADB в сети AP; вернуть IP.

    on_status(str) / on_log(str) — колбэки для вывода (использует визард).
    """
    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    start_server()
    port = model.adb_port
    deadline = time.time() + cfg.timeout
    candidate: str | None = None
    confirmations = 0
    need = 2  # устройство должно стабильно перезагрузиться в прошивку с adbd
    log_offset = 0

    status(f"ищу колонку в сети {cfg.ap_iface} (таймаут {cfg.timeout} с)")
    while time.time() < deadline:
        if log_path:
            try:
                with open(log_path, "r", errors="replace") as fh:
                    fh.seek(log_offset)
                    new = fh.read()
                    log_offset = fh.tell()
                if on_log:
                    for line in new.splitlines():
                        if line.strip():
                            on_log(line)
            except OSError:
                pass

        if candidate is not None:
            rooted, out = try_adb_root(candidate, port)
            if rooted:
                confirmations += 1
                if confirmations >= need:
                    ok(f"Колонка {candidate} стабильна, ADB root подтверждён")
                    cfg.device_ip = candidate
                    # запоминаем MAC — для статического lease на хотспоте
                    cfg.device_mac = mac_for_ip(candidate) or cfg.device_mac
                    return candidate
                status(f"ADB root есть, подтверждаю стабильность "
                       f"({confirmations}/{need})")
            else:
                warn("Устройство пропало/перезагружается — продолжаю ждать")
                candidate = None
                confirmations = 0
            time.sleep(10)
            continue

        for ip in find_candidates(cfg.ap_iface):
            rooted, _ = try_adb_root(ip, port)
            if rooted:
                candidate = ip
                status(f"найден кандидат {ip} (uid=0), жду стабилизации")
                break
        time.sleep(5)

    raise StepError(
        f"Колонка с root-ADB не найдена за {cfg.timeout} с. Проверьте: "
        f"выполнен ли аппаратный сброс, начата ли привязка в приложении, "
        f"работает ли NAT (net-up) и что показывает лог сервера lite-kit.")


def mac_for_ip(ip: str) -> str | None:
    """MAC-адрес соседа по IP из таблицы neigh (до перезагрузки колонки)."""
    res = run(["ip", "neigh", "show"], check=False, capture=True, quiet=True)
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == ip:
            for p in parts[1:]:
                if re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", p):
                    return p.lower()
    return None


def ips_for_mac(mac: str, ap_iface: str | None = None) -> list[str]:
    """Все известные IP для MAC: таблица neigh + lease-файлы dnsmasq."""
    mac = mac.lower().replace("-", ":")
    ips: set[str] = set()
    cmd = ["ip", "neigh", "show"]
    if ap_iface:
        cmd += ["dev", ap_iface]
    res = run(cmd, check=False, capture=True, quiet=True)
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].count(".") == 3:
            for p in parts[1:]:
                if p.lower() == mac:
                    ips.add(parts[0])
                    break
    import glob as _glob
    patterns = [
        "/var/lib/misc/dnsmasq.leases",
        "/var/lib/NetworkManager/dnsmasq*.leases",
        "/run/NetworkManager/dnsmasq*.leases",
    ]
    for pat in patterns:
        for path in _glob.glob(pat):
            try:
                with open(path, "r", errors="replace") as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) >= 3 and parts[1].lower() == mac:
                            ips.add(parts[2])
            except OSError:
                pass
    return sorted(ips)


# ---------------------------------------------------------------------------
# Поиск колонки в ЛЮБОЙ доступной сети (например, после переезда в домашнюю)
# ---------------------------------------------------------------------------

def local_subnets() -> list[tuple[str, str]]:
    """IPv4-подсети локальных интерфейсов: [(iface, 'a.b.c.d/nn'), ...]."""
    res = run("ip -o -4 addr show", check=False, capture=True, quiet=True)
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"^\d+: (\S+)\s+inet (\S+)", res.stdout or "", re.M):
        iface, cidr = m.group(1), m.group(2)
        if cidr.startswith("127."):
            continue
        out.append((iface, cidr))
    return out


def scan_ssh_hosts(cidrs: list[str], port: int = 22,
                   timeout: float = 1.0) -> list[str]:
    """Параллельный TCP-скан порта 22 по хостам указанных подсетей."""
    targets: set[str] = set()
    for cidr in cidrs:
        try:
            net = ipaddress.ip_interface(cidr).network
        except ValueError:
            continue
        if net.prefixlen < 22:
            warn(f"Сеть {net} слишком большая для скана — пропускаю")
            continue
        targets.update(str(h) for h in net.hosts())
    if not targets:
        return []

    def _check(ip: str):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=96) as ex:
        return [ip for ip in ex.map(_check, sorted(targets)) if ip]


def _speaker_keys(cfg, active_first: str | None = None) -> list[tuple[str, str]]:
    """Все известные ключи: [(путь, «кому»)], приоритетный — первым."""
    keys_try: list[tuple[str, str]] = []
    if active_first:
        keys_try.append((expand(active_first), "активный"))
    elif cfg.ssh_key:
        keys_try.append((expand(cfg.ssh_key), "активный"))
    for label, rec in sorted((cfg.speakers or {}).items()):
        k = rec.get("key") if isinstance(rec, dict) else None
        if k:
            k = expand(k)
            if k not in [p for p, _ in keys_try]:
                keys_try.append((k, label))
    return keys_try


def network_inventory(cfg) -> list[dict]:
    """Скан локальных подсетей: устройства с открытым SSH.

    Помечает «наши» колонки: ssh_ok по любому из наших ключей + MAC/метка
    из config.speakers. Нешитые колонки SSH не имеют — в списке их нет.
    """
    keys_try = _speaker_keys(cfg)
    found = scan_ssh_hosts([c for _, c in local_subnets()])
    out: list[dict] = []
    for ip in found:
        entry = {
            "ip": ip,
            "mac": mac_for_ip(ip) or "",
            "banner": ssh_banner(ip) or "",
            "key": None,
            "who": None,
            "label": None,
        }
        for kpath, who in keys_try:
            if ssh_ok(ip, kpath):
                entry["key"] = kpath
                entry["who"] = who
                break
        if entry["mac"]:
            for label, rec in (cfg.speakers or {}).items():
                if not isinstance(rec, dict):
                    continue
                rec_mac = normalize_mac(rec.get("mac") or "") \
                    if rec.get("mac") else ""
                if rec_mac and rec_mac == entry["mac"]:
                    entry["label"] = label
        out.append(entry)
    return out


def find_speaker_by_ssh(cfg, model, mac_hint: str | None = None,
                        key_hint: str | None = None) -> str:
    """Найти колонку с НАШИМ ключом в любой доступной сети; вернуть IP.

    Пробует все ключи из конфига (активный + ключи колонок из config.speakers),
    порядок: известный IP -> соседи/leases по MAC -> ping-скан -> скан
    подсетей на порт 22. Работает и на hotspot, и в домашней сети.
    """
    key_path = key_hint or expand(cfg.ssh_key)
    keys_try = _speaker_keys(cfg, active_first=key_path)
    mac = mac_hint or cfg.device_mac or None
    candidates: list[str] = []
    if cfg.device_ip:
        candidates.append(cfg.device_ip)
    for iface in dict.fromkeys([cfg.ap_iface, cfg.inet_iface]):
        if iface and mac:
            candidates += ips_for_mac(mac, iface)
    for cand in dict.fromkeys(candidates):
        for kpath, _who in keys_try:
            if ssh_ok(cand, kpath):
                return cand

    # MAC-поиск с ping-сканом (заполняет ARP даже в домашней сети)
    if mac:
        for cand in find_ip_by_mac(mac):
            for kpath, _who in keys_try:
                if ssh_ok(cand, kpath):
                    return cand

    info("Колонки с нашими ключами на известных адресах нет — "
         "сканирую подсети на порт 22...")
    found = scan_ssh_hosts([cidr for _, cidr in local_subnets()])
    for cand in found:
        for kpath, _who in keys_try:
            if ssh_ok(cand, kpath):
                return cand
    raise StepError(
        "Колонка с нашими SSH-ключами не найдена ни в одной доступной сети. "
        "Проверьте: колонка в сети (отвечает), ноутбук в той же сети, "
        "либо укажите IP вручную (--device-ip).")


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

def ssh_banner(ip: str, port: int = 22, timeout: int = 5) -> str | None:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).decode("utf-8", "replace").strip()
    except OSError:
        return None


def ssh_ok(ip: str, key_path: str) -> bool:
    cmd = ["ssh", "-i", key_path,
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=5",
           f"root@{ip}", "true"]
    return run(cmd, check=False, capture=True, quiet=True).returncode == 0
