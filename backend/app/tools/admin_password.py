"""Make (or change) the admin login for the server's .env.

    docker compose exec tradelab python -m app.tools.admin_password

Prints a strong random password and the three .env lines (username, password hash, secret page address).
Only the hash goes in .env; keep the password and the page address in your password manager.
Pass --password to hash a password you chose (at least 16 characters) instead of a random one.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import string

from app.security import hash_admin_password

ALPHABET = string.ascii_letters + string.digits + "-_.@#%+="


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", default="")
    ap.add_argument("--password", action="store_true", help="type your own password instead of a random one")
    ap.add_argument("--keep-path", default="", help="keep this admin page address part instead of a new one")
    a = ap.parse_args()
    username = a.username or "admin-" + secrets.token_hex(4)
    if a.password:
        password = getpass.getpass("New admin password (16+ characters): ")
        if len(password) < 16 or password != getpass.getpass("Again: "):
            raise SystemExit("The passwords don't match or are shorter than 16 characters.")
    else:
        password = "".join(secrets.choice(ALPHABET) for _ in range(28))
    path = a.keep_path or secrets.token_urlsafe(24)
    print("Admin username: ", username)
    if not a.password:
        print("Admin password: ", password)
    print("Admin page:      https://<your domain>/admin/" + path)
    print("\nPut these lines in .env, then run: docker compose up -d")
    print(f"TRADELAB_ADMIN_USERNAME={username}")
    print(f"TRADELAB_ADMIN_PASSWORD_HASH={hash_admin_password(password)}")
    print(f"TRADELAB_ADMIN_PATH={path}")


if __name__ == "__main__":
    main()
