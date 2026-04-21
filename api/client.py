from utils.retry_decorator import retry
import logging
import random

log = logging.getLogger(__name__)

class ExternalAPIClient:
    """
    Client for interacting with external services, incorporating retry logic.
    """

    @retry(max_attempts=5, initial_delay=0.5)
    def fetch_data(self, endpoint: str) -> dict:
        """
        Fetches data from an external API endpoint.
        Simulates transient network failures.
        """
        log.info(f"Attempting to fetch data from {endpoint}")

        # Simulate occasional failures (e.g., 30% chance of transient error)
        if random.random() < 0.3:
            raise ConnectionError(f"Transient connection failure to {endpoint}")

        return {"data": f"Data from {endpoint}", "status": 200}

    @retry(max_attempts=3, initial_delay=1.0)
    def post_request(self, endpoint: str, payload: dict) -> dict:
        """
        Submits data to an external API endpoint.
        """
        log.info(f"Attempting POST to {endpoint} with payload: {payload}")

        # Simulate occasional failures
        if random.random() < 0.2:
            raise TimeoutError(f"Request timed out to {endpoint}")
