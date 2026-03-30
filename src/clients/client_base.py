import requests


class MarketplaceClient:
    """
    Base REST client for Buyer and Seller CLI (PA2).
    """

    def __init__(self, base_url: str | list[str] | tuple[str, ...]):
        if isinstance(base_url, (list, tuple)):
            urls = [str(u).rstrip("/") for u in base_url if str(u).strip()]
        else:
            urls = [str(base_url).rstrip("/")]
        if not urls:
            raise ValueError("at least one frontend URL is required")
        self.base_urls = urls
        self.base_url = self.base_urls[0]
        self._replica_index = 0
        self.session_id = None

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

        errors: list[str] = []
        response = None
        for offset in range(len(self.base_urls)):
            idx = (self._replica_index + offset) % len(self.base_urls)
            base_url = self.base_urls[idx]
            url = f"{base_url}/{endpoint}"
            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=10
                )
                self._replica_index = idx
                self.base_url = base_url
                break
            except requests.exceptions.RequestException as e:
                errors.append(f"{base_url}: {e}")
                continue

        if response is None:
            raise Exception("All frontend replicas failed: " + " | ".join(errors))

        if response.status_code != 200:
            raise Exception(
                f"Request failed ({response.status_code}): {response.text}"
            )

        return response.json()

    # ---------------------------------
    # Session Handling
    # ---------------------------------

    def set_session(self, session_id: str):
        self.session_id = session_id

    def clear_session(self):
        self.session_id = None
