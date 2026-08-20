import smtplib
from email.mime.text import MIMEText
from app.core.config import settings

print(f"Loaded Host: {settings.SMTP_HOST}")
print(f"Loaded Port: {settings.SMTP_PORT}")
print(f"Loaded User: {settings.SMTP_USER}")
print(f"Loaded From Email: {settings.EMAILS_FROM_EMAIL}")

msg = MIMEText("Testing Mailtrap connection from Kazihub backend.")
msg["Subject"] = "KaziHub Direct Test"
msg["From"] = settings.EMAILS_FROM_EMAIL
msg["To"] = "peaceolaoluwa2006@gmail.com"

try:
    print("Connecting to SMTP server...")
    # Added timeout=10 to prevent hanging on blocked ports
    with smtplib.SMTP(settings.SMTP_HOST, int(settings.SMTP_PORT), timeout=10) as server:
        server.set_debuglevel(1)
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.EMAILS_FROM_EMAIL, ["peaceolaoluwa2006@gmail.com"], msg.as_string())
    print("\n✅ SUCCESS: Email sent successfully!")
except Exception as e:
    print(f"\n❌ FAILED: {e}")