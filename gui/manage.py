"""Owner-only CLI; no unauthenticated password-setup endpoint is exposed."""
import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
from auth import AuthRepository, AuthService
from shared import AppError, Paths


def main():
    if sys.argv[1:] != ["set-password"]:
        raise SystemExit("Usage: python manage.py set-password")
    first = getpass.getpass("New web console password (12+ characters): ")
    second = getpass.getpass("Confirm web console password: ")
    if first != second:
        raise SystemExit("Passwords differ. No credentials changed.")
    try:
        AuthService(AuthRepository(Paths.from_env().state)).set_password(first)
    except AppError as exc:
        raise SystemExit(exc.message)
    print("Web console password saved. Existing web sessions revoked.")


if __name__ == "__main__":
    main()
