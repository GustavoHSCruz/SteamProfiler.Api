#!/usr/bin/env bash
# Everything that has to be true before this tree is allowed near the server.
#
# The front has the same contract: one entry point called by the pre-push hook,
# CI, and a person preparing a release.
#
#   ./check.sh            check this working tree
#   ./check.sh /tmp/x     check an exported copy
#
# The nginx check is the one worth explaining. This config is mounted into the
# web container, and a syntax error in it does not degrade the site, it takes
# the site down: `docker restart steamprofiler-site` on a bad config leaves no
# container running at all. So the config is tested here before release in the
# same nginx image that will run it.
#
# `--add-host api:127.0.0.1` is not a workaround to be tidied away: nginx
# resolves the names in `upstream` when it starts, `api` is a name that only
# exists on the compose network, and a bare container has no such network. The
# address is never connected to. It exists so the name resolves and the parser
# gets to the end of the file.
set -uo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ROOT" || { echo "check: $ROOT is not a directory"; exit 1; }

FAILED=0
step() {
  local label="$1"; shift
  local out
  if out=$("$@" 2>&1); then
    printf '  ok    %s\n' "$label"
    [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/          /'
  else
    printf '  FAIL  %s\n' "$label"
    printf '%s\n' "$out" | sed 's/^/          /'
    FAILED=1
  fi
}

# Igual, mas cala a boca quando passa. Pro nginx, que escreve dez linhas de
# entrypoint antes de dizer que a config está boa, e cujas dez linhas são ruído
# em toda execução que der certo. Quando falha, sai tudo.
quiet() {
  local label="$1"; shift
  local out
  if out=$("$@" 2>&1); then
    printf '  ok    %s\n' "$label"
  else
    printf '  FAIL  %s\n' "$label"
    printf '%s\n' "$out" | sed 's/^/          /'
    FAILED=1
  fi
}

echo "checking $ROOT"

# ── Does it parse ────────────────────────────────────────────────────
while IFS= read -r f; do
  step "py_compile ${f#./}" python3 -m py_compile "$f"
done < <(find . -name '*.py' -not -path './.git/*' -not -path './__pycache__/*' \
         -not -path './data/*' | sort)

while IFS= read -r f; do
  step "node --check ${f#./}" node --check "$f"
done < <(find . -name '*.js' -not -path './.git/*' -not -path './data/*' | sort)

step "search catalogue tests" python3 -m unittest discover -s tests -p 'test_*.py'
step "release tree has no obvious secrets" python3 tools/audit-release.py

# ── Would it start ───────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
  quiet "nginx.conf is valid" \
    docker run --rm --add-host api:127.0.0.1 \
      -v "$ROOT/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
      nginx:alpine nginx -t
  step "dict routing" ./tools/check-dict-routing.sh

  # `env_file: .env` makes compose refuse to parse the file at all when .env is
  # missing, and .env is gitignored - so it is missing in every clean checkout,
  # which is to say in CI and in the tree the deploy exports. Validating the
  # structure does not need the secrets in it, only that the path resolves, so
  # an empty one is stood up for the length of the check and taken away after.
  # Never over a real one: that file holds the Steam key.
  if [ -f .env ]; then
    step "docker-compose.yml is valid" docker compose config -q
  else
    : > .env
    step "docker-compose.yml is valid (with an empty .env)" docker compose config -q
    rm -f .env
  fi
else
  echo "  skip  nginx.conf and docker-compose.yml (no docker here)"
fi

# ── Is anything undeclared ───────────────────────────────────────────
# Every variable the code reads should be in .env.example, because that file is
# the only description of what this service needs to run. A variable added to
# the code and not to the example is a service that works on this machine and
# fails on a fresh one, with no clue as to which line is missing.
step "every env var is in .env.example" python3 - <<'PY'
import pathlib, re, sys
root = pathlib.Path('.')
used = set()
for f in root.rglob('*.py'):
    if any(p in f.parts for p in ('.git', '__pycache__', 'data')):
        continue
    for m in re.finditer(r'environ(?:\.get)?\(\s*["\']([A-Z][A-Z0-9_]*)["\']', f.read_text()):
        used.add(m.group(1))
declared = set(re.findall(r'^([A-Z][A-Z0-9_]*)=',
                          pathlib.Path('.env.example').read_text(), re.M))
# Set by docker-compose.yml rather than by the operator, so the example has no
# business listing them.
from_compose = {'PORT', 'DATA_DIR', 'PYTHONUNBUFFERED', 'API_URL',
                'ADMIN_ALLOW_IPS', 'OLLAMA_URL', 'OLLAMA_MODEL',
                'OLLAMA_SLUG_MODEL', 'PUBLIC_SITE_URL'}
missing = sorted(used - declared - from_compose)
if missing:
    print('not in .env.example: ' + ', '.join(missing))
    sys.exit(1)
print(f'{len(used)} read, all accounted for')
PY

echo
if [ "$FAILED" -eq 0 ]; then
  echo "check: everything passed"
else
  echo "check: FAILED"
fi
exit "$FAILED"
