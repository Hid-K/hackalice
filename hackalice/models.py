"""Реестр моделей Яндекс Станций.

Каждая модель описывает платформу, архитектуру, метод получения root,_method
проверенную прошивку для отката и инструкцию аппаратного сброса. Новые модели
добавляются сюда — остальной код читает спецификацию.

Методы (field `method`):
  METHOD_DNS       — подмена DNS: обычный сброс, сервер разблокировки (lite-kit
                     в обычном режиме, БЕЗ маркера downgrade), привязка в
                     приложении -> ADB root. Прошивка не скачивается.
  METHOD_DOWNGRADE — сервер обновлений с маркером downgrade: аппаратный сброс
                     в factory-образ, сервер отдаёт прошивку с открытым ADB
                     (например, .46 у Лайт 1), затем привязка -> ADB root.

Статусы:
  supported     — метод проверен сообществом, есть инструкция сброса
  experimental  — по косвенным данным должно работать, требует проверки
  planned       — в планах, визард не даст продолжить

Источники методов: вики https://hackalice.me (Yandex Station Extended) и
репозиторий lite-kit (krevetkqqq/lite-kit).
"""
from __future__ import annotations

from dataclasses import dataclass

SUPPORTED = "supported"
EXPERIMENTAL = "experimental"
PLANNED = "planned"

METHOD_DNS = "dns"
METHOD_DOWNGRADE = "downgrade"

_STATUS_LABEL = {
    SUPPORTED: "поддерживается",
    EXPERIMENTAL: "экспериментально",
    PLANNED: "в планах",
}
_STATUS_MARK = {SUPPORTED: "[+] ", EXPERIMENTAL: "[~] ", PLANNED: "[ ] "}
_METHOD_LABEL = {
    METHOD_DNS: "подмена DNS",
    METHOD_DOWNGRADE: "откат прошивки (lite-kit downgrade)",
    "": "—",
}


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str
    code: str                     # YNDX-код или ""
    platform: str                 # платформа lite-kit/OTA (yandexmicro и т.п.)
    arch: str
    status: str
    method: str = METHOD_DNS      # как получать root (см. шапку модуля)
    firmware: str = ""            # проверенная прошивка для отката ("" — TBD)
    adb_port: int = 5555
    reset_instructions: str = ""  # аппаратный сброс (по модели)
    notes: str = ""

    @property
    def status_label(self) -> str:
        return _STATUS_LABEL[self.status]

    @property
    def method_label(self) -> str:
        return _METHOD_LABEL.get(self.method, self.method)

    @property
    def summary_line(self) -> str:
        parts = [self.name]
        if self.code:
            parts.append(f"({self.code})")
        parts.append(f"— {self.status_label}, {self.method_label}")
        return " ".join(parts)


RESET_LITE_1 = """\
АППАРАТНЫЙ СБРОС В FACTORY-4 (точный тайминг важен):
 1. Выньте кабель питания колонки и подождите 5 секунд.
 2. Вставьте кабель обратно.
 3. Ровно через 1 секунду (панель инициализируется, загорится кнопка-треугольник)
    приложите палец к ЦЕНТРАЛЬНОЙ сенсорной кнопке.
 4. Держите 7-10 секунд, пока индикатор не загорится ЖЁЛТО-ЗЕЛЁНЫМ, отпустите.
 5. Дождитесь перезагрузки из recovery (1-2 мин) — кнопка плавно мигает БЕЛЫМ.
"""

RESET_HOLD_POWER = """\
АППАРАТНЫЙ СБРОС (кнопка + подача питания):
 1. Отключите кабель питания.
 2. Зажмите кнопку отключения микрофона (или кнопку паузы на корпусе).
 3. Не отпуская кнопку, вставьте кабель питания.
 4. Держите 10-15 секунд, пока подсветка не замигает и Алиса не скажет
    о начале сброса.
 5. Отпустите; через 1-2 минуты колонка предложит настройку в приложении.
"""

RESET_MIDI = """\
АППАРАТНЫЙ СБРОС (проверено на живой Миди, YNDX-00054):
 1. Отключите кабель питания.
 2. Зажмите кнопку отключения микрофона на задней панели.
 3. Не отпуская кнопку, включите питание.
 4. Дождитесь СИНЕ-ЖЁЛТОГО цвета на окантовке.
 5. Ждите, пока Алиса сбросится и начнёт предлагать настроиться
    (1-2 минуты) — дальше привязка в приложении.
"""

RESET_GENERIC = """\
Для этой модели инструкция аппаратного сброса пока не документирована в
реестре. Добавьте её в hackalice/models.py при проверке.
"""


REGISTRY: list[ModelSpec] = [
    ModelSpec(
        key="midi",
        name="Яндекс Станция Миди (с Zigbee, LED-часы)",
        code="YNDX-00054",
        platform="cucumber",
        arch="aarch64",
        status=SUPPORTED,
        method=METHOD_DNS,
        reset_instructions=RESET_MIDI,
        notes="SoC Amlogic A5 (Cortex-A55), ядро 5.4, Buildroot 2023.11.1, "
              "eMMC. Root — DNS-метод без отката прошивки: сервер lite-kit "
              "в обычном режиме отвечает активацией ADB. Метод проверен "
              "сообществом (hackalice.me, devices/station-midi; lite-kit "
              "проверял factory-9 без смены прошивки). "
              "ВАЖНО (живой запуск): (1) корень смонтирован ro — правки "
              "только через remount,rw; (2) резолвер unbound колонки обходит "
              "/etc/hosts, поэтому блок OTA 'заглушкой + hosts' на Миди "
              "ненадёжен (swupdate) — после установки дополнительно "
              "блокируйте update-домены на РОУТЕРЕ для MAC колонки; "
              "(3) после unbind (wipe /data/quasar) наблюдались лаги и "
              "bootloop в домашней сети — лечится полным сбросом; перед "
              "переводом в домашнюю сеть держите колонку на хотспоте.",
    ),
    ModelSpec(
        key="lite-1",
        name="Яндекс Станция Лайт",
        code="YNDX-00025",
        platform="yandexmicro",
        arch="aarch64",
        status=SUPPORTED,
        method=METHOD_DOWNGRADE,
        firmware="0.369.1.46.4778262442.20261013.297",
        reset_instructions=RESET_LITE_1,
        notes="Все кнопки сенсорные. На сборках до 21.10.2026 работает и "
              "простой DNS-метод; на новых — откат на .46 через factory-4.",
    ),
    ModelSpec(
        key="mini-2",
        name="Яндекс Станция Мини 2",
        code="YNDX-00020",
        platform="yandexmini_2",
        arch="aarch64",
        status=EXPERIMENTAL,
        method=METHOD_DOWNGRADE,
        firmware="0.369.1.46.4778262943.20261013.299",
        reset_instructions=RESET_HOLD_POWER,
        notes="На сборках после 21.10.2026 ADB поднимается только через "
              "откат из factory-3. Важно: Quasar гасит ADB после первой "
              "синхронизации с облаком — сразу ставьте SSH (install-ssh).",
    ),
    ModelSpec(
        key="mini-1",
        name="Яндекс Станция Мини (2019)",
        code="YNDX-0004",
        platform="yandexmini",
        arch="aarch64",
        status=EXPERIMENTAL,
        method=METHOD_DOWNGRADE,
        firmware="1.305.4.16.3237824040.20250721",
        reset_instructions=RESET_HOLD_POWER,
        notes="Основной способ сообщества — свой образ по USB (Station Mini "
              "Root); в hackalice реализован запасной путь — downgrade через "
              "lite-kit (проверен в lite-kit на yandexmini).",
    ),
    ModelSpec(
        key="station-2",
        name="Яндекс Станция 2",
        code="YNDX-00051",
        platform="unknown",
        arch="aarch64",
        status=EXPERIMENTAL,
        method=METHOD_DNS,
        reset_instructions=RESET_HOLD_POWER,
        notes="DNS-метод проверен сообществой (hackalice.me). Платформа "
              "lite-kit для OTA не уточнена — для DNS-метода не нужна.",
    ),
    ModelSpec(
        key="station-3",
        name="Яндекс Станция 3 (Orion)",
        code="",
        platform="orion",
        arch="aarch64",
        status=EXPERIMENTAL,
        method=METHOD_DNS,
        reset_instructions=RESET_GENERIC,
        notes="DNS-метод как на других Amlogic (hackalice.me); свой "
              "сертификат колонка не примет — только активация ADB.",
    ),
    ModelSpec(
        key="hub",
        name="Яндекс Умный хаб (Saturn)",
        code="",
        platform="saturn",
        arch="aarch64",
        status=EXPERIMENTAL,
        method=METHOD_DNS,
        reset_instructions=RESET_GENERIC,
        notes="DNS-метод проверен сообществой (hackalice.me).",
    ),
    ModelSpec(
        key="station-street",
        name="Яндекс Станция Стрит (Mango)",
        code="",
        platform="mango",
        arch="aarch64",
        status=EXPERIMENTAL,
        method=METHOD_DNS,
        reset_instructions=RESET_HOLD_POWER,
        notes="DNS-метод проверен сообществой (hackalice.me).",
    ),
    ModelSpec(
        key="lite-2",
        name="Яндекс Станция Лайт 2 (Plum)",
        code="",
        platform="plum",
        arch="aarch64",
        status=PLANNED,
        method="",
        reset_instructions=RESET_HOLD_POWER,
        notes="Метод сообщества — сертификат из factory-образа (eQuasar + "
              "nginx + свой DNS, hackalice.me /retail-ca); в hackalice пока "
              "не реализован.",
    ),
    ModelSpec(
        key="mini-3",
        name="Яндекс Станция Мини 3 (Bergamot)",
        code="YNDX-00652*",
        platform="bergamot",
        arch="aarch64",
        status=PLANNED,
        method="",
        reset_instructions=RESET_HOLD_POWER,
        notes="Метод сообщества — сертификат и маркеры из factory "
              "(hackalice.me /retail-ca-mini3); в hackalice пока не реализован.",
    ),
    ModelSpec(
        key="mini-3-pro",
        name="Яндекс Станция Мини 3 Про (Pickle)",
        code="YNDX-00652*",
        platform="pickle",
        arch="aarch64",
        status=PLANNED,
        method="",
        reset_instructions=RESET_GENERIC,
        notes="Root только с factory .8/.12 (hackalice.me); на .50 способа нет.",
    ),
    ModelSpec(
        key="station-max",
        name="Яндекс Станция Макс (Android TV)",
        code="YNDX-0008",
        platform="yandexstation_2",
        arch="aarch64",
        status=PLANNED,
        method="",
        reset_instructions=RESET_HOLD_POWER,
        notes="Android TV, не Buildroot: DNS-метод даёт временный ADB, "
              "постоянный root — отдельный эксплойт (hackalice.me). "
              "Текущий установщик SSH/OTA не подходит.",
    ),
    ModelSpec(
        key="station-1",
        name="Яндекс Станция (1 поколения)",
        code="YNDX-0001",
        platform="unknown",
        arch="aarch64",
        status=PLANNED,
        method="",
        reset_instructions=RESET_GENERIC,
        notes="Метод сообщества — AliceBridge с Android-телефона (свой образ "
              "boot); в hackalice пока не реализован.",
    ),
    ModelSpec(
        key="fiero-hi",
        name="Fiero Hi (партнёрская Лайт, blackthorn)",
        code="",
        platform="fiero",
        arch="aarch64",
        status=PLANNED,
        method="",
        reset_instructions=RESET_GENERIC,
        notes="DNS-метод через полный заводской сброс (hackalice.me "
              "/devices/fiero-hi); в hackalice пока не реализован.",
    ),
]

# Коды с * в реестре — требуют уточнения при добавлении реальной поддержки.


def get_model(key: str) -> ModelSpec:
    for m in REGISTRY:
        if m.key == key:
            return m
    known = ", ".join(m.key for m in REGISTRY)
    raise KeyError(f"неизвестная модель '{key}' (доступны: {known})")


def choose_model(current: str = "") -> ModelSpec:
    """Интерактивный выбор модели (questionary). Плановые — заблокированы."""
    import questionary
    from .console import err

    order = {SUPPORTED: 0, EXPERIMENTAL: 1, PLANNED: 2}
    models = sorted(REGISTRY, key=lambda m: (order[m.status], m.name))
    choices = []
    for m in models:
        mark = _STATUS_MARK[m.status]
        suffix = f" — {m.status_label}, {m.method_label}"
        if m.code:
            suffix += f", {m.code}"
        choices.append(questionary.Choice(f"{mark}{m.name}{suffix}",
                                          value=m.key))
    answer = questionary.select(
        "Выберите модель устройства:",
        choices=choices,
        default=next((c for c in choices if c.value == current), None),
    ).ask()
    if answer is None:
        raise KeyboardInterrupt
    model = get_model(answer)
    if model.status == PLANNED:
        err(f"{model.name}: статус «{model.status_label}» — продолжение "
            f"невозможно. {model.notes}")
        raise KeyboardInterrupt
    if model.status == EXPERIMENTAL:
        from .console import warn
        warn(f"{model.name}: статус «экспериментально». {model.notes}")
        if not questionary.confirm(
                "Продолжить на свой страх и риск?", default=False).ask():
            raise KeyboardInterrupt
    return model
