import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
        ]
    )
    return urlunsplit((scheme, hostname, path, query, ""))


def is_allowed_public_url(url: str, allowed_domains: set[str]) -> bool:
    try:
        parsed = urlsplit(url.strip())
        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return False
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address and not address.is_global:
            return False
        return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)
    except (TypeError, ValueError):
        return False
