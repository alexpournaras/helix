from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    """Process-level config, read from env vars (HELIX_*) or backend/.env."""

    model_config = SettingsConfigDict(env_prefix="HELIX_", env_file=ENV_FILE, extra="ignore")

    app_name: str = "Helix"
    version: str = "0.1.0"
    data_dir: Path = BACKEND_DIR / "data"
    web_dist: Path = BACKEND_DIR.parent / "dist"
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]


class ProviderKeys(BaseSettings):
    """Third-party API keys, using each provider's conventional variable name. Never sent to the client."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    ncbi_api_key: str = ""
    ollama_api_key: str = ""  # only needed for ollama.com cloud models
    openalex_api_key: str = ""  # research search and citation graph (required by OpenAlex since Feb 2026)
    semantic_scholar_api_key: str = ""  # optional; higher Semantic Scholar rate limit
    unpaywall_email: str = ""  # contact email Unpaywall requires; Helix never fills it in

    def present(self) -> set[str]:
        return {name for name, value in self.model_dump().items() if value.strip()}


settings = Settings()
