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
#   ./deploy.sh --dry-run  exporta, roda a suíte e mostra o que o rsync faria,
#                          sem escrever nada no servidor e sem reiniciar nada.
#                          É como se ensaia uma mudança neste arquivo sem
#                          usar a produção de cobaia.
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
ENSAIO=0
for arg in "$@"; do
  case "$arg" in
    --local)   LOCAL=1 ;;
    --dry-run) ENSAIO=1 ;;
  esac
done
SECO=()
[ "$ENSAIO" -eq 1 ] && SECO=(--dry-run)

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
morre() { log "$*"; exit 1; }

TMP="$(mktemp -d)"
WORKTREES=()
trap 'limpa_worktrees; rm -rf "$TMP"' EXIT

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
  # worktree e não `git archive`: a suíte tem checks que perguntam ao git o
  # que está versionado - o audit-release.py roda `git ls-files` para saber
  # quais arquivos vão de fato sair do repositório - e um diretório extraído
  # de um tar não tem .git nenhum para responder. Com archive o deploy morria
  # em "returned non-zero exit status 128" no meio de uma checagem que estava
  # certa; o errado era o export.
  #
  # `--detach` porque isto é uma foto de um commit e não um branch em que
  # alguém vai trabalhar, e `--force` porque uma worktree deixada para trás
  # por um deploy que morreu não pode travar o próximo.
  rmdir "$destino" 2>/dev/null
  git -C "$repo" worktree add --detach --force --quiet "$destino" origin/main \
    || morre "$nome: worktree falhou"
  WORKTREES+=("$repo|$destino")
}

# Toda worktree criada acima sai no fim, dê certo ou não. Sem isto o repo
# acumula referências a diretórios em /tmp que já não existem.
limpa_worktrees() {
  local par repo caminho
  for par in "${WORKTREES[@]:-}"; do
    [ -z "$par" ] && continue
    repo="${par%%|*}"; caminho="${par##*|}"
    git -C "$repo" worktree remove --force "$caminho" >/dev/null 2>&1
    git -C "$repo" worktree prune >/dev/null 2>&1
  done
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
# ── O front novo, compilado antes da suíte ───────────────────────────
# next/ tem etapa de build, o que o site/ nunca teve, e a ordem importa: a
# suíte do front roda o typecheck e confere que a pré-renderização escreveu um
# arquivo por página e por idioma, e ela não tem o que conferir se as
# dependências não estiverem instaladas. `npm ci` e não `npm install` porque o
# que sobe tem que ser o package-lock.json do commit.
#
# Isso põe o registry do npm no caminho de todo deploy, que é o preço da
# escolha de framework e está escrito no README do front. Se o build falhar, o
# deploy morre aqui e o servidor continua servindo o que já estava lá.
if [ -f "$TMP/front/next/package.json" ]; then
  log "front: instalando e compilando next/"
  ( cd "$TMP/front/next" && npm ci --silent && npm run --silent build ) > "$TMP/next.log" 2>&1 \
    || { sed 's/^/          /' "$TMP/next.log"; morre "front: o build do next/ falhou"; }
  log "front: next/ compilado"
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
#   site/, next/       os dois fronts, que são de outro repo e têm viagem própria
#   ollama-bridge/     o bridge.conf da ponte nginx que alcança o tradutor
#                      local. Um arquivo, só no servidor, desde 30/07/2026 -
#                      e apagá-lo derruba a tradução do blog em silêncio,
#                      porque o nginx segue de pé servindo o resto
#   deploy.sh/watch.py editar o watcher não pode reiniciar a api em produção
#
# Cada um desses foi visto num --dry-run antes de este arquivo existir na
# forma atual: os três últimos apareceram como "deleting" na primeira versão
# da lista, que só tinha os óbvios.
# `next/` entrou nesta lista em 11/09/2026 e a ausência dela é o motivo pelo
# qual produção serviu a home antiga depois de três deploys "com sucesso":
# esta viagem tem --delete na raiz do projeto, o next/ não mora no repo da api,
# então ela o apagava; a viagem de baixo o recriava com inode novo, e o
# container do nginx seguia montado na pasta apagada, vazia. O nginx caía no
# fallback e ninguém via erro nenhum.
API_SAIU="$(rsync -azc --delete "${SECO[@]}" --out-format='%n' \
  --exclude '.env' --exclude '.env.bak-*' --exclude 'data/' \
  --exclude '__pycache__/' --exclude '.git' --exclude 'site/' --exclude 'next/' \
  --exclude 'ollama-bridge/' --exclude 'deploy.sh' --exclude 'watch.py' \
  "$TMP/api/" "$REMOTO:$DESTINO/")" || morre "api: rsync falhou"

FRONT_SAIU="$(rsync -azc --delete "${SECO[@]}" --out-format='%n' --exclude '.git' \
  "$TMP/front/site/" "$REMOTO:$DESTINO/site/")" || morre "front: rsync falhou"

# O front novo, só o que o build escreveu. dist/server/ fica de fora: é o
# bundle que a pré-renderização usou na máquina que compilou e não tem nada
# que fazer no servidor.
NEXT_SAIU=""
if [ -d "$TMP/front/next/dist" ]; then
  NEXT_SAIU="$(rsync -azc --delete "${SECO[@]}" --out-format='%n' \
    --exclude 'server/' \
    "$TMP/front/next/dist/" "$REMOTO:$DESTINO/next/")" || morre "next: rsync falhou"
fi

lista() {
  local nome="$1" saiu="$2"
  local arquivos
  arquivos="$(printf '%s\n' "$saiu" | grep -v '/$' | grep -v '^$' | tr '\n' ' ')"
  [ -n "${arquivos// }" ] && log "$nome: $arquivos"
}
lista api "$API_SAIU"
lista front "$FRONT_SAIU"
[ -n "$NEXT_SAIU" ] && lista next "$NEXT_SAIU"

# ── Só o restart que o tipo de arquivo exige ─────────────────────────
# Estático vale na hora porque o site/, o next/ e o ./ são bind mounts de
# pasta. O nginx.conf não: é mount de arquivo único, e `rsync` escreve um
# inode novo, então o container segue servindo a config velha e o `nginx -t`
# valida a velha sem reclamar de nada.
#
# E aqui moravam três bugs que custaram uma tarde em 11/09/2026:
#
#   1. os nomes estavam errados. `steamprofiler-site` não existe - o compose
#      cria `steamprofiler-web-1`. O docker respondia "No such container",
#      o 2>/dev/null engolia, e o deploy dizia que tinha reiniciado.
#   2. `docker restart` não re-liga o inode. Mesmo com o nome certo, o mount
#      de arquivo continua apontando para o arquivo antigo: só recriar o
#      container pega a config nova.
#   3. o healthz era pedido em 127.0.0.1:16200, e a porta é publicada em
#      192.168.0.20. A espera falhava sempre e ninguém via.
#
# Agora é `docker compose up -d --force-recreate` pelo serviço, rodando na
# pasta do projeto no servidor, e o erro aparece.
NEEDS_API=0; NEEDS_WEB=0; NEEDS_ADMIN=0
while IFS= read -r f; do
  case "$f" in
    admin/*)            NEEDS_ADMIN=1 ;;
    nginx.conf)         NEEDS_WEB=1 ;;
    *.py)               NEEDS_API=1 ;;
  esac
done <<< "$API_SAIU"

# O next/ também pede o web recriado. Em 11/09/2026 um deploy trocou a pasta no
# host e o container seguiu com o mount apontando para a anterior, vazia: o
# nginx caiu no fallback e produção serviu a home antiga com tudo dizendo que
# estava certo. Recriar custa cinco segundos; servir a versão errada custou
# quatro pedidos do dono.
[ -n "$NEXT_SAIU" ] && NEEDS_WEB=1

if [ "$ENSAIO" -eq 1 ]; then
  log "ensaio: nada foi escrito no servidor e nada foi reiniciado"
  log "ensaio: reiniciaria api=$NEEDS_API web=$NEEDS_WEB admin=$NEEDS_ADMIN"
  exit 0
fi

recria() { # <serviço> <motivo>
  log "$2, recriando $1"
  ssh "$REMOTO" "cd $DESTINO && docker compose up -d --force-recreate $1" 2>&1 \
    | sed 's/^/          /' \
    || morre "$1: não subiu"
}

[ "$NEEDS_API" -eq 1 ]   && recria api   "python mudou"
[ "$NEEDS_WEB" -eq 1 ]   && recria web   "nginx.conf mudou"
[ "$NEEDS_ADMIN" -eq 1 ] && recria admin "admin mudou"

if [ "$NEEDS_API" -eq 1 ] || [ "$NEEDS_WEB" -eq 1 ]; then
  # Espera de verdade. A versão anterior perguntava uma vez logo depois do
  # restart e avisava que o healthz não voltou 200 - o container levava uns
  # quarenta segundos para subir e o aviso era sempre falso alarme, o que é
  # pior que não avisar: um alerta que sempre grita deixa de ser lido.
  for _ in $(seq 1 30); do
    sleep 3
    # De dentro do container, e não pelo endereço publicado: o bind da porta é
    # configuração do servidor (HTTP_BIND) e já foi 127.0.0.1 e já foi
    # 192.168.0.20. Perguntar por dentro não depende de acertar qual é hoje.
    if ssh "$REMOTO" "cd $DESTINO && docker compose exec -T web wget -qO- --timeout=5 http://127.0.0.1/healthz" >/dev/null 2>&1; then
      log "no ar"
      exit 0
    fi
  done
  log "ATENÇÃO: healthz não voltou 200 em 90s depois do restart"
  exit 1
fi

log "no ar (só arquivos estáticos, sem restart)"
