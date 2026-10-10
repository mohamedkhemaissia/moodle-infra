"""Initialiser le schema des politiques sans activer les corrections."""
import json
from .operating_policy import PolicyStore


def main():
    changed=PolicyStore().initialize()
    print(json.dumps({"status":"available","changed":changed}))


if __name__ == "__main__":
    main()
