import asyncio
import logging
from typing import List

log = logging.getLogger(__name__)

# --- Mock Dependencies ---
# In a real application, these would be robust external modules

class TelegramNotifier:
    """Handles sending notifications via Telegram."""
    async def send_message(self, message: str):
        log.info(f"[TelegramNotifier] Sending message: {message}")

class WebsiteMonitor:
    """Simulates scraping and checking a website for new links."""
    def __init__(self, url: str):
        self.url = url
        # In a real scenario, this would store hashes or last known links
        self.last_scraped_links = set()

    async def monitor(self) -> List[str]:
        """
        Simulates fetching new links from the website.
        Returns a list of newly discovered links.
        """
        await asyncio.sleep(0.1) # Simulate I/O delay

        # Mock logic: Simulate finding new content on specified sites
        if "site1.com" in self.url:
            new_content = ["http://example.com/site1/new_page_a"]
        elif "site2.com" in self.url:
            new_content = ["http://example.com/site2/latest_update_b", "http://example.com/site2/beta_c"]
        else:
            new_content = []

        # Assuming all simulated content is 'new' for this mock example
        return new_content

# --- Global Instances ---
telegram_notifier = TelegramNotifier()
TARGET_WEBSITES = [
    "https://site1.com",
    "https://site2.com"
]

async def daily_job():
    """
    Monitors all configured websites sequentially for new content.
    Triggers notification if any new links are found.
    """
    log.info("--- Daily Job starting website monitoring cycle ---")

    monitors = [WebsiteMonitor(url) for url in TARGET_WEBSITES]

    for monitor in monitors:
        url = monitor.url
