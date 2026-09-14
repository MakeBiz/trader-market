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
  python3 collector/collect.py --backfill догрузить дневную историю за год
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

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
CG_PAUSE = float(os.environ.get("CG_PAUSE", "7"))     # пауза между запросами к CoinGecko
STOOQ_PAUSE = 1.2
TIMEOUT = 30
HISTORY_DAYS = 400

_log_lines = []
BACKFILL = False
backfill_rows = []
DEADLINE = None          # до какого момента работаем, дальше выходим и пишем что есть


def time_left():
    return 1e9 if DEADLINE is None else DEADLINE - time.time()


def out_of_time(reserve=60):
    """Пора закругляться: оставляем запас на запись и коммит."""
    return time_left() < reserve


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    _log_lines.append(line)
    print(line, flush=True)


def http_get(url, params=None, headers=None, retries=3, backoff_429=True):
    """GET с ретраями. backoff_429=False: на 429 сразу сдаёмся, не ждём.

    Для CoinGecko 429 означает «слишком часто, подожди», и ждать имеет смысл.
    Для Yahoo с адресов GitHub Actions это означает «отсюда не обслуживаем»,
    и ожидание только съедает бюджет прогона.
    """
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
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                if not backoff_429:
                    raise
                wait = 20 * (attempt + 1)
                if attempt < retries - 1:
                    log("  лимит запросов, жду %ss" % wait)
                    time.sleep(wait)
                    continue
            wait = 2 ** attempt
            if attempt < retries - 1:
                log("  повтор через %ss после ошибки: %s" % (wait, exc))
                time.sleep(wait)
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
            time.sleep(CG_PAUSE)  # бережём лимит free-тарифа
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


def history_depth():
    """Сколько дневных точек уже лежит по каждому тикеру."""
    depth = {}
    if not os.path.exists(PRICES_CSV):
        return depth
    with open(PRICES_CSV, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            depth[row["symbol"]] = depth.get(row["symbol"], 0) + 1
    return depth


def backfill_crypto(coins, days=365, min_depth=300):
    """Дневная история крипты за год. Пропускает то, что уже загружено,
    поэтому при обрыве по лимиту следующий прогон доберёт остаток."""
    out = []
    depth = history_depth()
    skipped = 0
    for coin in coins:
        if not coin.get("cg"):
            continue
        if coin.get("tier") == "avoid":
            continue
        if depth.get(coin["symbol"], 0) >= min_depth:
            skipped += 1
            continue
        if out_of_time(90):
            log("  время вышло, историю доберём следующим прогоном")
            break
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
                    "date": date, "symbol": coin["symbol"],
                    "asset_class": coin.get("asset_class", "crypto"),
                    "close_usd": price, "close_rub": None, "source": "coingecko:chart",
                })
            log("  история %s: %d точек" % (coin["symbol"], len(series)))
        except Exception as exc:  # noqa: BLE001
            log("  история %s не забралась: %s" % (coin["symbol"], exc))
        time.sleep(CG_PAUSE)
    if skipped:
        log("  пропущено (история уже есть): %d" % skipped)
    return out


def resolve_xstocks(items):
    """Ищем на CoinGecko сами токены Wallet вида AAPLX, TSLAX, SPYX."""
    wanted = {}
    for item in items:
        wallet = item.get("wallet")
        if wallet:
            wanted[wallet.upper().rstrip("X") + "X"] = item
    if not wanted:
        return []
    try:
        listing = json.loads(http_get(CG_BASE + "/coins/list", headers=cg_headers()))
    except Exception as exc:  # noqa: BLE001
        log("xStocks: список монет не забрался: %s" % exc)
        return []
    found = []
    for entry in listing:
        sym = entry["symbol"].upper()
        if sym in wanted and "xstock" in entry["id"].lower():
            item = wanted[sym]
            found.append({"symbol": item["wallet"], "name": item["name"] + " (токен Wallet)",
                          "cg": entry["id"], "tier": item.get("tier"), "wallet_earn": None})
    log("xStocks: нашлось токенов %d из %d" % (len(found), len(wanted)))
    return found


# --------------------------------------------------------------------- Yahoo

def fetch_yahoo_candles(symbol, days=400):
    """Дневные свечи с Yahoo Finance. Без ключа, нужен браузерный User-Agent."""
    rng = "1y" if days <= 370 else "2y"
    raw = http_get(YAHOO_CHART + urllib.parse.quote(symbol),
                   params={"range": rng, "interval": "1d"},
                   headers={"Accept": "application/json"},
                   retries=1, backoff_429=False)
    data = json.loads(raw)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        err = (data.get("chart") or {}).get("error")
        raise ValueError("пустой ответ Yahoo: %s" % err)
    res = result[0]
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    out = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        out.append({"date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "close": float(close)})
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


def fetch_daily(items, asset_class):
    """Дневные свечи: сначала Yahoo, если не вышло, то Stooq."""
    rows, report = [], {"ok": [], "failed": []}
    d2 = datetime.now(timezone.utc)
    d1 = d2 - timedelta(days=HISTORY_DAYS)
    for item in items:
        if out_of_time(120):
            log("  время вышло, остаток класса %s доберём следующим прогоном" % asset_class)
            break
        candles = []
        used = None
        if item.get("yahoo"):
            try:
                candles = fetch_yahoo_candles(item["yahoo"], HISTORY_DAYS)
                if candles:
                    used = "yahoo:" + item["yahoo"]
            except Exception as exc:  # noqa: BLE001
                log("Yahoo %s: %s" % (item["yahoo"], exc))
            time.sleep(0.6)
        symbols = [] if used else [s for s in (item.get("stooq"), item.get("alt")) if s]
        for sym in symbols:
            try:
                text = http_get(STOOQ_CSV, params={
                    "s": sym, "i": "d",
                    "d1": d1.strftime("%Y%m%d"), "d2": d2.strftime("%Y%m%d"),
                })
                candles = parse_stooq_csv(text)
                if candles:
                    used = "stooq:" + sym
                    break
                log("  Stooq %s ответил без данных: %s" % (sym, text.strip()[:120].replace("\n", " ")))
            except Exception as exc:  # noqa: BLE001
                log("Stooq %s: %s" % (sym, exc))
            time.sleep(STOOQ_PAUSE)
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
                    "close_rub": None, "source": used,
                })
        rows.append({
            "date": last["date"],
            "symbol": item["symbol"],
            "asset_class": asset_class,
            "close_usd": last["close"],
            "close_rub": None,
            "source": used,
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
    log("Дневные свечи (%s): получено %d из %d" % (asset_class, len(report["ok"]), len(items)))
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
    """Кладём строки в CSV по ключу дата+тикер: новые добавляем, сегодняшние обновляем.

    Раньше строка за текущий день писалась один раз и дальше пропускалась, поэтому
    в истории оставался первый снимок дня, а не последний. За день цена успевает
    уйти на проценты, и уровни считались по утренней цифре. Теперь каждый прогон
    переписывает строку своего дня, и к вечеру там стоит последнее известное значение.
    """
    store = {}
    if os.path.exists(PRICES_CSV):
        with open(PRICES_CSV, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                store[(row["date"], row["symbol"])] = row
    было = len(store)
    обновлено = 0
    for r in rows:
        ключ = (r["date"], r["symbol"])
        if ключ in store:
            обновлено += 1
        store[ключ] = {k: r.get(k) for k in CSV_FIELDS}
    добавлено = len(store) - было
    with open(PRICES_CSV, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows([{k: v.get(k) for k in CSV_FIELDS} for v in store.values()])
    log("История: добавлено %d строк, обновлено %d, всего ключей %d"
        % (добавлено, обновлено, len(store)))
    return добавлено


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


def clean_history(bad_sources=("fixture", "selftest")):
    """Чистит историю: строки самотеста и повторы одной пары дата+тикер.

    Повторы появляются не от сборщика, а от git: файл дописывается с двух
    сторон (робот в Actions и Мак), и слияние оставляет обе пачки строк.
    Дальше на них спотыкается запись в хранилище, поэтому чиним здесь, у
    источника: из каждой пары дата+тикер оставляем последнюю строку.
    """
    if not os.path.exists(PRICES_CSV):
        log("Истории ещё нет, чистить нечего")
        return 0
    with open(PRICES_CSV, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    clean = [r for r in rows if r.get("source") not in bad_sources]
    unique = {}
    for r in clean:
        unique[(r["date"], r["symbol"])] = r
    keep = list(unique.values())
    dropped = len(rows) - len(keep)
    if dropped:
        with open(PRICES_CSV, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows([{k: r.get(k) for k in CSV_FIELDS} for r in keep])
    log("Уборка истории: выкинуто %d строк, осталось %d" % (dropped, len(keep)))
    return dropped


# ------------------------------------------------------------------ Самотест

def selftest():
    """Проверяем парсинг и запись без сети. Настоящие данные не трогаем."""
    global DATA, PRICES_CSV, LATEST_JSON, CG_MAP, LOGS
    import tempfile
    sandbox = tempfile.mkdtemp(prefix="trader-selftest-")
    DATA = sandbox
    PRICES_CSV = os.path.join(sandbox, "prices_daily.csv")
    LATEST_JSON = os.path.join(sandbox, "latest.json")
    CG_MAP = os.path.join(sandbox, "coingecko_map.json")
    LOGS = os.path.join(sandbox, "logs")
    log("САМОТЕСТ идёт в песочнице %s" % sandbox)

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
    import shutil
    shutil.rmtree(sandbox, ignore_errors=True)
    log("САМОТЕСТ ПРОЙДЕН, песочница убрана")


# ---------------------------------------------------------------------- main

def main():
    os.makedirs(DATA, exist_ok=True)
    args = sys.argv[1:]

    if "--selftest" in args:
        selftest()
        return 0

    global BACKFILL, DEADLINE
    BACKFILL = "--backfill" in args
    budget_min = float(os.environ.get("RUN_BUDGET_MIN", "22"))
    DEADLINE = time.time() + budget_min * 60
    if "--clean" in args:
        clean_history()
    log("Старт сбора%s, %s UTC, бюджет %.0f мин" % (
        " (с историей за год)" if BACKFILL else "",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), budget_min))
    clean_history()
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

    xs = resolve_xstocks(uni["equity"] + uni["etf"])
    if xs:
        for coin in xs:
            coin["asset_class"] = "wallet_token"
        rows, rep = fetch_crypto(xs)
        for row in rows:
            row["asset_class"] = "wallet_token"
        all_rows += rows
        reports["wallet_token"] = rep

    for block, klass in (("equity", "equity"), ("etf", "etf"),
                         ("commodity", "commodity"), ("index", "index")):
        rows, rep = fetch_daily(uni[block], klass)
        all_rows += rows
        reports[block] = rep

    usdrub = fetch_usdrub()

    if not all_rows:
        log("ОШИБКА: не собрано ни одной котировки, ничего не пишу")
        write_log()
        return 1

    # Сначала фиксируем результат, и только потом тратим время на догрузку истории:
    # если прогон прервут, снапшот уже на месте
    append_history(all_rows)
    if backfill_rows:
        append_history(backfill_rows)
        log("История по акциям и товарам: %d точек" % len(backfill_rows))
    write_latest(all_rows, usdrub, reports)

    if BACKFILL and not out_of_time(120):
        log("Догружаю историю, осталось времени %.0f мин" % (time_left() / 60))
        hist_rows = backfill_crypto(coins + xs)
        if hist_rows:
            append_history(hist_rows)
            write_latest(all_rows, usdrub, reports)
    elif BACKFILL:
        log("На догрузку истории времени не осталось, доберём следующим прогоном")

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
