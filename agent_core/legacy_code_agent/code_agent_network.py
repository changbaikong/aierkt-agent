"""Host-side network proxy for code agents; sandbox processes remain offline."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlunsplit
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx


class NetworkPolicyError(ValueError):
    """A URL, address, redirect, or response violates the host proxy policy."""


class _PinnedHttpxClient:
    """Connect to an already-validated IP while preserving Host and TLS SNI."""

    def __init__(self, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "dm-code-agent/1"},
        )

    def get_pinned(self, url: str, addresses: tuple[str, ...], **kwargs: Any) -> Any:
        parsed = urlsplit(url)
        default_port = 443 if parsed.scheme == "https" else 80
        port = parsed.port or default_port
        host_header = parsed.hostname or ""
        if port != default_port:
            host_header = f"{host_header}:{port}"
        last_error: Exception | None = None
        for address in addresses:
            netloc_address = f"[{address}]" if ":" in address else address
            netloc = netloc_address if port == default_port else f"{netloc_address}:{port}"
            pinned_url = urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
            headers = dict(kwargs.get("headers") or {})
            headers["Host"] = host_header
            request = self._client.build_request("GET", pinned_url, headers=headers)
            request.extensions["sni_hostname"] = parsed.hostname
            try:
                return self._client.send(request, follow_redirects=False)
            except httpx.HTTPError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise NetworkPolicyError("host has no validated address")


class CodeAgentNetworkProxy:
    def __init__(
        self,
        search_backend: Any,
        http_client: Any | None = None,
        *,
        resolver: Callable[..., list[Any]] = socket.getaddrinfo,
        max_body_bytes: int = 512 * 1024,
        max_redirects: int = 3,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.search_backend = search_backend
        self.http_client = http_client or _PinnedHttpxClient(timeout_seconds)
        self.resolver = resolver
        self.max_body_bytes = max(1, int(max_body_bytes))
        self.max_redirects = max(0, int(max_redirects))
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    @staticmethod
    def _allowed_address(value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise NetworkPolicyError("DNS returned an invalid address") from exc
        return bool(address.is_global)

    def _validate_url(self, url: str) -> tuple[str, tuple[str, ...]]:
        if not isinstance(url, str) or not url.strip():
            raise NetworkPolicyError("URL is required")
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"}:
            raise NetworkPolicyError("only HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise NetworkPolicyError("URL user information is not allowed")
        if not parsed.hostname:
            raise NetworkPolicyError("URL host is required")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            try:
                answers = self.resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise NetworkPolicyError("host cannot be resolved") from exc
            addresses = {str(answer[4][0]) for answer in answers if len(answer) >= 5}
            if not addresses:
                raise NetworkPolicyError("host has no usable address")
        else:
            addresses = {str(literal)}
        if not all(self._allowed_address(address) for address in addresses):
            raise NetworkPolicyError("host resolves to a non-public address")
        return url.strip(), tuple(sorted(addresses))

    def search(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(10, int(max_results)))
        results = self.search_backend.search(
            query, max_results=limit, freshness="noLimit"
        )
        normalized = []
        for item in results[:limit]:
            url = str(getattr(item, "url", "") or "").strip()
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            normalized.append(
                {
                    "title": str(getattr(item, "title", "") or "")[:500],
                    "url": url,
                    "snippet": str(getattr(item, "snippet", "") or "")[:2000],
                    "summary": str(getattr(item, "summary", "") or "")[:4000],
                }
            )
        return {"query": query, "results": normalized, "result_count": len(normalized)}

    def read(self, url: str) -> dict[str, Any]:
        current, addresses = self._validate_url(url)
        for redirect_count in range(self.max_redirects + 1):
            request_options = {
                "follow_redirects": False,
                "timeout": self.timeout_seconds,
                "headers": {"Accept": "text/plain,text/html,application/json,application/xml;q=0.9"},
            }
            pinned = getattr(self.http_client, "get_pinned", None)
            if callable(pinned):
                response = pinned(current, addresses, **request_options)
            else:
                response = self.http_client.get(current, **request_options)
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= self.max_redirects:
                    raise NetworkPolicyError("redirect limit exceeded")
                location = response.headers.get("location")
                if not location:
                    raise NetworkPolicyError("redirect has no location")
                current, addresses = self._validate_url(urljoin(current, location))
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"web read failed with HTTP {response.status_code}")
            content_type = str(response.headers.get("content-type") or "").lower()
            allowed = (
                content_type.startswith("text/")
                or "application/json" in content_type
                or "application/xml" in content_type
                or "application/xhtml+xml" in content_type
                or "javascript" in content_type
            )
            if not allowed:
                raise NetworkPolicyError("response is not a supported text type")
            encoded = str(response.text).encode("utf-8")
            truncated = len(encoded) > self.max_body_bytes
            limited = encoded[: self.max_body_bytes]
            text = limited.decode("utf-8", errors="ignore")
            return {
                "url": current,
                "status_code": int(response.status_code),
                "content_type": content_type,
                "text": text,
                "bytes": len(limited),
                "truncated": truncated,
            }
        raise NetworkPolicyError("redirect limit exceeded")
