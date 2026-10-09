"""Initialiser le journal additionnel sans modifier les demandes existantes."""
import json
import sys
from .recovery_execution import RecoveryExecutionStore


def main():
    if sys.argv[1:] != ['init']:return 2
    try:
        service=RecoveryExecutionStore()
        changed=service.initialize_execution()
        service.check()
        print(json.dumps({'status':'available','changed':changed}));return 0
    except Exception:
        print(json.dumps({'status':'unavailable','changed':False}));return 2


if __name__=='__main__':sys.exit(main())
