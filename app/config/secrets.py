"""Secrets, loaded from environment / `.env` only. Never logged, hashed or sent to AI.

Phase 0 defines the names. Phase 2 adds the encrypted vault (section 15) that holds
broker tokens; `.env` then only holds app keys and the vault location.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ALGO_", extra="ignore")

    fyers_app_id: SecretStr | None = None
    fyers_app_secret: SecretStr | None = None
    fyers_redirect_uri: str | None = None
    flattrade_api_key: SecretStr | None = None
    flattrade_api_secret: SecretStr | None = None
    flattrade_user_id: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    dashboard_password_hash: SecretStr | None = None
    vault_path: str = "data/vault.json"

    def __repr__(self) -> str:  # never show values, even by accident
        set_names = [k for k, v in self.__dict__.items() if v is not None]
        return f"Secrets(set={sorted(set_names)})"

    __str__ = __repr__
