import time
import functools
import logging
from typing import Callable, Any, Type

log = logging.getLogger(__name__)

def retry(max_attempts: int = 3, initial_delay: float = 1.0, backoff_factor: float = 2.0, exceptions: tuple[Type[Exception], ...] = (Exception,)):
    """
    A decorator that retries a function call upon specific exceptions
    with exponential backoff.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt + 1 == max_attempts:
                        log.error(f"Function {func.__name__} failed after {max_attempts} attempts.")
                        raise last_exception

                    # Calculate delay: initial_delay * (backoff_factor ** attempt)
                    delay = initial_delay * (backoff_factor ** attempt)
                    log.warning(f"Attempt {attempt + 1} failed for {func.__name__}. Retrying in {delay:.2f} seconds. Error: {e}")
                    time.sleep(delay)

            if last_exception:
                 raise last_exception
        return wrapper
    return decorator
