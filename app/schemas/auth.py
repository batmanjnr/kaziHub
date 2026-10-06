from pydantic import BaseModel, EmailStr

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
    totp_code: str  # a 6-digit authenticator code, or one unused backup code
