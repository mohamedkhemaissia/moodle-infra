#!/usr/bin/env python3
import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
events = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
ids = [event.get("snapshot_id") for event in events if event.get("message_type") == "summary"]
if len(ids) != 1 or not isinstance(ids[0], str) or re.fullmatch(r"[0-9a-f]{64}", ids[0]) is None:
    raise SystemExit("Identifiant du snapshot Restic invalide")
marker = Path(sys.argv[3])
marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
temporary = marker.with_suffix(".pending")
completed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
temporary.write_text(f"snapshot={ids[0]}\nfrozen_at_utc={sys.argv[2]}\ncompleted_at_utc={completed}\n")
os.chmod(temporary, 0o600)
os.replace(temporary, marker)
print(f"Sauvegarde enregistree : {ids[0]}")
