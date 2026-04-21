import logging
import requests
from bs4 import BeautifulSoup
from typing import Dict, Any

log = logging.getLogger(__name__)

class WebScraper:
    """
    Handles fetching and parsing content from external URLs.
    """
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.headers = {
            'User-Agent': 'AI Curation Bot v1.0'
        }

    def scrape(self) -> str | None:
        """Fetches content from the URL and extracts text."""
        try:
            log.info(f"Starting scrape for URL: {self.base_url}")
            response = requests.get(self.base_url, headers=self.headers, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, 'html.parser')

            text_content = soup.get_text(separator=' ', strip=True)
            log.info("Scraping successful.")
            return text_content
        except requests.exceptions.RequestException as e:
