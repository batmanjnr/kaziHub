from pydantic import BaseModel, EmailStr, Field

from app.core.validators import Password


class ForgotPasswordSchema(BaseModel):
    email: EmailStr


class ResetPasswordSchema(BaseModel):
    email: EmailStr
    otp: str
    new_password: Password


class ChangePasswordSchema(BaseModel):
    current_password: str
    new_password: Password


class RequestEmailChangeSchema(BaseModel):
    new_email: EmailStr
    current_password: str


class ConfirmEmailChangeSchema(BaseModel):
    otp: str


class LogoutSchema(BaseModel):
    refresh_token: str


class TwoFactorDisableSchema(BaseModel):
    current_password: str
    totp_code: str = Field(
        max_length=20,
        description="A 6-digit authenticator code, or one unused backup code: 8 characters 0-9/A-F "
        "shown as `7F3A-9C21`, accepted with or without the hyphen, any case.",
    )
