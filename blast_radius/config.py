"""Settings read from environment variables (and a local .env file if present)."""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional; plain env vars still work
    pass


@dataclass(frozen=True)
class Settings:
    falkordb_host: str = os.getenv("FALKORDB_HOST", "localhost")
    falkordb_port: int = int(os.getenv("FALKORDB_PORT", "6379"))
    falkordb_username: str | None = os.getenv("FALKORDB_USERNAME") or None
    falkordb_password: str | None = os.getenv("FALKORDB_PASSWORD") or None
    falkordb_tls: bool = os.getenv("FALKORDB_TLS", "false").lower() == "true"


settings = Settings()
