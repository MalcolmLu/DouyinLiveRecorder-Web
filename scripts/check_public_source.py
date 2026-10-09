"""Reject private runtime artifacts and obvious credentials before publishing."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
paths = subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"],
                                cwd=ROOT).decode().split("\0")
forbidden_parts = {"data","vendor","__pycache__","state","runtime"}
patterns = [
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"192\.168\.\d{1,3}\.\d{1,3}(?=\D|$)"),
    re.compile(r"/Users/[^/\s]+/(?:Desktop|Documents)/"),
]
bad = []
count = 0
for name in sorted(set(paths)):
    if not name:
        continue
    path = ROOT / name
    if not path.is_file():
        continue
    count += 1
    if forbidden_parts.intersection(Path(name).parts) or path.suffix in {".sqlite3",".db",".key",".crt",".pem",".mp4",".ts",".mkv",".flv"} or name == ".env":
        bad.append(name + ": private runtime artifact")
        continue
    try:
        content = path.read_text()
    except UnicodeDecodeError:
        continue
    if name != "scripts/check_public_source.py" and any(p.search(content) for p in patterns):
        bad.append(name + ": private path/address or credential")
if bad:
    raise SystemExit("\n".join(bad))
print("Public-source check passed:",count,"files; no private runtime files or matched credentials.")
