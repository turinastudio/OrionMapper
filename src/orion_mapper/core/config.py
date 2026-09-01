from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ORION_",
        extra="ignore",
    )

    # TMDB Configuration
    tmdb_api_key: str = Field(
        default="34fafb223263c2461f8f88a3489cb92e",
        description="TMDB v3 API Key with working default fallback",
    )
    tmdb_rate_limit: float = Field(default=40.0, description="Max TMDB API requests per second")
    tmdb_rate_burst: int = Field(default=40, description="Max burst token capacity for TMDB")

    # Scraper Rate Limits
    default_provider_rate_limit: float = Field(default=5.0, description="Default provider req/sec")
    default_provider_rate_burst: int = Field(default=10, description="Default provider burst capacity")

    # HTTP Client Configuration
    http_timeout: float = Field(default=15.0, description="Request timeout in seconds")
    http_max_retries: int = Field(default=3, description="Maximum retry attempts for transient errors")
    http_backoff_factor: float = Field(default=1.5, description="Exponential backoff factor")
    http_backoff_max: float = Field(default=30.0, description="Maximum backoff wait time in seconds")
    http_pool_max_connections: int = Field(default=100, description="HTTP connection pool limit")
    http_pool_max_keepalive: int = Field(default=30, description="Max keepalive connections")
    user_agents: list[str] = Field(default_factory=lambda: list(DEFAULT_USER_AGENTS))

    # Storage Paths
    data_dir: Path = Field(default=Path("data"))
    mappings_dir: Path = Field(default=Path("data/mappings"))
    orion_mappings_dir: Path = Field(default=Path("data/orion_mappings"))

    # Logging
    log_level: str = Field(default="INFO")


settings = Settings()
