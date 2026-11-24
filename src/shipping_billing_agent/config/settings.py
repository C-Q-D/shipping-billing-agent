"""Load runtime settings and determine which external capabilities are configured."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, TypeAdapter, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_MODEL = "deepseek-flash"
DEFAULT_OCR_GATEWAY_URL = "https://ocr.example.com/ai/process"
_DEEPSEEK_ENDPOINT = ("https", "api.deepseek.com")
_HTTP_URL = TypeAdapter(AnyHttpUrl)


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
    llm_max_retries: int = 0

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
            endpoint = urlsplit(base_url)
        except ValueError:
            return base_url, None

        if (endpoint.scheme, endpoint.hostname) == _DEEPSEEK_ENDPOINT:
            return base_url, self.deepseek_api_key.strip() or None
        return base_url, None

    def resolve_validated_llm_credentials(self) -> tuple[str, str] | None:
        """Resolve credentials for a syntactically valid endpoint without network I/O."""
        base_url, api_key = self.resolve_llm_credentials()
        if (
            not api_key
            or "\\" in base_url
            or any(char.isspace() or ord(char) < 32 or char == "\x7f" for char in base_url)
        ):
            return None

        try:
            source_url = urlsplit(base_url)
            endpoint = _HTTP_URL.validate_python(base_url)
            port = source_url.port
        except ValueError:
            return None

        host = source_url.hostname
        if not host or "%" in host or source_url.username is not None or port == 0:
            return None
        if not endpoint.host or source_url.scheme != endpoint.scheme:
            return None
        if (
            api_key == self.deepseek_api_key.strip()
            and (endpoint.scheme, host) != _DEEPSEEK_ENDPOINT
        ):
            return None
        return str(endpoint), api_key

    def is_llm_configured(self) -> bool:
        return self.resolve_validated_llm_credentials() is not None

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
