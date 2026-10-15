"""
Settings for the whole app (API keys, phone numbers, model name).

Values are read from the `.env` file in the project root (copy `.env.example`).
Environment variable names are case-insensitive: GROQ_API_KEY -> settings.groq_api_key

Use anywhere with:
    from app.config import settings
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" -> keys in .env that are not listed below are simply skipped
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM (Groq) ---
    groq_api_key: str                       # required
    groq_api_key_2: str = ""                # optional; the router node uses it to spread rate limits
    llm_model: str = "openai/gpt-oss-120b"  # check available ones: https://console.groq.com/docs/models

    # --- Place search (SerpStack) ---
    serp_api_key: str                       # required
    serp_api_url: str = "https://api.serpstack.com/search"

    # --- Review scraping (WebScraping.AI) ---
    webscraping_ai_api_key: str = ""        # optional; without it reviews are skipped

    # --- SMS (provider not wired up yet - Twilio is planned) ---
    default_target_number: str = ""         # used when a shop has no phone number, e.g. +919876543210

    # --- Langfuse: prompts (app/prompts.py) + tracing (app/telemetry.py) ---
    langfuse_public_key: str                # required: every prompt is loaded from Langfuse
    langfuse_secret_key: str                # required
    langfuse_host: str = "https://cloud.langfuse.com"  # US projects: https://us.cloud.langfuse.com
    langfuse_prompt_label: str = "production"  # which prompt version to use
    langfuse_tracing: bool = True           # false = do not send traces (e.g. on your laptop)
    service_name: str = "place-discover-agent"
    deployment_environment: str = "local"

    # --- Optional: also save traces as files in a Google Drive folder (app/drive_export.py) ---
    drive_traces: bool = False              # true = save traces to Drive (log in first: scripts/google_drive_login.py)
    drive_folder_name: str = "place-discover-traces"

    log_level: str = "INFO"


settings = Settings()
