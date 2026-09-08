"""Runtime configuration for the Luca MCP server.

Every value comes from the environment (or a local ``.env`` for development). Secrets are never
defaulted: a missing signing key or upstream client secret fails startup on purpose.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Redirect URIs MCP clients are allowed to register. Loopback entries are matched with the
# RFC 8252 §7.3 rule (any port), everything else is an exact match. Extend by configuration,
# never by editing tool code. Sources: docs/research/2026-09-08-mcp-oauth-clients.md §3.
DEFAULT_ALLOWED_REDIRECT_URIS: tuple[str, ...] = (
    "http://localhost:*",
    "http://127.0.0.1:*",
    "http://[::1]:*",
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://vscode.dev/redirect",
    "https://www.cursor.com/agents/mcp/oauth/callback",
    "cursor://anysphere.cursor-mcp/oauth/callback",
    "https://antigravity.google/oauth-callback",
)

# Scopes advertised to MCP clients in the beta. Advertised == requested == sent upstream ==
# stored on the refresh token, so Auth0 issues a refresh token (Claude Code only requests
# advertised scopes). See docs/plans/2026-09-08-luca-mcp-plan.md §3.2 "Scopes".
DEFAULT_ADVERTISED_SCOPES: tuple[str, ...] = ("openid", "email", "offline_access")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- deployment -------------------------------------------------------------------
    luca_mcp_env: Literal["local", "dev", "stg", "prod"] = "dev"
    luca_mcp_base_url: HttpUrl = Field(
        description="Public base URL of this server; issuer of the tokens it mints.",
    )
    luca_mcp_port: int = 8080
    luca_mcp_build: str = Field(
        default="local",
        description="Build identifier surfaced in whoami and error hints (image tag / git sha).",
    )
    log_level: str = "INFO"

    # --- Liebre API ---------------------------------------------------------------------
    liebre_api_base_url: HttpUrl = Field(
        description="Liebre API root, e.g. https://api-dev.liebre.ai/api/v1"
    )
    liebre_app_base_url: HttpUrl = Field(description="Liebre web app root used for platform links.")
    liebre_timeout_seconds: float = 30.0
    liebre_tool_deadline_seconds: float = 60.0
    liebre_max_retries: int = 3

    # --- upstream identity provider (Auth0) ------------------------------------------------
    auth0_config_url: HttpUrl = Field(description="OpenID discovery document of the upstream IdP.")
    auth0_client_id: str
    auth0_client_secret: str
    auth0_audience: str = Field(
        description="Liebre API identifier requested as `audience` upstream."
    )
    auth0_discovery_timeout_seconds: int = 10
    auth0_discovery_retries: int = 3

    # --- keys and stores ----------------------------------------------------------------------
    luca_mcp_jwt_signing_key: str = Field(min_length=32)
    luca_mcp_storage_key: str = Field(
        description="Fernet key used to encrypt the OAuth state store."
    )
    redis_url: str | None = None

    # --- OAuth surface --------------------------------------------------------------------------
    luca_mcp_allowed_redirect_uris: list[str] = Field(default=list(DEFAULT_ALLOWED_REDIRECT_URIS))
    luca_mcp_advertised_scopes: list[str] = Field(default=list(DEFAULT_ADVERTISED_SCOPES))
    luca_mcp_required_scopes: list[str] = Field(default=["openid"])
    luca_mcp_access_token_ttl_seconds: int = 3600
    luca_mcp_upstream_refresh_threshold_seconds: int = 300
    luca_mcp_consent_mode: Literal["always", "remember", "off"] = "remember"

    # Extra hostnames this server may be reached on (e.g. the run.app URL next to the custom
    # domain). The host of LUCA_MCP_BASE_URL is always allowed.
    luca_mcp_allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("luca_mcp_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # --- tenant context ---------------------------------------------------------------------------
    tenant_cache_ttl_seconds: int = 300

    @field_validator(
        "luca_mcp_allowed_redirect_uris",
        "luca_mcp_advertised_scopes",
        "luca_mcp_required_scopes",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _check_urls(self) -> Settings:
        if str(self.luca_mcp_base_url).endswith("/"):
            # HttpUrl normalises "http://host" to "http://host/"; strip once so issuer and
            # resource comparisons stay byte-exact for clients.
            object.__setattr__(
                self, "luca_mcp_base_url", HttpUrl(str(self.luca_mcp_base_url).rstrip("/"))
            )
        if self.luca_mcp_env == "prod" and not str(self.luca_mcp_base_url).startswith("https://"):
            raise ValueError("LUCA_MCP_BASE_URL must be https in prod")
        if self.luca_mcp_consent_mode == "off" and self.luca_mcp_env not in ("local", "dev"):
            raise ValueError("Consent cannot be disabled outside local/dev")
        return self

    # --- derived -----------------------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return str(self.luca_mcp_base_url).rstrip("/")

    @property
    def mcp_url(self) -> str:
        """Canonical resource URL (RFC 8707). No trailing slash, ever."""
        return f"{self.base_url}/mcp"

    @property
    def liebre_api_root(self) -> str:
        return str(self.liebre_api_base_url).rstrip("/")

    @property
    def liebre_app_root(self) -> str:
        return str(self.liebre_app_base_url).rstrip("/")

    @property
    def consent_setting(self) -> bool | Literal["remember"]:
        if self.luca_mcp_consent_mode == "off":
            return False
        if self.luca_mcp_consent_mode == "remember":
            return "remember"
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
