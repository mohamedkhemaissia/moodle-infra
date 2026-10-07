"""Initialiser le journal dedie sans toucher aux incidents."""
import json
import os

from .collector import load_config
from .run import CONFIG
from .store import DiagnosticStore


if __name__ == "__main__":
    os.umask(0o027)
    changed = DiagnosticStore(load_config(CONFIG)["data_dir"]).initialize()
    print(json.dumps({"changed": changed}))
