import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from intuitlib.client import AuthClient
from intuitlib.enums import Scopes

from credentials_ui import prompt_for_callback_url


class OAuthError(RuntimeError):
    pass


class _CallbackState:
    callback_url = None
    event = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "localhost")
        _CallbackState.callback_url = f"http://{host}{self.path}"
        _CallbackState.event.set()

        body = (
            "<html><body style='font-family:Segoe UI,Arial,sans-serif;padding:40px;'>"
            "<h2>QuickBooks authorization received.</h2>"
            "<p>You can close this browser tab and return to the program.</p>"
            "</body></html>"
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress the default local HTTP server console logging.
        return


def _parse_callback(callback_url, expected_state):
    parsed = urlparse(callback_url)
    params = parse_qs(parsed.query)

    if "error" in params:
        error = params.get("error", ["Unknown OAuth error"])[0]
        description = params.get("error_description", [""])[0]
        raise OAuthError(f"Intuit OAuth error: {error}. {description}".strip())

    code = params.get("code", [None])[0]
    realm_id = params.get("realmId", [None])[0]
    returned_state = params.get("state", [None])[0]

    if not code:
        raise OAuthError("OAuth callback did not contain an authorization code.")
    if not realm_id:
        raise OAuthError("OAuth callback did not contain a QBO realmId/company ID.")
    if expected_state and returned_state != expected_state:
        raise OAuthError("OAuth state validation failed. Authorization was stopped for safety.")

    return code, realm_id


def _is_local_redirect(redirect_uri):
    parsed = urlparse(redirect_uri)
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}


def _wait_for_local_callback(redirect_uri, timeout_seconds=180):
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    _CallbackState.callback_url = None
    _CallbackState.event.clear()

    server = HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        if not _CallbackState.event.wait(timeout_seconds):
            raise OAuthError(
                f"Timed out waiting for OAuth callback after {timeout_seconds} seconds."
            )
        return _CallbackState.callback_url
    finally:
        server.shutdown()
        server.server_close()


def authorize(client_id, client_secret, redirect_uri, environment):
    """
    Run Intuit OAuth 2.0 authorization.

    Returns:
        auth_client: authorized Intuit AuthClient
        realm_id: QBO company/realm ID
    """
    auth_client = AuthClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        environment=environment,
    )

    authorization_url = auth_client.get_authorization_url([Scopes.ACCOUNTING])
    expected_state = auth_client.state_token

    if _is_local_redirect(redirect_uri):
        # Start the callback server first, then open the Intuit authorization page.
        parsed = urlparse(redirect_uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 80

        _CallbackState.callback_url = None
        _CallbackState.event.clear()

        server = HTTPServer((host, port), _CallbackHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            webbrowser.open(authorization_url, new=2)
            if not _CallbackState.event.wait(180):
                raise OAuthError("Timed out waiting for QuickBooks authorization.")
            callback_url = _CallbackState.callback_url
        finally:
            server.shutdown()
            server.server_close()
    else:
        # Production-friendly fallback:
        # Intuit redirects to the configured HTTPS URI, and the user pastes the
        # resulting full browser URL into a popup.
        webbrowser.open(authorization_url, new=2)
        callback_url = prompt_for_callback_url()

    auth_code, realm_id = _parse_callback(callback_url, expected_state)

    # Exchange authorization code for bearer tokens.
    auth_client.get_bearer_token(auth_code, realm_id=realm_id)

    if not auth_client.access_token:
        raise OAuthError("Intuit did not return an access token.")

    return auth_client, realm_id
