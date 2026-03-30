import random
import os

from spyne import Application, rpc, ServiceBase, Unicode
from spyne.protocol.soap import Soap11
from spyne.server.wsgi import WsgiApplication


class FinancialService(ServiceBase):

    @rpc(
        Unicode,  # username
        Unicode,  # card_number
        Unicode,  # expiration
        Unicode,  # security_code
        _returns=Unicode
    )
    def process(ctx, username, card_number, expiration, security_code):
        """
        Simulates a financial transaction.

        Returns:
            "Yes" with 90% probability
            "No" with 10% probability
        """

        # Basic validation (optional but safer)
        if not username or not card_number:
            return "No"

        # 90% success probability
        return "Yes" if random.random() < 0.9 else "No"


# SOAP Application
application = Application(
    [FinancialService],
    tns='financial',
    in_protocol=Soap11(),
    out_protocol=Soap11()
)

wsgi_app = WsgiApplication(application)


# -------------------------------------------------
# Standalone Execution (VM Deployment)
# -------------------------------------------------

def main() -> None:
    import argparse
    from src.common.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_config(args.config)

    from wsgiref.simple_server import make_server
    host = cfg.soap.host
    port = cfg.soap.port

    server = make_server(host, port, wsgi_app)
    print(f"SOAP Financial Service running on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()