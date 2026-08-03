"""Shared credential check for the site-wide Basic Auth gate - used by
both core.middleware.BasicAuthMiddleware (regular HTTP) and
TrackingSessionConsumer.connect() (WebSocket, which bypasses Django's
MIDDLEWARE entirely). Kept in one place since it's the one bit of
security-sensitive comparison logic in the app.
"""
import base64
import binascii
import hmac

from django.conf import settings


def check_basic_auth(header_value):
    """True if auth is disabled (no SITE_BASIC_AUTH_USER configured) or
    header_value is a valid `Basic <base64>` Authorization header matching
    the configured shared credential."""
    if not settings.SITE_BASIC_AUTH_USER:
        return True
    if not header_value or not header_value.startswith('Basic '):
        return False
    try:
        decoded = base64.b64decode(header_value[6:]).decode('utf-8')
        username, password = decoded.split(':', 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return (
        hmac.compare_digest(username, settings.SITE_BASIC_AUTH_USER)
        and hmac.compare_digest(password, settings.SITE_BASIC_AUTH_PASSWORD)
    )
