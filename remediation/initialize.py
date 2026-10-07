"""Initialiser le journal dedie sans toucher aux incidents."""
import json
import os

from .action import config_read
from .run import CONFIG
from .store import RemediationStore


if __name__ == "__main__":
    os.umask(0o027)
    changed = RemediationStore(config_read(CONFIG)["data_dir"]).initialize()
    print(json.dumps({"changed": changed}))
