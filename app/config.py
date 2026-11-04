from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    portal_url: str = "https://urja-ops.flockenergy.tech"
    portal_email: str = "operator@urja.local"
    # no default on purpose, I don't want a password sitting in the repo
    portal_password: SecretStr = SecretStr("")

    # meter list barely changes, readings are every 30 mins at best
    index_ttl_seconds: int = 900
    readings_ttl_seconds: int = 300

    # the portal 429s if you rush it and doesn't tell you the limit,
    # so we just keep a gap between calls. see PROTOCOL.md
    portal_min_gap_seconds: float = 0.7
    portal_timeout_seconds: float = 30.0
