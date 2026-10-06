import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_otp_email(to_email: str, otp: str) -> None:
    """Send OTP email in background task."""
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.EMAILS_FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = "Verify Your Email - KaziHub"

        body = f"Your 5-digit verification code is: {otp}\n\nThis code expires in 10 minutes."
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAILS_FROM_EMAIL, to_email, msg.as_string())

        logger.info(f"Successfully sent OTP email to {to_email}")

    except Exception as e:
        logger.error(f"Failed to send OTP email to {to_email}: {e}")


def send_email_change_otp(to_email: str, otp: str) -> None:
    """Send the confirmation OTP for an email-change request, to the NEW
    address (proves the user actually controls it before the swap)."""
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.EMAILS_FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = "Confirm Your New Email - KaziHub"

        body = (
            f"You requested to change your KaziHub account email to this address.\n\n"
            f"Your 5-digit confirmation code is: {otp}\n\n"
            f"This code will expire in 10 minutes. If you did not request this, please ignore this email."
        )
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAILS_FROM_EMAIL, to_email, msg.as_string())

        logger.info(f"Successfully sent email-change OTP to {to_email}")

    except Exception as e:
        logger.error(f"Failed to send email-change OTP to {to_email}: {e}")


def send_password_reset_email(to_email: str, otp: str) -> None:
    """Send password reset OTP email via background task."""
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.EMAILS_FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = "Password Reset Request - KaziHub"

        body = (
            f"You requested a password reset for your account.\n\n"
            f"Your 5-digit reset code is: {otp}\n\n"
            f"This code will expire in 10 minutes. If you did not request this, please ignore this email."
        )
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.EMAILS_FROM_EMAIL, to_email, msg.as_string())

        logger.info(f"Successfully sent password reset email to {to_email}")

    except Exception as e:
        logger.error(f"Failed to send password reset email to {to_email}: {e}")

def _send(to_email: str, subject: str, body: str) -> None:
    msg = MIMEMultipart()
    msg["From"] = settings.EMAILS_FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.EMAILS_FROM_EMAIL, to_email, msg.as_string())


def send_support_ticket_email(
    to_email: str, ticket_number: str, from_user: str, subject: str, message: str, booking_id
) -> None:
    """Copy of a new support ticket to the support inbox (ask 14)."""
    try:
        body = (
            f"Ticket: {ticket_number}\nFrom: {from_user}\n"
            f"Booking: {booking_id or '-'}\n\n{message}"
        )
        _send(to_email, f"[{ticket_number}] {subject}", body)
    except Exception as e:
        logger.error(f"Failed to send support ticket {ticket_number} email: {e}")


def send_notification_summary_email(to_email: str, first_name: str, lines: list) -> bool:
    """Daily summary of unread notifications (ask 12)."""
    try:
        body = (
            f"Hi {first_name},\n\nHere's what you missed on KaziHub:\n\n"
            + "\n".join(f"- {line}" for line in lines)
            + "\n\nOpen the app to see more. You can turn these emails off in Settings."
        )
        _send(to_email, "Your KaziHub summary", body)
        return True
    except Exception as e:
        logger.error(f"Failed to send summary email to {to_email}: {e}")
        return False
