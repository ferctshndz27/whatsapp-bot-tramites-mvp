#!/usr/bin/env python3
"""Genera un hash bcrypt compatible con el bot (mismo método que app/dashboard.py).

Uso:
    python scripts/hash_password.py            # pide la contraseña oculta y muestra el hash
    python scripts/hash_password.py --check '<hash>'   # verifica una contraseña contra un hash
"""
import getpass
import sys

import bcrypt


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        password = getpass.getpass("Contraseña a verificar: ")
        ok = bcrypt.checkpw(password.encode(), sys.argv[2].encode())
        print("✔ Coincide" if ok else "✘ NO coincide")
        sys.exit(0 if ok else 1)

    password = getpass.getpass("Contraseña: ")
    confirm = getpass.getpass("Confirmar:  ")
    if password != confirm:
        sys.exit("Las contraseñas no coinciden.")
    if not password:
        sys.exit("La contraseña no puede estar vacía.")
    print(bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())


if __name__ == "__main__":
    main()
