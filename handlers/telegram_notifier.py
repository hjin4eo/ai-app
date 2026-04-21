import logging
from typing import List
import asyncio

log = logging.getLogger(__name__)

# NOTE: In a real implementation, this module would depend on an initialized
# TelegramClient service. We use a mock client here for structural completeness.

class MockTelegramClient:
    """Mock client to simulate API interaction."""
    async def send_message(self, chat_id: int, text: str) -> bool:
        # Simulate network delay
        await asyncio.sleep(0.1)
        log.info(f"Mock: Sent notification to chat {chat_id}. Message preview: {text[:50]}...")
        return True

telegram_client = MockTelegramClient()

async def send_notification(links: List[str], chat_id: int = 12345) -> bool:
    """
    Sends a notification containing a list of detected links to Telegram.

    Args:
        links: List of detected links.
        chat_id: The target Telegram chat ID.

    Returns:
        True if the notification was sent successfully, False otherwise.
    """
    if not links:
        log.info("No links provided for notification.")
        return True

    # Format the links into a markdown-friendly list
    formatted_links = "\n".join([f"🔗 {link}" for link in links])
    message = (
