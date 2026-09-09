#!/usr/bin/env python3
"""Publica o steamprofiler quando o origin/main anda.

Roda como serviço de usuário (steamprofiler-deploy.service) e não faz nada além
de vigiar dois repositórios remotos e chamar o deploy.sh quando um deles mexe.
O gatilho é `git push`, não salvar arquivo: antes deste arquivo existir na forma
atual o watcher publicava três segundos depois de qualquer alteração no disco, e
trabalho pela metade tinha plateia.

Antes de publicar, espera o veredito do CI quando consegue lê-lo. O repo do
front é público, então a API do GitHub responde sem credencial; o da api é
privado e só responde com um token em ~/.config/steamprofiler/gh-token. Sem
token, o front espera o CI e a api segue com a suíte local - que o deploy.sh
roda contra o export de qualquer jeito, então nada sobe sem ter sido checado.

Recriado em 09/09/2026 junto com o deploy.sh, depois de os dois sumirem do
disco. Nunca tinham sido commitados, o que quer dizer que a esteira dependia de
dois arquivos sem cópia em lugar nenhum. Agora estão no git - e continuam fora
do rsync, porque editar o watcher não pode reiniciar a api em produção.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
REPOS = {
    "api": RAIZ / "steamprofiler-api",
    "front": RAIZ / "steamprofiler-front",
}
GITHUB = {
    "api": "GustavoHSCruz/SteamProfiler.Api",
    "front": "GustavoHSCruz/SteamProfiler.Front",
}
DEPLOY = RAIZ / "steamprofiler-api" / "deploy.sh"
TOKEN_FILE = Path.home() / ".config" / "steamprofiler" / "gh-token"

# De quanto em quanto tempo perguntar ao remoto onde está o main.
INTERVALO = 30
# Quanto esperar o CI antes de seguir sem ele. Um workflow que trava não pode
# segurar a publicação para sempre; a suíte local roda contra o export de todo
# jeito e é a mesma suíte.
CI_ESPERA = 600
# Depois de um deploy que não terminou bem.
RETENTA = 600


def log(*partes):
    agora = datetime.now().strftime("%H:%M:%S")
    print(agora, " ".join(str(p) for p in partes), flush=True)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])
    return r.stdout.strip()


def remoto(nome):
    """O sha que o origin/main tem agora, perguntado ao remoto."""
    saida = git(REPOS[nome], "ls-remote", "origin", "refs/heads/main")
    return saida.split()[0][:7] if saida else None


def token():
    try:
        valor = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return valor or None


def veredito(nome, sha, gh_token):
    """O que o CI achou desse commit: True, False, None (não sei) ou
    'rodando'."""
    url = (f"https://api.github.com/repos/{GITHUB[nome]}"
           f"/commits/{sha}/check-runs")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "steamprofiler-deploy",
        **({"Authorization": f"Bearer {gh_token}"} if gh_token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            corpo = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"  a API do GitHub respondeu {e.code}, seguindo sem o veredito")
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    runs = corpo.get("check_runs") or []
    if not runs:
        return None
    if any(r.get("status") != "completed" for r in runs):
        return "rodando"
    return all(r.get("conclusion") in ("success", "neutral", "skipped") for r in runs)


def espera_ci(nome, sha, gh_token):
    """True quando pode publicar. Só um `False` explícito do CI segura."""
    limite = time.monotonic() + CI_ESPERA
    avisou = False
    while True:
        v = veredito(nome, sha, gh_token)
        if v is None:
            log("  sem veredito do CI (repositório privado sem token, ou sem "
                "workflow neste commit); seguindo com a suite local")
            return True
        if v == "rodando":
            if not avisou:
                log("  CI ainda rodando, esperando")
                avisou = True
            if time.monotonic() > limite:
                log("  CI não concluiu a tempo; seguindo com a suite local")
                return True
            time.sleep(20)
            continue
        if v:
            log("  CI verde")
            return True
        log("  CI vermelho; nada publicado")
        return False


def publica():
    r = subprocess.run(["bash", str(DEPLOY)], capture_output=True, text=True)
    for linha in (r.stdout or "").splitlines():
        log("  " + linha)
    if r.returncode != 0:
        for linha in (r.stderr or "").splitlines()[-5:]:
            log("  " + linha)
    return r.returncode == 0


def main():
    if not DEPLOY.exists():
        log(f"deploy.sh não existe em {DEPLOY}")
        return 1
    gh = token()
    log(f"observando o origin/main de api e front, a cada {INTERVALO}s")
    log("token do GitHub:", "presente, os dois esperam o CI" if gh
        else "ausente, só o front espera o CI")

    publicado = {}
    for nome in REPOS:
        try:
            publicado[nome] = remoto(nome)
        except Exception as e:  # noqa: BLE001 - um watcher não pode morrer
            log(f"{nome}: {e}")
            publicado[nome] = None
    log("último publicado:",
        ", ".join(f"{n}={s}" for n, s in publicado.items()))

    proxima = 0.0
    while True:
        time.sleep(INTERVALO)
        if time.monotonic() < proxima:
            continue
        mexeu = []
        for nome in REPOS:
            try:
                agora = remoto(nome)
            except Exception as e:  # noqa: BLE001
                log(f"{nome}: {e}")
                continue
            if agora and agora != publicado.get(nome):
                log(f"{nome}: origin/main agora é {agora}")
                mexeu.append((nome, agora))
        if not mexeu:
            continue

        # O veredito é por repositório, mas o deploy é um só: ele exporta os
        # dois de qualquer forma, então basta um vermelho para segurar tudo.
        if not all(espera_ci(nome, sha, gh) for nome, sha in mexeu):
            proxima = time.monotonic() + RETENTA
            continue

        log("publicando")
        if publica():
            for nome, sha in mexeu:
                publicado[nome] = sha
            proxima = 0.0
        else:
            log(f"deploy não terminou bem; próxima tentativa em {RETENTA}s, "
                "ou assim que o origin/main andar")
            proxima = time.monotonic() + RETENTA


if __name__ == "__main__":
    sys.exit(main() or 0)
