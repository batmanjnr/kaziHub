# app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "KaziHub Backend"
    MONGODB_URL: str
    DATABASE_NAME: str = "kazihub_db"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    # Spec §3: 15-minute access tokens, backed by a 30-day rotating refresh
    # token so the frontend isn't forced to re-login constantly.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    SMTP_HOST: str = "sandbox.smtp.mailtrap.io"
    SMTP_PORT: int = 2525
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    EMAILS_FROM_EMAIL: str = "noreply@kazihub.com"
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # AES-256-GCM key (base64, 32 bytes decoded) for encrypting PII at the
    # application layer (NIN, document numbers, 2FA secrets). Local-dev value
    # only — production must source this from a secrets manager.
    PII_ENCRYPTION_KEY: str = ""

    PAYSTACK_SECRET_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()