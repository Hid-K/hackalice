#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обратная совместимость: старый однофайловый запуск теперь оборачивает пакет.

Раньше это был автономный скрипт; теперь логика живёт в пакете hackalice.
Работают оба варианта:
  sudo python3 alice_root.py run     # эквивалент: sudo hackalice wizard
  sudo hackalice                     # интерактивный мастер
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hackalice.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["wizard"] if len(sys.argv) <= 1 else sys.argv[1:]))
