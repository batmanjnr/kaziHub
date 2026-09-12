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

    # FIX (high-assurance security review): previously kyc_upload_token.py
    # signed its tokens with SECRET_KEY — the same secret used to sign JWTs.
    # A single leaked secret would then let an attacker both forge access
    # tokens AND forge KYC-upload ownership tokens. Each security context
    # gets its own scoped key so a compromise of one doesn't cascade into
    # the other. Falls back to SECRET_KEY only if unset, so existing
    # deployments don't break before rotating in a dedicated value.
    KYC_UPLOAD_TOKEN_SECRET: str = ""

    # Blind-index key for enforcing one-account-per-NIN (anti-fraud) without
    # ever storing or indexing the plaintext NIN. NIN itself stays
    # AES-256-GCM encrypted (nin_encrypted, non-deterministic, unsearchable);
    # this key produces a *deterministic* HMAC-SHA256 (nin_hash) so the same
    # real NIN always hashes to the same value and a unique DB index can
    # catch a second registration — but the hash can't be reversed back to
    # the NIN. Its own dedicated key, same reasoning as KYC_UPLOAD_TOKEN_SECRET.
    NIN_HASH_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def model_post_init(self, __context) -> None:
        if not self.KYC_UPLOAD_TOKEN_SECRET:
            self.KYC_UPLOAD_TOKEN_SECRET = self.SECRET_KEY
        if not self.NIN_HASH_KEY:
            self.NIN_HASH_KEY = self.SECRET_KEY

settings = Settings()