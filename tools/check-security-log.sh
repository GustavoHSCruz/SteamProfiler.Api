#!/usr/bin/env bash
# Exercise completed request logging through the real nginx and UDP receiver.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
NET="sp-security-$$"
WEB="sp-security-web-$$"
GATE="sp-security-api-$$"
cleanup() {
  if [ "$?" -ne 0 ]; then docker logs "$WEB" 2>&1 | tail -12; fi
  docker rm -f "$WEB" "$GATE" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/site" "$WORK/data"
mkdir -p "$WORK/data/art"
printf 'test art\n' > "$WORK/data/art/440.jpg"
printf 'body {}\n' > "$WORK/site/style.css"
printf '<svg xmlns="http://www.w3.org/2000/svg"/>\n' > "$WORK/site/favicon.svg"
printf 'const DICT_LANG = "en";\n' > "$WORK/site/dict.en.js"
printf '<!doctype html><title>normal</title>\n' > "$WORK/site/about.html"
printf '<!doctype html><title>game</title><!--#include virtual="/api/game/public/meta?appid=$appid" -->\n' > "$WORK/site/game-public.html"
printf '<!doctype html><title>blocked</title>\n' > "$WORK/site/banned.html"
docker network create "$NET" >/dev/null
docker run -d --name "$GATE" --network "$NET" --network-alias api \
  -e DATA_DIR=/data -e IP_SALT=integration-test-salt -e CENSUS_SEED=integration-test-seed \
  -v "$ROOT/security_log.py:/app/security_log.py:ro" \
  -v "$ROOT/census.py:/app/census.py:ro" -v "$ROOT/store.py:/app/store.py:ro" \
  -v "$ROOT/tools/security-log-fixture.py:/app/fixture.py:ro" \
  -v "$WORK/data:/data" -w /app python:3.13-alpine python fixture.py >/dev/null
# Bring the UDP receiver up before nginx opens its syslog sockets. Otherwise
# early connection-refused ICMP errors can discard a later worker's datagram.
docker exec -i "$GATE" python - <<'PY'
import time
import urllib.request
for _ in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/__security_test__', timeout=1):
            break
    except OSError:
        time.sleep(.2)
else:
    raise SystemExit('FAIL: the security receiver never became ready')
PY
docker run -d --name "$WEB" --network "$NET" \
  -v "$ROOT/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$WORK/site:/usr/share/nginx/html:ro" \
  -v "$WORK/data:/srv:ro" \
  -p 127.0.0.1:0:80 nginx:alpine >/dev/null
PORT="$(docker port "$WEB" 80/tcp | head -1 | sed 's/.*://')"
ready=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/dict.en.js" 2>/dev/null; then ready=1; break; fi
  sleep 0.2
done
if [ "$ready" -ne 1 ]; then docker logs "$GATE"; docker logs "$WEB"; exit 1; fi
curl -fsS -o /dev/null -A 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36' "http://127.0.0.1:$PORT/about"
curl -sS -o /dev/null -A 'Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)' "http://127.0.0.1:$PORT/g/70"
curl -fsS -o /dev/null -A 'Googlebot/2.1' "http://127.0.0.1:$PORT/g/440"
curl -fsS -o /dev/null -A 'GPTBot/1.0' "http://127.0.0.1:$PORT/g/570"
curl -fsS -o /dev/null -A 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36' "http://127.0.0.1:$PORT/g/10"
for path in style.css favicon.svg art/440.jpg healthz; do
  curl -fsS -o /dev/null -A 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36' "http://127.0.0.1:$PORT/$path"
done
curl -sS -o /dev/null -A 'curl/8.0' -H 'CF-IPCountry: BR' \
  --data 'password=body-secret' "http://127.0.0.1:$PORT/api/probe?token=query-secret&appid=440"
# The trap forces GET upstream and redirects to a static error page. The
# security event must still describe the client's original POST and final 403.
curl -sS -o /dev/null -A 'Mozilla/5.0' --data 'password=body-secret' "http://127.0.0.1:$PORT/.env"
curl -sS -o /dev/null -A 'Mozilla/5.0' "http://127.0.0.1:$PORT/api/execution-test?cmd=system%28id%29"
docker exec -i "$GATE" python - <<'PY'
import json
import time
import urllib.request
for attempt in range(30):
    with urllib.request.urlopen('http://127.0.0.1:8000/__security_test__') as response:
        report = json.load(response)
    probes = [r for r in report['items'] if r['uri'].startswith('/api/probe')]
    traps = [r for r in report['items'] if r['uri'] == '/.env']
    execution = [r for r in report['items'] if r['uri'].startswith('/api/execution-test')]
    games = {r['uri']: r for r in report['items'] if r['uri'] in ('/g/70', '/g/440', '/g/570', '/g/10')}
    browser_paths = {r['uri'] for r in report['items'] if r['ua'].startswith('Mozilla/5.0 Chrome/')}
    if probes and traps and execution and len(games) == 4 and {'/about', '/style.css', '/favicon.svg', '/art/440.jpg', '/healthz'} <= browser_paths:
        break
    time.sleep(.2)
else:
    raise SystemExit('FAIL: nginx events missing: ' + json.dumps({
        'items': [{k: r[k] for k in ('uri', 'status', 'method', 'signals')} for r in report['items']],
        'malformed': report['malformed'],
    }))
assert probes[0]['method'] == 'POST' and probes[0]['status'] == 400, probes
assert probes[0]['country'] == 'BR' and probes[0]['kind'] == 'tool', probes
assert traps[0]['method'] == 'POST' and traps[0]['status'] == 403, traps
assert 'secrets' in traps[0]['signals'], traps
assert execution[0]['status'] == 204 and 'execution' in execution[0]['signals'], execution
assert games['/g/70']['status'] == 403 and games['/g/70']['kind'] == 'tool', games
for uri, kind in (('/g/440', 'search'), ('/g/570', 'ai')):
    assert games[uri]['status'] == 200 and games[uri]['kind'] == kind, games
assert games['/g/10']['status'] == 200 and games['/g/10']['kind'] == 'unknown', games
assert not any(r['uri'] == '/_gate' for r in report['items']), 'Internal subrequests must not duplicate client requests'
assert 'query-secret' not in json.dumps(report) and 'body-secret' not in json.dumps(report)
assert report['malformed'] == 0, report['malformed']
print('security logging ok: all client requests, browser game pages, assets, cached art, icons, health checks, crawlers, final responses, original POST and sanitisation')
PY
docker exec -i "$GATE" python - "http://$WEB" <<'PY'
import json
import sys
import urllib.error
import urllib.request

def upstream_count():
    with urllib.request.urlopen('http://127.0.0.1:8000/__security_test__') as response:
        return json.load(response)['test_upstream_requests']

before = upstream_count()
for agent in ('SemrushBot/7~bl', 'sEmRuShBoT-BA/1.0', 'SemrushBot-SI/1.0',
              'SemrushBot-SWA/1.0', 'SemrushBot-OCOB/1.0', 'SemrushBot-FT/1.0',
              'SemrushBot-ESI/1.0', 'SiteAuditBot/1.0', 'SplitSignalBot/1.0', 'RyteBot/1.0'):
    for path in ('/g/440', '/api/probe', '/style.css', '/art/440.jpg', '/favicon.svg',
                 '/healthz', '/robots.txt', '/jogo/440', '/privacy/.env', '/appeal'):
        request = urllib.request.Request(sys.argv[1] + path, headers={'User-Agent': agent})
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, (agent, path, exc.code)
        else:
            raise AssertionError((agent, path, 'Semrush obtained access'))
for method in ('POST', 'HEAD', 'OPTIONS'):
    request = urllib.request.Request(sys.argv[1] + '/api/probe', method=method,
                                     headers={'User-Agent': 'SemrushBot/7~bl'})
    try:
        urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, (method, exc.code)
    else:
        raise AssertionError((method, 'Semrush obtained access'))
assert upstream_count() == before, 'Denied Semrush requests reached the upstream API'
print('Semrush denied: all crawler tokens, paths and HTTP methods; zero upstream requests')
PY
