"""Установка root-SSH на колонку: dropbear в /data, автозапуск, блок OTA."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .console import dim, info, ok, warn
from .config import save_config
from .device import (find_candidates, find_speaker_by_ssh, ips_for_mac,
                     mac_for_ip, push, reboot, serial, shell, ssh_banner,
                     ssh_ok, start_server, try_adb_root)
from .models import ModelSpec, get_model
from .util import StepError, expand, run

MARKER = "ALICE_ROOT_MARKER"

OTA_HOSTS_ENTRIES = [
    "update.quasar.yandex.net",
    "ota.quasar.yandex.net",
    "static-ota.quasar.yandex.net",
    "update-yandex.s3.yandex.net",
]

START_SSH_SH = """#!/bin/sh
# %(marker)s: автозапуск dropbear (/data/start_ssh.sh)
# Всё пишем в лог на /data — переживёт перезагрузку и покажет, что было при буте
LOG=/data/ssh_boot.log
{
echo "=== $(date) старт (args: $*) ==="
if pidof dropbear 2>/dev/null; then
    echo "dropbear уже работает — выхожу"
    exit 0
fi

# На eMMC-моделях (Миди) корень при загрузке смонтирован ro — открываем на
# запись ради /root/.ssh (ключи, записанные при установке, и так переживают
# перезагрузку; копия здесь — на случай обновлённого authorized_keys)
mount -o remount,rw / 2>/dev/null && echo "корень перезаведён в rw"
mkdir -p /root/.ssh 2>&1
cp /data/authorized_keys /root/.ssh/authorized_keys 2>&1 && echo "ключ на месте"
chmod 700 /root/.ssh 2>/dev/null
chmod 600 /root/.ssh/authorized_keys 2>/dev/null

/data/dropbearmulti dropbear -R -s -g \\
  -r /data/dropbear/dropbear_rsa_host_key \\
  -r /data/dropbear/dropbear_ed25519_host_key -p 22 2>&1
echo "dropbear завершился с кодом $?"
} >> "$LOG" 2>&1
""" % {"marker": MARKER}

S99QUASAR_SH = """#!/bin/sh
# %(marker)s: SSH-автостарт + оригинальный onboot
# Оригинал этого файла сохранён в /data/S99quasar.orig (если существовал)
echo "$(date) S99quasar вызван (args: $*)" >> /data/ssh_boot.log

# rcS на разных моделях вызывает S-скрипты по-разному (с "start" и без) —
# стартуем при любом вызове; повтор безопасен (pidof-предохранитель в скрипте)
if [ -x /data/start_ssh.sh ]; then
    /data/start_ssh.sh
fi

if [ -x /system/vendor/quasar/onboot.sh ]; then
    /system/vendor/quasar/onboot.sh "$@"
fi
""" % {"marker": MARKER}

# Диагностика бут-цепочки: снимается при установке и скачивается на ноутбук,
# чтобы понять, почему dropbear не стартует при загрузке (Миди)
BOOT_DIAG_SH = """#!/bin/sh
{
echo "=== boot_diag $(date) ==="
echo "--- inittab (без комментариев) ---"
grep -v '^#' /etc/inittab 2>/dev/null
echo "--- rcS (первые 60 строк) ---"
head -60 /etc/init.d/rcS 2>/dev/null
echo "--- ls /etc/init.d ---"
ls -la /etc/init.d/ 2>/dev/null
echo "--- S99quasar (наша обёртка) ---"
cat /etc/init.d/S99quasar 2>/dev/null
echo "--- S99quasar.orig (оригинал, первые 40 строк) ---"
head -40 /data/S99quasar.orig 2>/dev/null
echo "--- onboot.sh (полностью) ---"
cat /system/vendor/quasar/onboot.sh 2>/dev/null
echo "--- процессы сейчас ---"
ps 2>/dev/null | head -40
echo "--- смонтировано ---"
cat /proc/mounts 2>/dev/null | grep -E ' / | /data|/system'
echo "--- END ---"
} > /data/boot_diag.txt 2>&1
echo "BOOT_DIAG_DONE"
"""

REMOTE_INSTALL_SH = """#!/bin/sh
set -x

# eMMC-модели (Миди/cucumber и др.): корень смонтирован ro (wiki hackalice.me,
# devices/station-midi) — правки корня только через remount
mount -o remount,rw / 2>&1
mount -o remount,rw /system 2>/dev/null

if touch /etc/.alice_rw_test 2>/dev/null; then
    rm -f /etc/.alice_rw_test
    echo "RW_OK"
else
    echo "INSTALL_BLOCKED_RO"
    exit 1
fi

chmod 755 /data/dropbearmulti
mkdir -p /data/dropbear

[ -f /data/dropbear/dropbear_rsa_host_key ] || \\
  /data/dropbearmulti dropbearkey -t rsa -f /data/dropbear/dropbear_rsa_host_key
[ -f /data/dropbear/dropbear_ed25519_host_key ] || \\
  /data/dropbearmulti dropbearkey -t ed25519 -f /data/dropbear/dropbear_ed25519_host_key

chmod 755 /data/start_ssh.sh

# ключи сразу в /root/.ssh (переживают перезагрузку на ext4)
mkdir -p /root/.ssh
cp /data/authorized_keys /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys

# бэкап оригинального S99quasar один раз
if [ ! -f /data/S99quasar.orig ] && [ -f /etc/init.d/S99quasar ] \\
   && ! grep -q "%(marker)s" /etc/init.d/S99quasar; then
    cp /etc/init.d/S99quasar /data/S99quasar.orig
fi

cat > /etc/init.d/S99quasar <<'EOF_S99'
%(s99)sEOF_S99
chmod 755 /etc/init.d/S99quasar

# Хук №2 (основной на Миди): /system/vendor/quasar/onboot.sh выполняется
# при КАЖДОЙ загрузке (именно он активирует ADB-маркеры) — добавляем запуск
# SSH и туда; на случай, если S99-скрипты в бут-цепочке не задействованы
if [ -f /system/vendor/quasar/onboot.sh ]; then
    if ! grep -q "%(marker)s" /system/vendor/quasar/onboot.sh; then
        cp /system/vendor/quasar/onboot.sh /data/onboot.sh.orig
        printf '\\n# %(marker)s: автозапуск dropbear\\n[ -x /data/start_ssh.sh ] && /data/start_ssh.sh\\n' \\
            >> /system/vendor/quasar/onboot.sh
    fi
else
    echo "WARN_NO_ONBOOT"
fi

# блокировка OTA
if [ -f /system/vendor/quasar/device_ota_update.sh ] && [ ! -f /data/device_ota_update.sh.bak ]; then
    cp /system/vendor/quasar/device_ota_update.sh /data/device_ota_update.sh.bak
fi
printf '#!/bin/sh\\nexit 0\\n' > /system/vendor/quasar/device_ota_update.sh
chmod 755 /system/vendor/quasar/device_ota_update.sh

# домены обновлений -> localhost
for d in %(domains)s; do
    grep -q "127.0.0.1 $d" /etc/hosts || echo "127.0.0.1 $d" >> /etc/hosts
done

touch /data/.alice_root_installed
sync

# верификация: SSH-обёртка обязана встать, иначе после reboot доступа не будет
grep -q "%(marker)s" /etc/init.d/S99quasar || { echo "INSTALL_S99_FAIL"; exit 1; }
if [ -f /system/vendor/quasar/onboot.sh ]; then
    grep -q "%(marker)s" /system/vendor/quasar/onboot.sh || \\
        { echo "INSTALL_ONBOOT_FAIL"; exit 1; }
fi
echo "REMOTE_INSTALL_DONE"
""" % {
    "marker": MARKER,
    "s99": S99QUASAR_SH,
    "domains": " ".join(OTA_HOSTS_ENTRIES),
}

UNBIND_SH = """#!/bin/sh
/etc/init.d/S99quasar stop 2>/dev/null || killall -9 quasar_daemon yandex_io 2>/dev/null || true
rm -rf /data/quasar/* /data/misc/wifi/wpa_supplicant.conf /etc/wpa_supplicant.conf 2>/dev/null || true
sync
reboot
"""


# ---------------------------------------------------------------------------
# Ключи
# ---------------------------------------------------------------------------

def ensure_ssh_key(key_path: str) -> str:
    """Гарантировать наличие пары ключей; вернуть путь к .pub."""
    key_path = expand(key_path)
    pub = key_path + ".pub"
    if not os.path.exists(pub):
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        info(f"Генерирую SSH-ключ {key_path}")
        run(["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", ""])
    ok(f"Публичный ключ: {pub}")
    return pub


# ---------------------------------------------------------------------------
# adb remote script
# ---------------------------------------------------------------------------

def _adb_run_script(sr: str, script_text: str, remote_name: str,
                    expect: str | None = "REMOTE_INSTALL_DONE") -> str:
    """Пушим скрипт в /data и выполняем `sh file` — без хрупких heredoc."""
    local = Path("/tmp") / remote_name
    local.write_text(script_text)
    push(sr, local, "/data/" + remote_name)
    res = run(["adb", "-s", sr, "shell", "sh", "/data/" + remote_name],
              check=False, capture=True)
    out = res.stdout or ""
    print(out.rstrip())
    if (res.stderr or "").strip():
        warn((res.stderr or "").rstrip())
    shell(sr, f"rm -f /data/{remote_name}")
    if "INSTALL_BLOCKED_RO" in out:
        raise StepError(
            "корень колонки остался read-only: `mount -o remount,rw /` не "
            "сработал (на Миди/eMMC он должен работать — пришлите вывод "
            "выше в issue)")
    if "INSTALL_S99_FAIL" in out:
        raise StepError("S99quasar не записался — SSH-автозапуск не "
                        "закреплён; смотрите вывод выше")
    if "INSTALL_ONBOOT_FAIL" in out:
        raise StepError("маркер автозапуска не прописался в onboot.sh — "
                        "смотрите вывод выше")
    if expect and expect not in out:
        raise StepError("установочный скрипт не завершился штатно — "
                        "смотрите вывод выше")
    return out


# ---------------------------------------------------------------------------
# Добавление ключа на УЖЕ ПРОШИТУЮ колонку (без переустановки)
# ---------------------------------------------------------------------------

APPEND_KEY_SH = """#!/bin/sh
LINE='%(line)s'
for f in /data/authorized_keys /root/.ssh/authorized_keys; do
    [ -f "$f" ] || continue
    grep -qxF "$LINE" "$f" || echo "$LINE" >> "$f"
    chmod 600 "$f" 2>/dev/null
done
echo "KEY_APPENDED"
"""


def add_key(cfg, model: ModelSpec, new_pub: Path, label: str | None = None) -> str:
    """Добавить публичный ключ на уже прошитую колонку.

    Каналы по приоритету: SSH с активным ключом -> ADB root.
    """
    new_pub = Path(expand(str(new_pub)))
    if not new_pub.exists():
        raise StepError(f"файл публичного ключа не найден: {new_pub}")
    pub_line = (new_pub.read_text().strip().splitlines() or [""])[0]
    pub_line = pub_line.replace("'", "").strip()
    if not pub_line.startswith("ssh-"):
        raise StepError(f"{new_pub} не похож на публичный SSH-ключ "
                        f"(строка должна начинаться с 'ssh-')")

    ip = resolve_device_ip(cfg, model)
    info(f"Колонка {ip}: добавляю ключ {new_pub.name}")

    key_path = expand(cfg.ssh_key)
    appended = False
    if os.path.exists(key_path) and ssh_ok(ip, key_path):
        script = APPEND_KEY_SH % {"line": pub_line}
        res = run(["ssh", "-i", key_path,
                   "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null",
                   "-o", "BatchMode=yes",
                   f"root@{ip}", "sh -s"],
                  check=False, capture=True, input_text=script)
        appended = "KEY_APPENDED" in (res.stdout or "")
        if appended:
            ok("Ключ добавлен по SSH")

    if not appended:
        warn("SSH-канал не сработал — пробую через ADB root...")
        start_server()
        rooted, out = try_adb_root(ip, model.adb_port)
        if not rooted:
            raise StepError(
                f"добавить ключ не удалось: SSH с активным ключом на {ip} "
                f"не работает, ADB root недоступен ({out or 'нет ответа'}). "
                f"Проверьте IP (колонка могла сменить его) — ip neigh show "
                f"dev {cfg.ap_iface}")
        sr = serial(ip, model.adb_port)
        push(sr, new_pub, "/data/_new_key.pub")
        script = """#!/bin/sh
for f in /data/authorized_keys /root/.ssh/authorized_keys; do
    [ -f "$f" ] || continue
    grep -qxF "$(cat /data/_new_key.pub)" "$f" || cat /data/_new_key.pub >> "$f"
    chmod 600 "$f" 2>/dev/null
done
rm -f /data/_new_key.pub
echo "KEY_APPENDED"
"""
        _adb_run_script(sr, script, "_alice_add_key.sh", expect="KEY_APPENDED")
        appended = True
        ok("Ключ добавлен по ADB")

    if label:
        cfg.speakers[label] = {"key": cfg.ssh_key, "ip": ip,
                               "added_key": str(new_pub)}
        save_config(cfg)
        ok(f"Записано в конфиг: «{label}» -> ключ {cfg.ssh_key}")

    priv_guess = str(new_pub)[:-4] if str(new_pub).endswith(".pub") else None
    if priv_guess and os.path.exists(priv_guess):
        if ssh_ok(ip, priv_guess):
            ok(f"Проверка: вход с новым ключом ({priv_guess}) работает")
        else:
            warn("Вход с новым приватным ключом не подтвердился — "
                 "проверьте вручную: ssh -i {} root@{}".format(priv_guess, ip))
    cfg.device_ip = ip
    save_config(cfg)
    ok(f"Готово: ключ добавлен на колонку {ip}")
    return ip


# ---------------------------------------------------------------------------
# Установка
# ---------------------------------------------------------------------------

def resolve_device_ip(cfg, model: ModelSpec) -> str:
    """IP из аргументов/конфига, иначе быстрый авто-поиск."""
    if cfg.device_ip:
        return cfg.device_ip
    from .device import find_candidates
    from .console import info as _info
    _info("IP не задан — быстрый поиск кандидатов в сети...")
    for ip in find_candidates(cfg.ap_iface):
        rooted, _ = try_adb_root(ip, model.adb_port)
        if rooted:
            cfg.device_ip = ip
            save_config(cfg)
            return ip
    raise StepError("IP колонки неизвестен: укажите --device-ip "
                    "или выполните wait-device")


def wait_ssh_after_reboot(cfg, old_ip: str, timeout: int = 180) -> str:
    """Ждать SSH после reboot. Колонка может сменить IP (DHCP) — ищем по MAC.

    Порядок опроса: старый IP -> IP по MAC (neigh + lease-файлы) -> соседи.
    """
    mac = mac_for_ip(old_ip)
    if mac:
        info(f"MAC колонки: {mac} — после перезагрузки IP может смениться, "
             f"ищу по нему")
    else:
        warn("MAC колонки не определился — ищу SSH на всех устройствах сети")

    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = [old_ip]
        for c in ips_for_mac(mac, cfg.ap_iface) if mac else []:
            if c not in candidates:
                candidates.append(c)
        for c in find_candidates(cfg.ap_iface):
            if c not in candidates:
                candidates.append(c)
        for cand in candidates:
            b = ssh_banner(cand, timeout=3)
            if b and b.startswith("SSH-"):
                if cand != old_ip:
                    ok(f"Колонка сменила IP после перезагрузки: "
                       f"{old_ip} -> {cand}")
                info(f"SSH-баннер: {b}")
                return cand
        time.sleep(5)
    raise StepError(
        f"SSH не поднялся за {timeout} с. Частая причина — колонка сменила "
        f"IP после reboot. Найдите новый: ip neigh show dev {cfg.ap_iface}, "
        f"затем ssh -i <ключ> root@<новый_IP>, либо повторите install-ssh "
        f"--device-ip <новый_IP>")


def install_ssh(cfg, model: ModelSpec, dropbear_path: str | Path) -> str:
    """Полная установка: push, скрипты, OTA-блок, reboot, проверка SSH."""
    start_server()
    ip = resolve_device_ip(cfg, model)
    sr = serial(ip, model.adb_port)
    info(f"Колонка: {sr}")

    rooted, out = try_adb_root(ip, model.adb_port)
    if not rooted:
        raise StepError(f"ADB root недоступен на {sr}: "
                        f"{out or 'нет ответа'}")
    ok(f"ADB root подтверждён: {out.splitlines()[0] if out else 'uid=0'}")

    # запоминаем MAC — после переезда в домашнюю сеть найдём колонку по нему
    cfg.device_mac = mac_for_ip(ip) or cfg.device_mac

    key_path = expand(cfg.ssh_key)
    pub = ensure_ssh_key(cfg.ssh_key)

    info("Загружаю файлы в /data колонки...")
    push(sr, dropbear_path, "/data/dropbearmulti")
    push(sr, pub, "/data/authorized_keys")
    start_local = Path("/tmp/hackalice_start_ssh.sh")
    start_local.write_text(START_SSH_SH)
    push(sr, start_local, "/data/start_ssh.sh")

    info("Установка на колонке: ключи, S99quasar, блокировка OTA...")
    out = _adb_run_script(sr, REMOTE_INSTALL_SH, "_hackalice_install.sh")
    if "REMOTE_INSTALL_DONE" not in out:
        raise StepError("установочный скрипт не завершился штатно — "
                        "смотрите вывод выше")

    # Диагностика бут-цепочки: дамп на /data и скачивание на ноутбук.
    # Если после reboot SSH снова не поднимется — этот файл + ssh_boot.log
    # (читаются по ADB даже без SSH) покажут, как устроен старт на модели.
    try:
        diag_res = _adb_run_script(sr, BOOT_DIAG_SH, "_alice_bootdiag.sh",
                                   expect="BOOT_DIAG_DONE")
        if "BOOT_DIAG_DONE" in diag_res:
            diag_local = Path("/tmp/hackalice_boot_diag_{}.txt".format(ip))
            run(["adb", "-s", sr, "pull", "/data/boot_diag.txt", str(diag_local)],
                check=False, capture=True, quiet=True)
            info("Диагностика бут-цепочки: {}".format(diag_local))
    except StepError as e:
        warn("boot_diag не снялся (не критично): {}".format(e))

    ok("Установка завершена. Проверяю SSH без перезагрузки...")
    out = shell(sr, "/data/start_ssh.sh")
    if out.strip():
        dim("start_ssh: " + out.strip()[:400])
    time.sleep(2)
    if not ssh_ok(ip, key_path):
        warn("SSH-вход до перезагрузки не прошёл — возможно, dropbear ещё "
             "стартует; главная проверка будет после reboot")

    info("Перезагружаю колонку и жду SSH (до 180 с; IP может смениться)...")
    shell(sr, "sync")
    reboot(sr)

    ip = wait_ssh_after_reboot(cfg, ip, timeout=180)
    save_config(cfg)
    time.sleep(3)
    if ssh_ok(ip, key_path):
        ok(f"Вход `ssh -i {cfg.ssh_key} root@{ip}` работает — "
           f"ROOT И SSH ЗАКРЕПЛЕНЫ")
    else:
        raise StepError("Порт 22 открыт, но вход по ключу не удался. "
                        f"Проверьте: ssh -v -i {cfg.ssh_key} root@{ip}")

    cfg.device_ip = ip
    save_config(cfg)
    return ip


def verify_ssh(cfg) -> None:
    ip = find_speaker_by_ssh(cfg, get_model(cfg.model))
    if ip != cfg.device_ip:
        ok(f"Колонка найдена на новом адресе: {cfg.device_ip} -> {ip}")
        cfg.device_ip = ip
        save_config(cfg)
    key_path = expand(cfg.ssh_key)
    b = ssh_banner(ip)
    if not b:
        raise StepError(f"порт 22 на {ip} не отвечает")
    ok(f"SSH-баннер: {b}")
    if not ssh_ok(ip, key_path):
        raise StepError("вход по ключу не работает")
    ok(f"ssh root@{ip} — вход по ключу работает")
    cmd = ["ssh", "-i", key_path, "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
           f"root@{ip}",
           "head -c 40 /system/vendor/quasar/device_ota_update.sh; "
           "grep -c 127.0.0.1 /etc/hosts"]
    res = run(cmd, check=False, capture=True)
    print(res.stdout or "")


def unbind_account(cfg) -> None:
    """Безопасная отвязка от аккаунта по SSH (root/SSH/OTA-блок сохраняются)."""
    ip = find_speaker_by_ssh(cfg, get_model(cfg.model))
    if ip != cfg.device_ip:
        ok(f"Колонка найдена на новом адресе: {cfg.device_ip} -> {ip} "
           f"(колонка сменила сеть?)")
        cfg.device_ip = ip
        save_config(cfg)
    key_path = expand(cfg.ssh_key)
    if not ssh_ok(ip, key_path):
        raise StepError(f"нет SSH-доступа к {ip} — отвязка требует SSH. "
                        f"Если знаете IP колонки — укажите --device-ip <IP>")
    ok(f"SSH-доступ к {ip} есть")
    warn("Аппаратный сброс НЕ использовать: он вернёт factory-4 и сотрёт "
         "SSH и OTA-блок.")
    res = run(["ssh", "-i", key_path, "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=8", f"root@{ip}", "sh -s"],
              check=False, capture=True, input_text=UNBIND_SH)
    out = (res.stdout or "").strip()
    if out:
        print(out)
    if res.returncode not in (0, 255):
        warn(f"ssh вернул код {res.returncode} (обычно это разрыв связи "
             f"при reboot)")
    ok("Данные аккаунта и Wi-Fi очищены, колонка перезагружается в режим "
       "настройки.")
    info("Далее: приложение «Дом с Алисой» под НОВЫМ аккаунтом -> «+» -> "
         "«Устройство с Алисой» -> стандартная привязка.")
