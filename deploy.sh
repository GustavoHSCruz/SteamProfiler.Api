#!/usr/bin/env bash
# Publica o steamprofiler no servidor.
#
# O que sobe é um commit, nunca a árvore de trabalho: `git archive` exporta o
# origin/main dos dois repos para um diretório temporário, a suíte roda contra
# esse export, e só então o rsync sai. Trabalho pela metade salvo no disco não
# tem plateia - foi por isso que a esteira deixou de publicar a cada save em
# 30/07/2026.
#
#   ./deploy.sh            publica o origin/main dos dois repos
#   ./deploy.sh --local    publica a árvore de trabalho, para o dia em que o
#                          GitHub cair. Ainda roda a suíte.
#
# Este arquivo e o watch.py ficam fora do rsync de propósito: editar o watcher
# não pode reiniciar a api em produção.
#
# Recriado em 09/09/2026 depois de sumirem do disco. Eles nunca tinham sido
# commitados - existiam só como arquivo local não rastreado, o que quer dizer
# que a esteira inteira dependia de dois arquivos que ninguém tinha cópia. Por
# isso agora estão no git.
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$RAIZ/steamprofiler-api"
FRONT="$RAIZ/steamprofiler-front"
REMOTO="server"
DESTINO="steamprofiler"

LOCAL=0
[ "${1:-}" = "--local" ] && LOCAL=1

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
morre() { log "$*"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── O que vai subir ──────────────────────────────────────────────────
# Exportado, não copiado da árvore: um arquivo sujo no disco não tem como
# entrar num export do origin/main, que é a única garantia que essa esteira
# oferece.
exporta() {
  local nome="$1" repo="$2" destino="$3"
  mkdir -p "$destino"
  if [ "$LOCAL" -eq 1 ]; then
    log "$nome: árvore de trabalho (--local)"
    rsync -a --exclude '.git/' --exclude '__pycache__/' --exclude 'data/' \
          --exclude 'site/' "$repo/" "$destino/" || morre "$nome: cópia local falhou"
    # O front é o único cujo site/ importa, e o exclude acima o tirou.
    [ "$nome" = "front" ] && rsync -a --exclude '.git/' "$repo/site/" "$destino/site/"
    return 0
  fi
  git -C "$repo" fetch --quiet origin main || morre "$nome: fetch falhou"
  local sha
  sha="$(git -C "$repo" rev-parse --short origin/main)" || morre "$nome: sem origin/main"
  log "$nome: origin/main em $sha"
  git -C "$repo" archive origin/main | tar -x -C "$destino" \
    || morre "$nome: git archive falhou"
}

exporta api "$API" "$TMP/api"
exporta front "$FRONT" "$TMP/front"

# ── A suíte, contra o que vai subir ──────────────────────────────────
# Contra o export e não contra a árvore, porque é o export que vira produção.
if [ -x "$TMP/api/check.sh" ]; then
  ( cd "$TMP/api" && bash check.sh "$TMP/api" ) > "$TMP/api.log" 2>&1 \
    || { sed 's/^/          /' "$TMP/api.log"; morre "api: checks falharam"; }
  log "api: checks ok"
fi
if [ -x "$TMP/front/tools/check.sh" ]; then
  ( cd "$TMP/front" && bash tools/check.sh "$TMP/front" ) > "$TMP/front.log" 2>&1 \
    || { sed 's/^/          /' "$TMP/front.log"; morre "front: checks falharam"; }
  log "front: checks ok"
fi

# ── Duas viagens ─────────────────────────────────────────────────────
# `--exclude 'site/'` na primeira é o que impede o --delete de apagar o front
# inteiro do servidor: o site/ do destino é do outro repo.
#
# `-c` e não só `-az`: o git archive carimba tudo com a data do commit, então
# sem checksum todo deploy via *.py como alterado e reiniciava a api à toa.
#
# A lista de exclude não é higiene, é a diferença entre código e estado. O
# --delete existe para que um arquivo removido do repo suma do servidor, e
# tudo que mora só lá dentro precisa estar nesta lista ou ele apaga:
#
#   .env, .env.bak-*   a configuração e os backups dela, que nunca estiveram
#                      no git e são o que faz a api subir
#   data/              os bancos: recados, bans, meta, houses
#   ollama-bridge/     o bridge.conf da ponte nginx que alcança o tradutor
#                      local. Um arquivo, só no servidor, desde 30/07/2026 -
#                      e apagá-lo derruba a tradução do blog em silêncio,
#                      porque o nginx segue de pé servindo o resto
#   deploy.sh/watch.py editar o watcher não pode reiniciar a api em produção
#
# Cada um desses foi visto num --dry-run antes de este arquivo existir na
# forma atual: os três últimos apareceram como "deleting" na primeira versão
# da lista, que só tinha os óbvios.
API_SAIU="$(rsync -azc --delete --out-format='%n' \
  --exclude '.env' --exclude '.env.bak-*' --exclude 'data/' \
  --exclude '__pycache__/' --exclude '.git/' --exclude 'site/' \
  --exclude 'ollama-bridge/' --exclude 'deploy.sh' --exclude 'watch.py' \
  "$TMP/api/" "$REMOTO:$DESTINO/")" || morre "api: rsync falhou"

FRONT_SAIU="$(rsync -azc --delete --out-format='%n' --exclude '.git/' \
  "$TMP/front/site/" "$REMOTO:$DESTINO/site/")" || morre "front: rsync falhou"

lista() {
  local nome="$1" saiu="$2"
  local arquivos
  arquivos="$(printf '%s\n' "$saiu" | grep -v '/$' | grep -v '^$' | tr '\n' ' ')"
  [ -n "${arquivos// }" ] && log "$nome: $arquivos"
}
lista api "$API_SAIU"
lista front "$FRONT_SAIU"

# ── Só o restart que o tipo de arquivo exige ─────────────────────────
# Estático vale na hora porque o site/ e o ./ são bind mounts. O resto não:
# o nginx.conf é mount de arquivo único e o rsync troca o inode, então o
# container continua servindo a config velha e o `nginx -t` valida a velha
# sem reclamar. Só o restart pega a nova.
NEEDS_API=0; NEEDS_WEB=0; NEEDS_ADMIN=0
while IFS= read -r f; do
  case "$f" in
    admin/*)            NEEDS_ADMIN=1 ;;
    nginx.conf)         NEEDS_WEB=1 ;;
    *.py)               NEEDS_API=1 ;;
  esac
done <<< "$API_SAIU"

[ "$NEEDS_API" -eq 1 ]   && { log "python mudou, reiniciando a api";     ssh "$REMOTO" 'docker restart steamprofiler-api'   >/dev/null 2>&1; }
[ "$NEEDS_WEB" -eq 1 ]   && { log "nginx.conf mudou, reiniciando o web"; ssh "$REMOTO" 'docker restart steamprofiler-site'  >/dev/null 2>&1; }
[ "$NEEDS_ADMIN" -eq 1 ] && { log "admin mudou, reiniciando o painel";   ssh "$REMOTO" 'docker restart steamprofiler-admin' >/dev/null 2>&1; }

if [ "$NEEDS_API" -eq 1 ] || [ "$NEEDS_WEB" -eq 1 ]; then
  # Espera de verdade. A versão anterior perguntava uma vez logo depois do
  # restart e avisava que o healthz não voltou 200 - o container levava uns
  # quarenta segundos para subir e o aviso era sempre falso alarme, o que é
  # pior que não avisar: um alerta que sempre grita deixa de ser lido.
  for _ in $(seq 1 30); do
    sleep 3
    if ssh "$REMOTO" 'curl -fsS --max-time 5 http://127.0.0.1:16200/healthz' >/dev/null 2>&1; then
      log "no ar"
      exit 0
    fi
  done
  log "ATENÇÃO: healthz não voltou 200 em 90s depois do restart"
  exit 1
fi

log "no ar (só arquivos estáticos, sem restart)"
