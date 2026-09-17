"""Client configuration: which rai to talk to, and where credentials live."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RAI_URL = "https://dev-ms-rai-92624805037.us-central1.run.app"
CLIENT_ID = "luca-mcp"  # pre-registered public client in rai's OAuth server
SCOPES = "openid email"
SERVICE_NAME = "luca-mcp"


@dataclass(frozen=True)
class Config:
    rai_url: str
    client_id: str
    scopes: str
    config_dir: Path
    request_timeout: float
    login_timeout: float
    # Tool modules to expose (None = all). Session tools are always on. See tools/__init__.py.
    toolsets: tuple[str, ...] | None = None

    @property
    def api_root(self) -> str:
        return f"{self.rai_url}/api/v1/mcp"

    @property
    def credentials_file(self) -> Path:
        return self.config_dir / "credentials.json"


def load_config() -> Config:
    rai_url = os.getenv("LUCA_RAI_URL", DEFAULT_RAI_URL).strip().rstrip("/")
    config_dir = Path(os.getenv("LUCA_MCP_CONFIG_DIR", "~/.config/luca-mcp")).expanduser()
    return Config(
        rai_url=rai_url,
        client_id=os.getenv("LUCA_MCP_CLIENT_ID", CLIENT_ID).strip() or CLIENT_ID,
        scopes=os.getenv("LUCA_MCP_SCOPES", SCOPES).strip() or SCOPES,
        config_dir=config_dir,
        request_timeout=float(os.getenv("LUCA_MCP_TIMEOUT", "60")),
        login_timeout=float(os.getenv("LUCA_MCP_LOGIN_TIMEOUT", "300")),
        toolsets=_toolsets(os.getenv("LUCA_MCP_TOOLSETS", "")),
    )


def _toolsets(value: str) -> tuple[str, ...] | None:
    """`LUCA_MCP_TOOLSETS=reports,sat` limits the tools an agent sees; empty means all."""
    names = tuple(name.strip() for name in value.split(",") if name.strip())
    return names or None
