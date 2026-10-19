#!/usr/bin/env bash
# Перенос учёта с прода PDF-CONVERTER в базу KORT - схема `finance` целиком.
#
#   SOURCE_DATABASE_URL=postgresql://…/railway   # база PDF-CONVERTER
#   TARGET_DATABASE_URL=postgresql://…/railway   # база KORT
#   bash backend/scripts/copy_from_pdf_converter.sh
#
# Нужны pg_dump / pg_restore / psql той же старшей версии, что сервер (18).
#
# Что делает:
#   1. проверяет, что источник на той же ревизии, что последняя у KORT (0023):
#      схема старше или новее кода KORT - перенос откладывается;
#   2. отказывается писать в базу KORT, где уже есть учётки (FORCE=1 - снести);
#   3. снимает дамп одной схемы `finance` и разворачивает его вместо той, что
#      бэкенд KORT создал при первом старте (пустые таблицы из миграций);
#   4. ставит `alembic_version` = 0023 - схема уже на последней ревизии;
#   5. печатает счёт записей в источнике и в KORT рядом.
#
# Остальные схемы прода (public, bbc, books, webexcel) не трогаются и не
# переносятся. Источник только читается.
set -euo pipefail

KORT_HEAD="0023"

: "${SOURCE_DATABASE_URL:?задайте SOURCE_DATABASE_URL - база PDF-CONVERTER}"
: "${TARGET_DATABASE_URL:?задайте TARGET_DATABASE_URL - база KORT}"

# Адреса SQLAlchemy (postgresql+psycopg://) утилитам Postgres непонятны.
SRC="${SOURCE_DATABASE_URL/postgresql+psycopg:/postgresql:}"
DST="${TARGET_DATABASE_URL/postgresql+psycopg:/postgresql:}"
SRC="${SRC/postgres:\/\//postgresql://}"
DST="${DST/postgres:\/\//postgresql://}"

if [ "$SRC" = "$DST" ]; then
  echo "Источник и цель - одна и та же база. Отказ." >&2
  exit 1
fi

for tool in pg_dump pg_restore psql; do
  command -v "$tool" >/dev/null || { echo "Нет $tool в PATH." >&2; exit 1; }
done

q() { PGCLIENTENCODING=UTF8 psql "$1" -v ON_ERROR_STOP=1 -Atqc "$2"; }

src_rev="$(q "$SRC" "select version_num from alembic_version" || true)"
if [ "$src_rev" != "$KORT_HEAD" ]; then
  echo "Ревизия источника - «${src_rev:-нет}», у KORT последняя - $KORT_HEAD. Сначала выровнять." >&2
  exit 1
fi

has_users="$(q "$DST" "select count(*) from information_schema.tables where table_schema='finance' and table_name='users'")"
if [ "$has_users" = "1" ]; then
  users="$(q "$DST" "select count(*) from finance.users")"
  if [ "$users" != "0" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "В базе KORT уже $users учёток. Перезаписать - FORCE=1." >&2
    exit 1
  fi
fi

dump="$(mktemp -t kort-finance-XXXXXX.dump)"
trap 'rm -f "$dump"' EXIT

echo "→ дамп схемы finance из PDF-CONVERTER"
pg_dump "$SRC" --schema=finance --no-owner --no-privileges -Fc -f "$dump"

echo "→ замена схемы finance в KORT"
q "$DST" "drop schema if exists finance cascade"
pg_restore --no-owner --no-privileges -d "$DST" "$dump"

echo "→ ревизия $KORT_HEAD"
q "$DST" "create table if not exists alembic_version (version_num varchar(32) not null primary key)"
q "$DST" "delete from alembic_version"
q "$DST" "insert into alembic_version (version_num) values ('$KORT_HEAD')"

echo
echo "таблица            PDF-CONVERTER   KORT"
for table in workspaces users employees accounts operations contracts; do
  a="$(q "$SRC" "select count(*) from finance.$table")"
  b="$(q "$DST" "select count(*) from finance.$table")"
  mark=""; [ "$a" = "$b" ] || mark="  ← расходится"
  printf "%-18s %13s %6s%s\n" "$table" "$a" "$b" "$mark"
done
echo
# Пул соединений бэкенда помнит подготовленные запросы к таблицам, которых
# больше нет (схема пересоздана) - перезапуск сбрасывает их.
echo "Готово. Перезапустите бэкенд KORT (Railway → Restart)."
