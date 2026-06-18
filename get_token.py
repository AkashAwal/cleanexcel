"""
Run this script to get your Shopify Admin API access token.
It starts a local server, opens the OAuth page, and captures the token automatically.
"""
import http.server
import urllib.parse
import webbrowser
import requests
import threading
import json

SHOP             = "perfectbookhouse.myshopify.com"
CLIENT_ID        = "086182cb7d126493b327330c8c4f4179"
CLIENT_SECRET    = "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"
REDIRECT_URI     = "http://localhost:3000/callback"
SCOPES           = "read_products,write_products"

token_result = {}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]

        if code:
            # Exchange code for access token
            r = requests.post(
                f"https://{SHOP}/admin/oauth/access_token",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code}
            )
            data = r.json()
            token = data.get("access_token", "")
            token_result["token"] = token

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(f"""
                <h2>✅ Token captured!</h2>
                <p>Access token: <code>{token}</code></p>
                <p>You can close this window.</p>
            """.encode())

            # Save to file
            with open("shopify_token.txt", "w") as f:
                f.write(token)
            print(f"\n✅ Access token saved to shopify_token.txt")
            print(f"   Token: {token}")

            threading.Thread(target=server.shutdown).start()
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received.")

    def log_message(self, format, *args):
        pass  # suppress request logs

server = http.server.HTTPServer(("localhost", 3000), Handler)

oauth_url = (
    f"https://{SHOP}/admin/oauth/authorize"
    f"?client_id={CLIENT_ID}"
    f"&scope={SCOPES}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&state=bookimporter"
)

print("Opening Shopify OAuth page in your browser...")
print(f"If it doesn't open automatically, go to:\n{oauth_url}\n")
webbrowser.open(oauth_url)

print("Waiting for Shopify to redirect back...")
server.serve_forever()
