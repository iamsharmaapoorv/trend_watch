import logging
import os

import requests

logger = logging.getLogger(__name__)


class TelegramAlert:
    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token if bot_token else os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id else os.getenv("TELEGRAM_CHAT_ID")

    def send(self, message) -> bool:
        """Send a message. Returns True only if Telegram accepted it."""
        if not self.bot_token or not self.chat_id:
            logger.error("Alert error: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            res = requests.post(url, data=payload, timeout=15)
            if res.status_code == 400 and "parse entities" in res.text:
                # Unbalanced Markdown in the message; resend as plain text.
                payload.pop("parse_mode")
                res = requests.post(url, data=payload, timeout=15)
            if res.status_code != 200:
                logger.error(f"Alert failed: {res.text}")
                return False
            logger.debug(f"Alert sent successfully to chat_id: {self.chat_id}")
            return True
        except Exception as e:
            # Log only the exception type: the message contains the URL, which holds the bot token.
            logger.error(f"Alert error: {type(e).__name__}")
            return False
