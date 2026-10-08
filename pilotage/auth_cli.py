"""Gestion locale des comptes du pilotage ; mots de passe saisis sans echo."""
import argparse
import getpass
import json
import os
import sys

from .auth import AuthStore, AuthStorageError, AuthConflict, AuthDenied


def password():
    if not sys.stdin.isatty():
        raise ValueError("Saisir le mot de passe dans un terminal interactif.")
    first = getpass.getpass("Mot de passe (15 caracteres minimum) : ")
    second = getpass.getpass("Confirmer le mot de passe : ")
    if first != second:
        raise ValueError("Les deux mots de passe sont differents.")
    return first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("--username", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--role", choices=("admin", "reader"), required=True)
    reset = commands.add_parser("password")
    reset.add_argument("--username", required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Utiliser sudo pour gerer les comptes locaux.")
        store = AuthStore()
        if args.command == "init":
            result = {"status": "initialized", "changed": store.initialize()}
        elif args.command == "list":
            result = {"items": store.users()}
        elif args.command == "add":
            result = {"status": "created", "user": store.add_user(args.username, args.name, args.role, password())}
        else:
            result = {"status": "password_updated", "user": store.set_password(args.username, password())}
    except (ValueError, AuthStorageError, AuthConflict, AuthDenied) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
