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
DENIED = ('SemrushBot/7~bl', 'sEmRuShBoT-BA/1.0', 'SemrushBot-SI/1.0',
          'SemrushBot-SWA/1.0', 'SemrushBot-OCOB/1.0', 'SemrushBot-FT/1.0',
          'SemrushBot-ESI/1.0', 'SiteAuditBot/1.0', 'SplitSignalBot/1.0', 'RyteBot/1.0',
          # The backlink and SEO indexes, in the shapes they actually arrive in.
          'Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)',
          'Mozilla/5.0 (compatible; MJ12bot/v1.4.8; http://mj12bot.com/)',
          'Mozilla/5.0 (compatible; DotBot/1.2; +https://opensiteexplorer.org/dotbot)',
          'rogerbot/1.0', 'Mozilla/5.0 (compatible; BLEXBot/1.0)',
          'Mozilla/5.0 (compatible; DataForSeoBot/1.0)', 'SerpstatBot/2.1',
          'Barkrowler/0.9', 'SEOkicks/1.0', 'MegaIndex.ru/2.0', 'linkdexbot/2.2',
          'Mozilla/5.0 (compatible; SISTRIX Crawler)', 'spbot/5.0',
          'Screaming Frog SEO Spider/19.0',
          # Sales intelligence and content resale.
          'ZoomInfoBot/1.0', 'Diffbot/0.1', 'omgili/0.5 +https://omgili.com',
          'Mozilla/5.0 (compatible; webzio-extended/1.0)',
          # Survey scanners.
          'Mozilla/5.0 (compatible; InternetMeasurement/1.0)',
          'Mozilla/5.0 (compatible; CensysInspect/1.1)', 'Expanse, a Palo Alto Networks company')
for agent in DENIED:
    for path in ('/g/440', '/api/probe', '/style.css', '/art/440.jpg', '/favicon.svg',
                 '/healthz', '/robots.txt', '/jogo/440', '/privacy/.env', '/appeal'):
        request = urllib.request.Request(sys.argv[1] + path, headers={'User-Agent': agent})
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, (agent, path, exc.code)
        else:
            raise AssertionError((agent, path, 'denied crawler obtained access'))
for method in ('POST', 'HEAD', 'OPTIONS'):
    request = urllib.request.Request(sys.argv[1] + '/api/probe', method=method,
                                     headers={'User-Agent': 'SemrushBot/7~bl'})
    try:
        urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, (method, exc.code)
    else:
        raise AssertionError((method, 'Semrush obtained access'))
assert upstream_count() == before, 'Denied crawler requests reached the upstream API'
print(f'denied: {len(DENIED)} crawler tokens, all paths and HTTP methods; zero upstream requests')

# The other direction, which is the one a denylist gets wrong. A token added
# carelessly - `~*bot`, `~*spider` - refuses Googlebot and every assistant with
# somebody waiting on it, and nothing about the site looks broken from here:
# the readers simply stop arriving. So the agents that must get in are asserted
# by name, and this half is why the list above is safe to add to.
ALLOWED = (
    # Search engines. They send readers.
    'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
    'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)',
    'DuckDuckBot/1.1; (+http://duckduckgo.com/duckduckbot.html)',
    'Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)',
    'Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)',
    'Mozilla/5.0 (compatible; Applebot/0.1; +http://www.apple.com/go/applebot)',
    # Somebody asked their assistant about a page and is waiting for the answer.
    'Mozilla/5.0 (compatible; ChatGPT-User/1.0; +https://openai.com/bot)',
    'Mozilla/5.0 (compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot)',
    'Mozilla/5.0 (compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)',
    'Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)',
    'Mozilla/5.0 (compatible; Claude-User/1.0; +claudebot@anthropic.com)',
    'Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)',
    'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.4; +https://openai.com/gptbot)',
    # The unfurlers, which draw the card when somebody pastes a link.
    'Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)',
    'Twitterbot/1.0', 'facebookexternalhit/1.1', 'Slackbot-LinkExpanding 1.0',
    'TelegramBot (like TwitterBot)',
    # And a person.
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36',
)
for agent in ALLOWED:
    request = urllib.request.Request(sys.argv[1] + '/about.html', headers={'User-Agent': agent})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200, (agent, response.status)
    except urllib.error.HTTPError as exc:
        raise AssertionError((agent, exc.code, 'a wanted agent was refused'))
print(f'allowed: {len(ALLOWED)} search, assistant, unfurler and human agents still get in')

# Browsers below the front's build target: refused on every path, the health
# check and the home included, with the page that says why, and without the
# gate or the api ever hearing about it.
before = upstream_count()
OLD = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.169 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.5481.77 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36 Edge/18.19582',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:113.0) Gecko/20100101 Firefox/113.0',
    'Mozilla/5.0 (Windows NT 6.1; rv:52.0) Gecko/20100101 Firefox/52.0',
    'Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.1)',
    'Opera/9.80 (Windows NT 6.1) Presto/2.12.388 Version/12.18',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6.1 Safari/605.1.15',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Safari/605.1.15',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPad; CPU OS 12_5_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6099.119 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; U; Android 4.1.2; en-us; GT-I9100 Build/JZO54K) AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30',
)
for agent in OLD:
    for path in ('/', '/healthz', '/about.html', '/g/440', '/api/probe', '/style.css',
                 '/favicon.svg', '/art/440.jpg', '/robots.txt', '/_old_browser'):
        request = urllib.request.Request(sys.argv[1] + path, headers={'User-Agent': agent})
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            assert exc.code == 403, (agent, path, exc.code)
            assert 'Update your browser' in body, (agent, path, body[:200])
            assert exc.headers.get_content_type() == 'text/html', (agent, path, exc.headers)
        else:
            raise AssertionError((agent, path, 'an old browser obtained access'))
assert upstream_count() == before, 'Old browser requests reached the upstream API'

CURRENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0',
    'Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko/20100101 Firefox/115.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/140.0.0.0 Mobile/15E148 Safari/604.1',
    # Android WebView: Version/4.0, and Chrome after it.
    'Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/UQ1A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/140.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36',
    # Crawlers that name themselves on an old engine, in their full shapes.
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.1 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)',
    'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm) Chrome/100.0.4896.127 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.10; rv:38.0) Gecko/20100101 Firefox/38.0 (Discordbot)',
    # Not a browser at all: the container healthcheck and the deploy wait.
    'Wget', 'curl/8.0', 'Python-urllib/3.13',
)
for agent in CURRENT:
    for path in ('/about.html', '/healthz'):
        request = urllib.request.Request(sys.argv[1] + path, headers={'User-Agent': agent})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                assert response.status == 200, (agent, path, response.status)
        except urllib.error.HTTPError as exc:
            raise AssertionError((agent, path, exc.code, 'a current browser was refused'))
print(f'old browsers: {len(OLD)} refused on every path with zero upstream requests; '
      f'{len(CURRENT)} current browsers, named crawlers and tools still get in')
PY
