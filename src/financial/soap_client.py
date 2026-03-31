from zeep import Client, Transport
from requests import Session
from requests.exceptions import RequestException


class SOAPClient:
    """
    Financial SOAP client for PA2.
    Creates the Zeep client once and reuses it.
    """

    def __init__(self, wsdl_url: str):
        session = Session()

        transport = Transport(session=session, timeout=5, operation_timeout=5)

        self.client = Client(
            wsdl=wsdl_url,
            transport=transport
        )

    def process_payment(self, username, card_number, expiration, security_code):
        try:
            result = self.client.service.process(
                username,
                card_number,
                expiration,
                security_code
            )

            return result  # "Yes" or "No"

        except RequestException:
            return "No"

        except Exception:
            return "No"
