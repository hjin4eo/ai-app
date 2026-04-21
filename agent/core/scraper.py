#!/usr/bin/env python3
"""
scraper.py — 외부 URL 크롤링 및 원본 데이터 수집 엔진 (AI Curation MVP)
"""
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import json
import datetime
import logging

try:
    # Import configuration loaded from config.yaml
    from core.bot_config import CFG
except ImportError:
    # Handle case where core.bot_config might not be fully initialized in testing environments
    log = logging.getLogger(__name__)
    log.error("Could not import configuration from core.bot_config. Ensure setup is correct.")
    CFG = {}

log = logging.getLogger(__name__)

# Define the target storage directory
SCRAPER_ROOT_DIR = Path(__file__).parent.parent / "00_Raw" / "AI_Curation"

def get_target_urls() -> list[str]:
    """Retrieves the list of URLs from the configuration."""
    # Assuming configuration has a section for AI Curation sources
    urls = CFG.get("ai_curation", {}).get("source_urls", [])
    if not isinstance(urls, list):
        log.warning("Source URLs in config are not a list or are missing.")
        return []
    return urls

def save_raw_data(url: str, content: str, title: str) -> None:
    """Saves the scraped raw content and metadata to the designated directory."""
    SCRAPER_ROOT_DIR.mkdir(parents=True, exist_ok=True)

    # Generate a unique filename based on URL hash and timestamp
    # Sanitization prevents filesystem errors
    base_name = "".join(c if c.isalnum() else "_" for c in url[:80]).replace(" ", "_")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{base_name}_{timestamp}.txt"

    file_path = SCRAPER_ROOT_DIR / filename

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Save metadata (JSON) for future processing/logging
        metadata = {
            "url": url,
            "timestamp": timestamp,
            "title": title,
            "source": "AI_Curation_Pipeline"
        }
        with open(file_path.with_suffix('.json'), 'w', encoding='utf-8') as f_meta:
            json.dump(metadata, f_meta, indent=2)

        log.info(f"Successfully saved raw data for {url} to {file_path.name}")
    except IOError as e:
        log.error(f"Error saving raw data for {url}: {e}")

def scrape_url(url: str) -> tuple[str, str]:
    """
    Fetches content from a URL and extracts clean body text.
    Returns (Title, Clean Body Content).
    """
    log.info(f"Starting scrape for: {url}")
    try:
        headers = {'User-Agent': 'ExpertSoftwareEngineer/AI-Curation-Scraper/1.0'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')
        title = soup.title.string if soup.title else "Untitled Document"

        # Use a general selector to capture main content
        main_content = soup.find(['article', 'main', 'body'])
        content = main_content.get_text(separator='\n', strip=True) if main_content else soup.get_text(separator='\n', strip=True)

        return title, content

    except requests.exceptions.RequestException as e:
        log.error(f"Failed to fetch {url}: {e}")
        return "Scrape Error", f"Failed to scrape: {e}"
    except Exception as e:
        log.error(f"An unexpected error occurred during scraping {url}: {e}")
        return "Scrape Error", f"An unexpected error occurred: {e}"
