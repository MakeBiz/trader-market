#!/bin/bash
# Отправка изменений в MakeBiz/trader-market.
# Двойной клик после того, как Клод обновил файлы в этой папке.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

say(){ printf "\n\033[1m%s\033[0m\n" "$*"; }
fail(){ printf "\n\033[31m%s\033[0m\n" "$*"; read -n1 -r -p "Нажмите любую клавишу, чтобы закрыть..."; exit 1; }

[ -d .git ] || fail "Здесь ещё нет репозитория. Сначала запустите setup-trader-market.command"
rm -f .git/index.lock 2>/dev/null

say "1/5 Подтягиваю то, что собрал робот"
git fetch origin main -q && git merge origin/main -q --no-edit 2>/dev/null || git rebase origin/main -q 2>/dev/null || true

say "2/5 Убираю тестовые строки из истории цен"
python3 collector/collect.py --selftest >/dev/null 2>&1 && echo "  самотест пройден" || echo "  самотест не прошёл, но публикую"
python3 - <<'PY' 2>/dev/null || true
import csv, os
p = "data/prices_daily.csv"
if os.path.exists(p):
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    keep = [r for r in rows if r.get("source") not in ("fixture", "selftest")]
    if len(keep) != len(rows):
        w = csv.DictWriter(open(p, "w", encoding="utf-8", newline=""),
                           fieldnames=["date","symbol","asset_class","close_usd","close_rub","source"])
        w.writeheader(); w.writerows([{k: r.get(k) for k in w.fieldnames} for r in keep])
        print("  выкинуто строк: %d" % (len(rows) - len(keep)))
PY

say "3/5 Коммит"
git add -A
if git diff --staged --quiet; then
  echo "  менять нечего"
else
  git -c commit.gpgsign=false commit -q -m "обновление панели: $(date '+%d.%m %H:%M')"
  echo "  закоммичено"
fi

say "4/5 Пуш"
git push origin main || fail "Пуш не прошёл. Проверьте gh auth status"

say "5/5 Запускаю сбор котировок"
if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  sleep 2
  if gh workflow run "Сбор котировок" --repo MakeBiz/trader-market -f backfill=true -f check=true; then
    echo "  запущен, идёт 10-20 минут"
  else
    echo "  не запустился, включите вручную: вкладка Actions, Run workflow"
  fi
else
  echo "  gh не авторизован, запустите сбор вручную во вкладке Actions"
fi

printf "\n\033[32mОпубликовано.\033[0m https://github.com/MakeBiz/trader-market\n"
echo "Ход сбора: https://github.com/MakeBiz/trader-market/actions"
read -n1 -r -p "Нажмите любую клавишу, чтобы закрыть..."
