"""
Non-secret QBO settings.

IMPORTANT:
1) For Sandbox:
   ENVIRONMENT = "sandbox"
   REDIRECT_URI = "http://localhost:8000/callback"

2) For Production:
   ENVIRONMENT = "production"
   REDIRECT_URI must EXACTLY match a Production Redirect URI configured
   in the Intuit Developer Portal. Intuit production apps generally require
   an HTTPS redirect URI.

Client ID and Client Secret are intentionally NOT stored here.
They are requested by popup every time the program runs.
"""

ENVIRONMENT = "production"  # "sandbox" or "production"
REDIRECT_URI = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"

# QBO query paging. Intuit allows up to 1000 entities per query response.
PAGE_SIZE = 1000

# Output file name
OUTPUT_CSV = "qbo_customers.csv"

# HTTP timeout for QBO API calls, in seconds.
REQUEST_TIMEOUT = 60
