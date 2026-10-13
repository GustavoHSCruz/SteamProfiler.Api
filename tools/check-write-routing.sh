#!/usr/bin/env bash
# Stands the real nginx.conf up and checks which path the api is handed for
# the three writes a visitor can make: a message, a vote on it, a vote on a
# post.
#
# This exists because `nginx -t` called the config valid while every one of
# them reached the api as `POST /`. The location captured the route as $1 and
# built proxy_pass from it, and by the time proxy_pass was built the capture
# was gone: limit_conn evaluates $sp_conn_key, that map matches with a regex of
# its own, and a regex that runs after the location matched resets the numbered
# captures. The api saw a path it does not serve, fell through to the admin
# routes and answered 401, so the vote button on a post said "wrong token" to
# a reader who had never been asked for one. Found by a 401 in a browser.
#
# Two containers, the same arrangement as check-dict-routing.sh: a stub named
# `api` that passes the gate and otherwise answers with the request line it was
# given, and nginx itself in front of it. Wants docker; skips without it.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  echo "skip  write routing (no docker here)"
  exit 0
fi

WORK="$(mktemp -d)"
NET="sp-write-$$"
WEB="sp-write-web-$$"
API="sp-write-api-$$"
cleanup() {
  docker rm -f "$WEB" "$API" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/site" "$WORK/next" "$WORK/proxy-trust"
printf 'const DICT_LANG = %s;\n' "'en'" > "$WORK/site/dict.en.js"
cat > "$WORK/api.conf" <<'NGINX'
server {
    listen 8000;
    location = /gate { return 204; }
    location / { return 200 "$request_method $request_uri\n"; }
}
NGINX

docker network create "$NET" >/dev/null || { echo "FAIL  write routing: no network"; exit 1; }
docker run -d --name "$API" --network "$NET" --network-alias api \
  -v "$WORK/api.conf:/etc/nginx/conf.d/default.conf:ro" nginx:alpine >/dev/null \
  || { echo "FAIL  write routing: the stub api did not start"; exit 1; }
docker run -d --name "$WEB" --network "$NET" \
  -v "$ROOT/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$WORK/proxy-trust:/etc/nginx/proxy-trust:ro" \
  -v "$WORK/site:/usr/share/nginx/html:ro" \
  -v "$WORK/next:/usr/share/nginx/next:ro" \
  -p 127.0.0.1:0:80 nginx:alpine >/dev/null \
  || { echo "FAIL  write routing: nginx did not start"; exit 1; }

PORT="$(docker port "$WEB" 80/tcp | head -1 | sed 's/.*://')"
ready=""
for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/dict.en.js" 2>/dev/null; then ready=1; break; fi
  sleep 0.3
done
if [ -z "$ready" ]; then
  echo "FAIL  write routing: nginx never answered"
  docker logs "$WEB" 2>&1 | tail -5
  exit 1
fi

fails=0
sent() { # <public path> <path the api must see>
  local got
  got="$(curl -sS -X POST -H 'Content-Type: application/json' -d '{}' \
         "http://127.0.0.1:$PORT$1" 2>/dev/null)"
  if [ "$got" != "POST $2" ]; then
    echo "FAIL  $1 reached the api as '${got:-nothing}', wanted 'POST $2'"
    fails=$((fails + 1))
  fi
}

sent /api/feedback  /feedback
sent /api/vote      /vote
sent /api/blog/vote /blog/vote

[ "$fails" -ne 0 ] && exit 1
echo "the three writes reach the api under their own paths"
