"""Load runtime settings and determine which external capabilities are configured."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_MODEL = "deepseek-flash"
DEFAULT_OCR_GATEWAY_URL = "https://gateway.pltplt.com/ai/process"


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", str_strip_whitespace=True
    )

    llm_provider: str = "openai_compatible"
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = Field(default="", repr=False)
    deepseek_api_key: str = Field(default="", repr=False)
    llm_model: str = DEFAULT_LLM_MODEL
    llm_trust_env: bool = False
    llm_proxy_url: str = ""
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 1

    ocr_gateway_url: str = DEFAULT_OCR_GATEWAY_URL
    ocr_trust_env: bool = False
    ocr_proxy_url: str = ""
    ocr_connect_timeout_seconds: float = 10.0
    ocr_read_timeout_seconds: float = 300.0
    ocr_task: str = "orderChange"

    public_relay_base_url: str = ""
    relay_presign_ttl_seconds: int = Field(default=300, ge=1, le=300)

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    allow_remote_bind: bool = False
    app_data_dir: Path = Path("data")
    sqlite_db_path: Path = Path("data/shipping_billing.db")

    @field_validator("llm_model")
    @classmethod
    def default_blank_llm_model(cls, value: str) -> str:
        return value or DEFAULT_LLM_MODEL

    @field_validator("ocr_gateway_url")
    @classmethod
    def default_blank_ocr_gateway_url(cls, value: str) -> str:
        return value or DEFAULT_OCR_GATEWAY_URL

    @field_validator("llm_trust_env", "ocr_trust_env", mode="before")
    @classmethod
    def trim_trust_env_flag(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or False
        return value

    def resolve_llm_credentials(self) -> tuple[str, str | None]:
        base_url = self.llm_base_url.strip() or DEFAULT_LLM_BASE_URL
        explicit_key = self.llm_api_key.strip()
        if explicit_key:
            return base_url, explicit_key

        try:
            endpoint = urlparse(base_url)
        except ValueError:
            return base_url, None

        if endpoint.scheme.lower() == "https" and endpoint.hostname == "api.deepseek.com":
            return base_url, self.deepseek_api_key.strip() or None
        return base_url, None

    def is_llm_configured(self) -> bool:
        _, api_key = self.resolve_llm_credentials()
        return api_key is not None

    def is_ocr_direct_mode(self) -> bool:
        return not self.ocr_trust_env and not self.ocr_proxy_url.strip()

    def is_public_relay_ready(self, relay_publisher: object | None = None) -> bool:
        return bool(self.public_relay_base_url.strip()) and relay_publisher is not None


def load_settings() -> AppSettings:
    """Read the current environment and .env without populating the settings cache."""
    return AppSettings()


@cache
def get_settings() -> AppSettings:
    """Return the cached settings instance until get_settings.cache_clear() is called."""
    return load_settings()
