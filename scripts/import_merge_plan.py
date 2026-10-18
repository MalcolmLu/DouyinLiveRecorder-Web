"""Review or import an explicit session plan; the worker performs verified merges."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
from merge_queue import seed_sessions
from platform_utils import recording_roots
from shared import Paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    entries = plan["sessions"]
    if args.apply:
        paths = Paths.from_env()
        with sqlite3.connect(paths.queue, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            seed_sessions(db, entries, recording_roots(paths.config, paths.downloads))
    print(json.dumps({"applied": args.apply, "sessions": len(entries), "parts": sum(len(s["parts"]) for s in entries)}, indent=2))


if __name__ == "__main__":
    main()
