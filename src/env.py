from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Application
    APPLICATION_CLIENT_ID: str
    APPLICATION_CLIENT_SECRET: str

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
