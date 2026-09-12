# app/services/sms.py
"""Pluggable SMS/WhatsApp dispatch (spec §7.3: Termii or Twilio to +234...
numbers). No provider account is configured for this project yet, so the
default implementation logs the message and reports success — a stand-in
until real Termii/Twilio credentials are wired up. Swap the body of
`send_sms`/`send_whatsapp` for a real API call; app.services.jobs.notify,
which calls these, doesn't need to change.
"""
import logging

logger = logging.getLogger("kazihub.sms")


async def send_sms(phone_number: str, message: str) -> bool:
    logger.info("[SMS stub] to=%s message=%s", phone_number, message)
    return True


async def send_whatsapp(phone_number: str, message: str) -> bool:
    logger.info("[WhatsApp stub] to=%s message=%s", phone_number, message)
    return True
