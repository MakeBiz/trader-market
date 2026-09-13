#!/bin/bash
# Создание репозитория MakeBiz/trader-market, включение Actions и первый сбор котировок.
# Запуск: двойной клик в Finder. Эта папка = корень репозитория.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

OWNER="MakeBiz"
NAME="trader-market"
VISIBILITY="--public"   # публичный намеренно: иначе Claude не прочитает котировки

say(){ printf "\n\033[1m%s\033[0m\n" "$*"; }
fail(){ printf "\n\033[31m%s\033[0m\n" "$*"; read -n1 -r -p "Нажмите любую клавишу, чтобы закрыть..."; exit 1; }

say "1/6 Проверка инструментов"
command -v git >/dev/null || fail "git не найден. Установите Xcode CLT: xcode-select --install"
if ! command -v gh >/dev/null; then
  if command -v brew >/dev/null; then say "Ставлю gh через Homebrew..."; brew install gh || fail "Не удалось поставить gh";
  else fail "gh (GitHub CLI) не найден. Установите Homebrew (brew.sh) или gh (cli.github.com) и запустите снова"; fi
fi

say "2/6 Авторизация в GitHub"
if ! gh auth status >/dev/null 2>&1; then
  say "Нужен вход. Откроется браузер или появится код..."
  gh auth login || fail "Вход в GitHub не выполнен"
fi

say "3/6 Локальный репозиторий"
# workflow приехал как workflow-collect.yml: папку .github мост записать не может
if [ -f workflow-collect.yml ]; then
  mkdir -p .github/workflows
  mv -f workflow-collect.yml .github/workflows/collect.yml
  echo "  workflow на месте: .github/workflows/collect.yml"
fi
rm -f .git/index.lock 2>/dev/null
if [ ! -d .git ]; then
  git init -q
  git add -A
  git -c commit.gpgsign=false commit -q -m "Панель трейдера: сборщик котировок и дашборд"
fi
git branch -M main

say "4/6 Создание $OWNER/$NAME"
if gh repo view "$OWNER/$NAME" >/dev/null 2>&1; then
  say "Репозиторий уже есть, привязываю remote и пушу"
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/$OWNER/$NAME.git"
  git push -u origin main || fail "Пуш не прошёл. Проверьте права на $OWNER"
else
  gh repo create "$OWNER/$NAME" $VISIBILITY --source=. --remote=origin --push \
    --description "Котировки и панель трейдера: сбор в GitHub Actions, портфель считается локально" \
    || fail "Не удалось создать репозиторий"
fi

say "5/6 Разрешаю Actions писать в репозиторий"
gh api -X PUT "repos/$OWNER/$NAME/actions/permissions/workflow" \
  -f default_workflow_permissions=write -F can_approve_pull_request_reviews=false >/dev/null 2>&1 \
  || say "  не получилось автоматически, включите вручную: Settings, Actions, General, Workflow permissions, Read and write"

say "6/6 Первый сбор с историей за год (идёт 10-20 минут)"
sleep 3
gh workflow run "Сбор котировок" --repo "$OWNER/$NAME" -f backfill=true -f check=true \
  || say "  запустите вручную: вкладка Actions, Сбор котировок, Run workflow, backfill = true"

printf "\n\033[32mГотово.\033[0m\n"
echo "Репозиторий: https://github.com/$OWNER/$NAME"
echo "Ход сбора:   https://github.com/$OWNER/$NAME/actions"
echo ""
echo "Ссылка для Клода (по ней он читает свежие котировки):"
echo "https://raw.githubusercontent.com/$OWNER/$NAME/main/data/latest.json"
echo ""
echo "Дальше: скопируйте portfolio.example.json в portfolio.json, впишите сделки,"
echo "и откройте панель: python3 -m http.server 8080 в этой папке."
read -n1 -r -p "Нажмите любую клавишу, чтобы закрыть..."
