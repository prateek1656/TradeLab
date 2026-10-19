"""Runtime configuration, read from environment variables (or a .env file)."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

IST = ZoneInfo("Asia/Kolkata")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TRADELAB_", extra="ignore")

    # Where generated secrets, caches and logs live (the records themselves are in MongoDB).
    data_dir: Path = Path("./data")
    # MongoDB: a replica set (even of one server) so a save is all-or-nothing (transactions).
    mongo_url: str = "mongodb://127.0.0.1:27017/?replicaSet=rs0"
    mongo_db: str = "tradelab"
    mongo_timeout_ms: int = 10000
    # None: use transactions when the server supports them (a replica set).
    mongo_transactions: bool | None = None

    # Master secret used to sign login tokens and encrypt broker credentials.
    # Generated and persisted in data_dir on first start when not supplied.
    secret_key: str | None = None

    token_ttl_hours: int = 24 * 7

    # The admin page (who may use TradeLab). Leave unset and there is no admin page and sign-ups need no
    # approval, as before. See docs/DEPLOY.md: python -m app.tools.admin_password makes these.
    admin_username: str | None = None
    admin_password_hash: str | None = None  # "scrypt:<salt hex>:<hash hex>", never the password itself
    admin_path: str | None = None  # the secret last part of the admin page's address

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_username and self.admin_password_hash and self.admin_path
                    and len(self.admin_path) >= 16)
    # Disable all scheduled jobs (used by tests).
    disable_scheduler: bool = False
    # Directory holding the built frontend (served at /).
    frontend_dist: Path = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    # Parallel broker calls when firing orders across many accounts.
    max_parallel_orders: int = 32

    def master_secret(self) -> str:
        if self.secret_key:
            return self.secret_key
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "secret.key"
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48))
            path.chmod(0o600)
        return path.read_text().strip()

    def fernet_key(self) -> bytes:
        digest = hashlib.sha256(("fernet:" + self.master_secret()).encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def jwt_secret(self) -> str:
        return hashlib.sha256(("jwt:" + self.master_secret()).encode()).hexdigest()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    # The Kotak SDK writes a rotating warning log; keep it with the app data, not in the cwd.
    os.environ.setdefault("NEO_LOG_FILE_PATH", str((s.data_dir / "logs" / "neo-api-client.log").resolve()))
    return s
