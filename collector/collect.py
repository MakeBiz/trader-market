#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборщик котировок для панели Трейдера.

Источники (все бесплатные, ключи не обязательны):
  - CoinGecko  : криптовалюты, цены в USD и RUB, изменения за 24ч/7д/30д
  - Stooq      : акции США, ETF, товары, индексы (дневные свечи, CSV)
  - ЦБ РФ      : официальный курс USD/RUB

Результат:
  data/prices_daily.csv     история: date,symbol,asset_class,close_usd,close_rub,source
  data/latest.json          свежий снапшот со всеми изменениями
  data/logs/collect-*.log   что получилось, а что нет

Зависимостей нет, только стандартная библиотека. Запуск:
  python3 collector/collect.py            обычный сбор
  python3 collector/collect.py --selftest проверка логики на фикстурах, без сети
  python3 collector/collect.py --check    сбор + отчёт, какие тикеры не разрешились
"""

import csv
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
LOGS = os.path.join(DATA, "logs")
UNIVERSE_PATH = os.path.join(ROOT, "universe.json")
PRICES_CSV = os.path.join(DATA, "prices_daily.csv")
LATEST_JSON = os.path.join(DATA, "latest.json")
CG_MAP = os.path.join(DATA, "coingecko_map.json")

CG_BASE = "https://api.coingecko.com/api/v3"
CG_KEY = os.environ.get("COINGECKO_KEY", "").strip()
STOOQ_CSV = "https://stooq.com/q/d/l/"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

USER_AGENT = "trader-panel/1.0 (+github actions collector)"
TIMEOUT = 30
HISTORY_DAYS = 400

_log_lines = []
BACKFILL = False
backfill_rows = []


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    _log_lines.append(line)
    print(line, flush=True)


def http_get(url, params=None, headers=None, retries=3):
    """GET с ретраями. Возвращает текст или бросает исключение."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2 ** attempt
            if attempt < retries - 1:
                log("  повтор через %ss после ошибки: %s" % (wait, exc))
                time.sleep(wait)
    raise last


def load_universe():
    with open(UNIVERSE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------- CoinGecko

def cg_headers():
    return {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}


def resolve_coingecko_ids(coins):
    """Достаём id для монет, у которых он не прописан или оказался неверным."""
    missing = [c for c in coins if not c.get("cg")]
    if not missing:
        return {}
    log("Разрешаю %d id через /coins/list" % len(missing))
    try:
        raw = http_get(CG_BASE + "/coins/list", headers=cg_headers())
        listing = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log("  не удалось получить список монет: %s" % exc)
        return {}
    by_symbol = {}
    for item in listing:
        by_symbol.setdefault(item["symbol"].upper(), []).append(item["id"])
    resolved = {}
    for coin in missing:
        candidates = by_symbol.get(coin["symbol"].upper(), [])
        if len(candidates) == 1:
            resolved[coin["symbol"]] = candidates[0]
            log("  %s -> %s" % (coin["symbol"], candidates[0]))
        elif candidates:
            log("  %s неоднозначен (%d вариантов), пропускаю" % (coin["symbol"], len(candidates)))
    if resolved:
        with open(CG_MAP, "w", encoding="utf-8") as fh:
            json.dump(resolved, fh, ensure_ascii=False, indent=2)
    return resolved


def fetch_crypto(coins):
    """Цены крипты в USD и RUB одним запросом на валюту."""
    rows, report = [], {"ok": [], "failed": []}
    ids = [c["cg"] for c in coins if c.get("cg")]
    if not ids:
        return rows, report
    by_id = {c["cg"]: c for c in coins if c.get("cg")}
    data = {}
    for vs in ("usd", "rub"):
        for chunk_start in range(0, len(ids), 100):
            chunk = ids[chunk_start:chunk_start + 100]
            try:
                raw = http_get(
                    CG_BASE + "/coins/markets",
                    params={
                        "vs_currency": vs,
                        "ids": ",".join(chunk),
                        "price_change_percentage": "24h,7d,30d",
                        "per_page": 100,
                    },
                    headers=cg_headers(),
                )
                for item in json.loads(raw):
                    rec = data.setdefault(item["id"], {})
                    rec["price_" + vs] = item.get("current_price")
                    if vs == "usd":
                        rec["chg_24h"] = item.get("price_change_percentage_24h_in_currency")
                        rec["chg_7d"] = item.get("price_change_percentage_7d_in_currency")
                        rec["chg_30d"] = item.get("price_change_percentage_30d_in_currency")
                        rec["volume"] = item.get("total_volume")
                        rec["mcap"] = item.get("market_cap")
                        rec["high_24h"] = item.get("high_24h")
                        rec["low_24h"] = item.get("low_24h")
                        rec["ath"] = item.get("ath")
                        rec["ath_pct"] = item.get("ath_change_percentage")
            except Exception as exc:  # noqa: BLE001
                log("CoinGecko %s: чанк не забрался: %s" % (vs, exc))
            time.sleep(2.5)  # бережём лимит free-тарифа
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for cg_id, rec in data.items():
        coin = by_id.get(cg_id)
        if not coin or rec.get("price_usd") is None:
            continue
        rows.append({
            "date": today,
            "symbol": coin["symbol"],
            "asset_class": "crypto",
            "close_usd": rec.get("price_usd"),
            "close_rub": rec.get("price_rub"),
            "source": "coingecko",
            "extra": rec,
            "name": coin.get("name"),
            "tier": coin.get("tier"),
            "wallet_earn": coin.get("wallet_earn"),
        })
        report["ok"].append(coin["symbol"])
    got = {r["symbol"] for r in rows}
    report["failed"] = [c["symbol"] for c in coins if c["symbol"] not in got]
    log("CoinGecko: получено %d из %d" % (len(report["ok"]), len(coins)))
    return rows, report


def backfill_crypto(coins, days=365):
    """Дневная история крипты за год: по одному запросу на монету, не спеша."""
    out = []
    for coin in coins:
        if not coin.get("cg"):
            continue
        if coin.get("tier") == "avoid":
            continue
        try:
            raw = http_get(
                CG_BASE + "/coins/%s/market_chart" % coin["cg"],
                params={"vs_currency": "usd", "days": days, "interval": "daily"},
                headers=cg_headers(),
            )
            series = json.loads(raw).get("prices", [])
            for ts, price in series:
                date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                out.append({
                    "date": date, "symbol": coin["symbol"], "asset_class": "crypto",
                    "close_usd": price, "close_rub": None, "source": "coingecko:chart",
                })
            log("  история %s: %d точек" % (coin["symbol"], len(series)))
        except Exception as exc:  # noqa: BLE001
            log("  история %s не забралась: %s" % (coin["symbol"], exc))
        time.sleep(3)
    return out


# --------------------------------------------------------------------- Stooq

def parse_stooq_csv(text):
    """CSV Stooq -> список свечей по возрастанию даты."""
    out = []
    reader = csv.DictReader(io.StringIO(text.strip()))
    for row in reader:
        close = row.get("Close") or row.get("close")
        date = row.get("Date") or row.get("date")
        if not close or not date or close in ("N/D", "null"):
            continue
        try:
            out.append({"date": date, "close": float(close)})
        except ValueError:
            continue
    return out


def fetch_stooq(items, asset_class):
    """Дневные свечи с Stooq. Один запрос на тикер, поэтому идём не спеша."""
    rows, report = [], {"ok": [], "failed": []}
    d2 = datetime.now(timezone.utc)
    d1 = d2 - timedelta(days=HISTORY_DAYS)
    for item in items:
        symbols = [s for s in (item.get("stooq"), item.get("alt")) if s]
        candles = []
        used = None
        for sym in symbols:
            try:
                text = http_get(STOOQ_CSV, params={
                    "s": sym, "i": "d",
                    "d1": d1.strftime("%Y%m%d"), "d2": d2.strftime("%Y%m%d"),
                })
                candles = parse_stooq_csv(text)
                if candles:
                    used = sym
                    break
            except Exception as exc:  # noqa: BLE001
                log("Stooq %s: %s" % (sym, exc))
            time.sleep(0.7)
        if not candles:
            report["failed"].append(item["symbol"])
            continue
        last = candles[-1]

        def change(days):
            if len(candles) <= days:
                return None
            prev = candles[-1 - days]["close"]
            return None if not prev else (last["close"] / prev - 1) * 100

        if BACKFILL:
            for c in candles:
                backfill_rows.append({
                    "date": c["date"], "symbol": item["symbol"],
                    "asset_class": asset_class, "close_usd": c["close"],
                    "close_rub": None, "source": "stooq:" + used,
                })
        rows.append({
            "date": last["date"],
            "symbol": item["symbol"],
            "asset_class": asset_class,
            "close_usd": last["close"],
            "close_rub": None,
            "source": "stooq:" + used,
            "extra": {
                "chg_24h": change(1),
                "chg_7d": change(5),
                "chg_30d": change(21),
                "history": [[c["date"], c["close"]] for c in candles[-180:]],
            },
            "name": item.get("name"),
            "tier": item.get("tier"),
            "wallet": item.get("wallet"),
        })
        report["ok"].append(item["symbol"])
        time.sleep(0.7)
    log("Stooq (%s): получено %d из %d" % (asset_class, len(report["ok"]), len(items)))
    return rows, report


# ----------------------------------------------------------------- USD / RUB

def fetch_usdrub():
    try:
        raw = http_get(CBR_URL)
        data = json.loads(raw)
        val = data["Valute"]["USD"]["Value"]
        prev = data["Valute"]["USD"]["Previous"]
        log("ЦБ РФ: USD/RUB = %.4f" % val)
        return {"value": val, "previous": prev, "date": data.get("Date")}
    except Exception as exc:  # noqa: BLE001
        log("ЦБ РФ недоступен: %s" % exc)
        return None


# ------------------------------------------------------------------- Хранение

CSV_FIELDS = ["date", "symbol", "asset_class", "close_usd", "close_rub", "source"]


def append_history(rows):
    """Дописываем в CSV, не плодя дублей за тот же день и тикер."""
    existing = set()
    if os.path.exists(PRICES_CSV):
        with open(PRICES_CSV, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                existing.add((row["date"], row["symbol"]))
    new_rows = [r for r in rows if (r["date"], r["symbol"]) not in existing]
    write_header = not os.path.exists(PRICES_CSV)
    with open(PRICES_CSV, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for row in new_rows:
            writer.writerow({k: row.get(k) for k in CSV_FIELDS})
    log("История: добавлено %d строк, всего уникальных ключей %d"
        % (len(new_rows), len(existing) + len(new_rows)))
    return len(new_rows)


def write_latest(rows, usdrub, reports):
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "usdrub": usdrub,
        "counts": {k: len(v.get("ok", [])) for k, v in reports.items()},
        "failed": {k: v.get("failed", []) for k, v in reports.items() if v.get("failed")},
        "instruments": {},
    }
    for row in rows:
        extra = row.get("extra") or {}
        snapshot["instruments"][row["symbol"]] = {
            "name": row.get("name"),
            "class": row["asset_class"],
            "tier": row.get("tier"),
            "wallet": row.get("wallet"),
            "wallet_earn": row.get("wallet_earn"),
            "date": row["date"],
            "usd": row.get("close_usd"),
            "rub": row.get("close_rub"),
            "chg_24h": extra.get("chg_24h"),
            "chg_7d": extra.get("chg_7d"),
            "chg_30d": extra.get("chg_30d"),
            "high_24h": extra.get("high_24h"),
            "low_24h": extra.get("low_24h"),
            "ath_pct": extra.get("ath_pct"),
            "volume": extra.get("volume"),
            "history": extra.get("history"),
            "source": row["source"],
        }
    with open(LATEST_JSON, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=1)
    log("Снапшот: %d инструментов" % len(snapshot["instruments"]))
    return snapshot


def write_log():
    os.makedirs(LOGS, exist_ok=True)
    name = "collect-%s.log" % datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(os.path.join(LOGS, name), "a", encoding="utf-8") as fh:
        fh.write("\n".join(_log_lines) + "\n")


# ------------------------------------------------------------------ Самотест

def selftest():
    """Проверяем парсинг и запись без сети."""
    log("САМОТЕСТ: парсинг Stooq")
    sample = "Date,Open,High,Low,Close,Volume\n2026-09-09,10,11,9,10.5,100\n2026-09-10,10.5,12,10,11.5,120\n"
    candles = parse_stooq_csv(sample)
    assert len(candles) == 2, candles
    assert candles[-1]["close"] == 11.5
    log("  ок, свечей %d, последняя %.2f" % (len(candles), candles[-1]["close"]))

    log("САМОТЕСТ: запись истории и снапшота")
    rows = [
        {"date": "2026-09-13", "symbol": "BTC", "asset_class": "crypto",
         "close_usd": 77219.2, "close_rub": 6507875.85, "source": "fixture",
         "name": "Bitcoin", "tier": "core",
         "extra": {"chg_24h": 0.13, "chg_7d": -2.4, "chg_30d": -8.1}},
        {"date": "2026-09-12", "symbol": "SPY", "asset_class": "etf",
         "close_usd": 760.1, "close_rub": None, "source": "fixture",
         "name": "SPDR S&P 500", "tier": "core",
         "extra": {"chg_24h": -0.61, "history": [["2026-09-11", 764.8], ["2026-09-12", 760.1]]}},
    ]
    added = append_history(rows)
    assert added == 2, added
    again = append_history(rows)
    assert again == 0, "повторный прогон не должен плодить дубли"
    snap = write_latest(rows, {"value": 84.22, "previous": 84.1, "date": "2026-09-13"},
                        {"crypto": {"ok": ["BTC"], "failed": []},
                         "etf": {"ok": ["SPY"], "failed": []}})
    assert snap["instruments"]["BTC"]["usd"] == 77219.2
    assert snap["usdrub"]["value"] == 84.22
    log("  ок, дубли не пишутся, снапшот валиден")

    log("САМОТЕСТ: вселенная")
    uni = load_universe()
    for block in ("crypto", "equity", "etf", "commodity", "index"):
        assert block in uni and uni[block], block
        for item in uni[block]:
            assert "symbol" in item and "tier" in item, item
    log("  ок, вселенная: %s" % ", ".join(
        "%s=%d" % (b, len(uni[b])) for b in ("crypto", "equity", "etf", "commodity", "index")))
    log("САМОТЕСТ ПРОЙДЕН")


# ---------------------------------------------------------------------- main

def main():
    os.makedirs(DATA, exist_ok=True)
    args = sys.argv[1:]

    if "--selftest" in args:
        selftest()
        write_log()
        return 0

    global BACKFILL
    BACKFILL = "--backfill" in args
    log("Старт сбора%s, %s UTC" % (
        " (с историей за год)" if BACKFILL else "",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")))
    uni = load_universe()
    all_rows, reports = [], {}

    coins = uni["crypto"]
    resolved = resolve_coingecko_ids(coins)
    for coin in coins:
        if not coin.get("cg") and coin["symbol"] in resolved:
            coin["cg"] = resolved[coin["symbol"]]
    rows, rep = fetch_crypto(coins)
    all_rows += rows
    reports["crypto"] = rep

    for block, klass in (("equity", "equity"), ("etf", "etf"),
                         ("commodity", "commodity"), ("index", "index")):
        rows, rep = fetch_stooq(uni[block], klass)
        all_rows += rows
        reports[block] = rep

    if BACKFILL:
        log("Догружаю историю крипты за год")
        hist_rows = backfill_crypto(coins)
        if hist_rows:
            append_history(hist_rows)
        if backfill_rows:
            append_history(backfill_rows)
            log("История по акциям и товарам: %d точек" % len(backfill_rows))

    usdrub = fetch_usdrub()

    if not all_rows:
        log("ОШИБКА: не собрано ни одной котировки, ничего не пишу")
        write_log()
        return 1

    append_history(all_rows)
    write_latest(all_rows, usdrub, reports)

    if "--check" in args:
        log("--- НЕ РАЗРЕШИЛИСЬ ---")
        for block, rep in reports.items():
            if rep.get("failed"):
                log("%s: %s" % (block, ", ".join(rep["failed"])))

    write_log()
    log("Готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())
