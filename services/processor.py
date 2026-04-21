import logging
from api.client import ExternalAPIClient

log = logging.getLogger(__name__)

class DataProcessorService:
    """
    Handles business logic, interacting with external APIs.
    """
    def __init__(self):
        self.api_client = ExternalAPIClient()

    def process_external_data(self, endpoint: str) -> dict:
        """
        Orchestrates fetching and processing data, relying on API client's retry logic.
        If the retries fail, it handles the final exception.
        """
        log.info(f"Starting data processing for {endpoint}")
        try:
            # This call will execute the retry logic internally if failures occur
            data = self.api_client.fetch_data(endpoint)
            log.info("Data successfully retrieved after potential retries.")

            # Simulate further processing
            data['processed'] = True
