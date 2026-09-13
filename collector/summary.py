#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Короткая сводка последнего сбора, для вывода в лог GitHub Actions."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    latest = os.path.join(ROOT, "data", "latest.json")
    if not os.path.exists(latest):
        print("Снапшота нет, сбор не дал результата")
        return 0
    with open(latest, encoding="utf-8") as fh:
        d = json.load(fh)
    total = sum(d.get("counts", {}).values())
    print("### Сбор котировок")
    print("")
    print("Собрано %d инструментов, %s" % (total, d.get("generated_at")))
    print("")
    print("| Класс | Штук |")
    print("|---|---|")
    for key, val in d.get("counts", {}).items():
        print("| %s | %s |" % (key, val))
    failed = d.get("failed") or {}
    if failed:
        print("")
        print("**Не разрешились:**")
        for key, val in failed.items():
            print("- %s: %s" % (key, ", ".join(val)))
    hist = os.path.join(ROOT, "data", "prices_daily.csv")
    if os.path.exists(hist):
        with open(hist, encoding="utf-8") as fh:
            lines = sum(1 for _ in fh) - 1
        print("")
        print("История цен: %d строк" % lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
