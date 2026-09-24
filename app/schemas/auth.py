from pydantic import BaseModel, EmailStr


class ForgotPasswordSchema(BaseModel):
    email: EmailStr


class ResetPasswordSchema(BaseModel):
    email: EmailStr
    otp: str
    new_password: str


class ChangePasswordSchema(BaseModel):
    current_password: str
    new_password: str


class RequestEmailChangeSchema(BaseModel):
    new_email: EmailStr
    current_password: str


class ConfirmEmailChangeSchema(BaseModel):
    otp: str