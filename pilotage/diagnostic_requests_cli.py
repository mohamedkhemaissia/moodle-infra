"""Initialiser ou verifier la file PostgreSQL des diagnostics guides."""
import argparse
import json

from .diagnostic_requests import DiagnosticRequestStorageError, DiagnosticRequestStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check"))
    args = parser.parse_args()
    try:
        store = DiagnosticRequestStore()
        changed = store.initialize() if args.command == "init" else False
        store.check()
    except DiagnosticRequestStorageError:
        parser.exit(2, "File des diagnostics indisponible ; verifier PostgreSQL, les comptes et les incidents.\n")
    print(json.dumps({"status": "available", "changed": changed, "schema_version": 1, "mode": "read_only"}))


if __name__ == "__main__":
    main()
