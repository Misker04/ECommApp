from collections.abc import Iterable
from threading import Lock

import requests


class MarketplaceClient:
    """
    Base REST client for Buyer and Seller CLI (PA2).
    """

    _next_start_index = 0
    _start_index_lock = Lock()

    def __init__(self, base_url: str | Iterable[str]):
        if isinstance(base_url, str):
            urls = [base_url.rstrip("/")]
        else:
            urls = [str(url).rstrip("/") for url in base_url if str(url).strip()]
        if not urls:
            raise ValueError("at least one frontend URL is required")

        self.base_urls = urls
        with self._start_index_lock:
            self.current_index = self._next_start_index % len(self.base_urls)
            MarketplaceClient._next_start_index += 1
        self.session_id = None

    @property
    def current_base_url(self) -> str:
        return self.base_urls[self.current_index]

    @staticmethod
    def _should_retry_status(status_code: int) -> bool:
        return status_code in {502, 503, 504}

    # ---------------------------------
    # Core REST sender
    # ---------------------------------

    def _post(self, endpoint: str, payload: dict, require_session: bool = False):
        """
        Sends POST request to REST server.

        Args:
            endpoint: REST endpoint path
            payload: JSON body
            require_session: automatically attach session_token
        """

        if require_session:
            if not self.session_id:
                raise Exception("You must login first.")
            payload = dict(payload)
            payload["session_token"] = self.session_id

        last_error: Exception | None = None

        for offset in range(len(self.base_urls)):
            idx = (self.current_index + offset) % len(self.base_urls)
            url = f"{self.base_urls[idx]}/{endpoint}"

            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=10,
                )
            except requests.exceptions.RequestException as e:
                last_error = Exception(f"Connection error to {url}: {e}")
                continue

            if response.status_code == 200:
                self.current_index = idx
                return response.json()

            error = Exception(f"Request failed ({response.status_code}): {response.text}")
            if len(self.base_urls) > 1 and self._should_retry_status(response.status_code):
                last_error = error
                continue
            raise error

        raise last_error or Exception("All frontend replicas failed")

    # ---------------------------------
    # Session Handling
    # ---------------------------------

    def set_session(self, session_id: str):
        self.session_id = session_id

    def clear_session(self):
        self.session_id = None
