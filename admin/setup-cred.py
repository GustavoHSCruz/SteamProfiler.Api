#!/usr/bin/env python3
"""Cria (ou reseta) a credencial do admin local.

Escreve só o hash. A senha em texto aparece uma vez na saída e não fica em lugar
nenhum depois disso: se perder, roda de novo e gera outra, que é mais seguro que
qualquer jeito de "recuperar" a antiga.

    python3 admin/setup-cred.py                 # gera senha aleatória
    python3 admin/setup-cred.py --user owner    # escolhe o nome
    python3 admin/setup-cred.py --stdout        # só imprime o JSON, não grava

O `--stdout` existe porque o arquivo mora no servidor, dentro de `data/`, que
fica de fora do git e do rsync de propósito.
"""

import argparse
import json
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import CRED_PATH, hash_password  # noqa: E402

# Sem caracteres que se confundem lidos de uma tela: O/0, l/1/I.
ALFABETO = "".join(c for c in string.ascii_letters + string.digits if c not in "Ol01lI")


def gerar_senha(n=20):
    return "".join(secrets.choice(ALFABETO) for _ in range(n))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user", default="owner")
    p.add_argument("--password", help="se omitido, gera uma aleatória")
    p.add_argument("--stdout", action="store_true", help="imprime o JSON em vez de gravar")
    a = p.parse_args()

    senha = a.password or gerar_senha()
    cred = {"user": a.user, **hash_password(senha), "must_change": True}

    if a.stdout:
        print(json.dumps(cred, indent=2))
        print(f"\nusuário: {a.user}\nsenha:   {senha}", file=sys.stderr)
        return

    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRED_PATH.write_text(json.dumps(cred, indent=2), encoding="utf-8")
    CRED_PATH.chmod(0o600)
    print(f"gravado em {CRED_PATH}")
    print(f"usuário: {a.user}")
    print(f"senha:   {senha}")
    print("\nA troca é obrigatória no primeiro login.")


if __name__ == "__main__":
    main()
