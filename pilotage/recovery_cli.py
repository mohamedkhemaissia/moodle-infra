"""Initialiser uniquement le journal de demandes ; aucune restauration."""
import argparse
import json
from .recovery import RecoveryStore, RecoveryStorageError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check"))
    args = parser.parse_args()
    try:
        store = RecoveryStore()
        changed = store.initialize() if args.command == "init" else False
        store.check()
    except RecoveryStorageError:
        parser.exit(2, "Journal des demandes indisponible ; verifier PostgreSQL et les comptes.\n")
    print(json.dumps({"status": "available", "changed": changed, "schema_version": 1,
                      "execution_enabled": False}))


if __name__ == "__main__":
    main()
