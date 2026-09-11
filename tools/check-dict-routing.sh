#!/usr/bin/env bash
# Stands the real nginx.conf up and asks it for /dict.js six ways.
#
# This exists because `nginx -t` called the config valid while it was serving
# every reader the same language. $cookie_sp_lang looks for a cookie named
# `sp_lang`: nginx replaces dashes with underscores for header variables and
# does nothing of the sort for cookie ones, so the variable was empty forever,
# the Accept-Language guess decided everything, and a reader who picked Russian
# got Portuguese twice in a row. None of that is a syntax error, and nothing in
# this repository would have caught it - it was found by curling production.
#
# Two containers, because this config gates every document on the api: a stub
# answering 204 at /gate, named `api` on the network so the upstream resolves,
# and nginx itself over a throwaway site directory holding one-line
# dictionaries. Wants docker; skips without it, like the rest of the checks.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  echo "skip  dict routing (no docker here)"
  exit 0
fi

WORK="$(mktemp -d)"
NET="sp-dict-$$"
WEB="sp-dict-web-$$"
GATE="sp-dict-gate-$$"
cleanup() {
  docker rm -f "$WEB" "$GATE" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/site"
for lang in en pt ru; do
  printf 'const DICT_LANG = %s;\n' "'$lang'" > "$WORK/site/dict.$lang.js"
done
# The gate says "not banned" to everything. auth_request treats 204 as a pass.
printf 'server { listen 8000; location / { return 204; } }\n' > "$WORK/gate.conf"

docker network create "$NET" >/dev/null || { echo "FAIL  dict routing: no network"; exit 1; }
docker run -d --name "$GATE" --network "$NET" --network-alias api \
  -v "$WORK/gate.conf:/etc/nginx/conf.d/default.conf:ro" nginx:alpine >/dev/null \
  || { echo "FAIL  dict routing: the stub gate did not start"; exit 1; }
docker run -d --name "$WEB" --network "$NET" \
  -v "$ROOT/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$WORK/site:/usr/share/nginx/html:ro" \
  -p 127.0.0.1:0:80 nginx:alpine >/dev/null \
  || { echo "FAIL  dict routing: nginx did not start"; exit 1; }

PORT="$(docker port "$WEB" 80/tcp | head -1 | sed 's/.*://')"
ready=""
for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/dict.en.js" 2>/dev/null; then ready=1; break; fi
  sleep 0.3
done
if [ -z "$ready" ]; then
  echo "FAIL  dict routing: nginx never answered"
  docker logs "$WEB" 2>&1 | tail -5
  exit 1
fi

fails=0
ask() { # <expected> <label> [curl args...]
  local want="$1" label="$2"; shift 2
  local got
  got="$(curl -fsS "$@" "http://127.0.0.1:$PORT/dict.js" 2>/dev/null \
         | sed -n "s/.*DICT_LANG = '\([a-z]*\)'.*/\1/p")"
  if [ "$got" != "$want" ]; then
    echo "FAIL  $label: wanted $want, got ${got:-nothing}"
    fails=$((fails + 1))
  fi
}

# The cookie decides, even against a browser asking for something else.
ask ru "a cookie is obeyed" -H 'Cookie: sp-lang=ru' -H 'Accept-Language: pt-BR,pt;q=0.9'
ask pt "a cookie among others is obeyed" -H 'Cookie: theme=dark; sp-lang=pt; seen=1'
# No cookie: the browser's own preference, which is the guess that keeps a
# first visit from costing a reload.
ask pt "Accept-Language decides a first visit" -H 'Accept-Language: pt-BR,pt;q=0.9,en;q=0.8'
ask ru "Accept-Language in Russian" -H 'Accept-Language: ru-RU,ru;q=0.9'
# Nothing to go on, and nonsense, both land in English.
ask en "no cookie and no header is English"
ask en "an unknown language is English" -H 'Cookie: sp-lang=xx' -H 'Accept-Language: xx'

[ "$fails" -ne 0 ] && exit 1
echo "dict routing ok: cookie beats Accept-Language, and both beat nothing"
