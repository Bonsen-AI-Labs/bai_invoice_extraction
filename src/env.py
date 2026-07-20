from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Observability
    LOG_LEVEL: Literal["INFO", "ERROR", "DEBUG"] = "INFO"
    ENVIRONMENT: Literal["dev", "local"] = "local"
    TEMPLATE_LIVE_LEARNING: bool = False

    # Application
    APPLICATION_CLIENT_ID: str
    APPLICATION_CLIENT_SECRET: str
    APPLICATION_TENANT_ID: str

    # Cosmos DB
    COSMOS_ENDPOINT: str
    COSMOS_KEY: str
    COSMOS_DB_NAME: str
    COSMOS_HISTORY_CONTAINER: str

    # Azure Document Intellegence
    DOC_INTEL_ENDPOINT: str
    DOC_INTEL_KEY: str

    # Sharepoint
    SHAREPOINT_FOLDER_URL: str
    MASTER_EXCEL_DOCUMENT_URL: str

    # Azure OpenAI
    # AZURE_OPENAI_API_ENDPOINT: str
    # AZURE_OPENAI_API_KEY: str
    OPENAI_API_KEY: str
