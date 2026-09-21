#!/usr/bin/env bash
# Liga e desliga a página de manutenção no servidor, sem deploy.
#
#   tools/maintenance.sh on      toda rota pública passa a responder 503
#   tools/maintenance.sh off     o site volta
#   tools/maintenance.sh status  diz em qual dos dois está
#
# A chave é o arquivo `data/maintenance` (ver o bloco "Manutenção" no
# nginx.conf). `data/` é do uid 10001 e o usuário do ssh não escreve nela, por
# isso o arquivo é criado de dentro de um container da api. `run --no-deps`
# funciona com a api de pé ou parada.
set -euo pipefail

REMOTO="${REMOTO:-server}"
DESTINO="${DESTINO:-steamprofiler}"

no_servidor() {
  ssh "$REMOTO" "cd ~/$DESTINO && docker compose run --rm --no-deps -T --entrypoint sh api -c '$1'"
}

case "${1:-status}" in
  on)     no_servidor 'touch /app/data/maintenance' ;;
  off)    no_servidor 'rm -f /app/data/maintenance' ;;
  status) ;;
  *)      echo "uso: $0 on|off|status" >&2; exit 2 ;;
esac

if no_servidor 'test -f /app/data/maintenance' 2>/dev/null; then
  echo "manutenção: ligada"
else
  echo "manutenção: desligada"
fi
