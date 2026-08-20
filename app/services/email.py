import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from fastapi import HTTPException, status
from app.core.config import settings


def send_otp_email(email_to: str, otp: str):
    """Send 5-digit OTP code to user via Mailtrap SMTP."""
    message = MIMEMultipart("alternative")
    message["Subject"] = "KaziHub - Verify Your Email"
    message["From"] = settings.EMAILS_FROM_EMAIL
    message["To"] = email_to

    text_content = f"Welcome to KaziHub. Your 5-digit verification code is: {otp}. This code expires in 10 minutes."

    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; margin: auto; padding: 20px; border: 1px solid #e0e0e0; border-radius: 8px;">
        <h2 style="color: #1a365d; text-align: center;">Welcome to KaziHub</h2>
        <p>Thank you for registering. Please use the following 5-digit verification code to complete your signup process:</p>
        <div style="text-align: center; margin: 30px 0;">
            <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #2b6cb0; background: #ebf8ff; padding: 10px 20px; border-radius: 6px;">{otp}</span>
        </div>
        <p style="color: #718096; font-size: 14px;">This code will expire in 10 minutes. If you did not request this, please ignore this email.</p>
    </div>
    """

    message.attach(MIMEText(text_content, "plain"))
    message.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, int(settings.SMTP_PORT), timeout=10) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAILS_FROM_EMAIL, email_to, message.as_string())
    except Exception as e:
        print(f"Failed to send email to {email_to}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send verification email: {str(e)}"
        )