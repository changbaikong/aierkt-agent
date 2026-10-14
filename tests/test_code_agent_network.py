from __future__ import annotations

import socket

import pytest

from agent_core.legacy_code_agent.code_agent_network import CodeAgentNetworkProxy, NetworkPolicyError


class SearchBackend:
    def search(self, query, *, max_results, freshness="noLimit"):
        return [
            type(
                "Result",
                (),
                {"title": "Title", "url": "https://example.com/page", "snippet": "Snippet", "summary": ""},
            )()
        ]


class Response:
    def __init__(self, status_code=200, *, headers=None, text="body"):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/plain; charset=utf-8"}
        self.text = text


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


class PinnedClient(Client):
    def __init__(self, responses):
        super().__init__(responses)
        self.pins = []

    def get_pinned(self, url, addresses, **kwargs):
        self.pins.append(tuple(addresses))
        return self.get(url, **kwargs)


def public_resolver(host, port, type=socket.SOCK_STREAM):
    return [(socket.AF_INET, type, 6, "", ("93.184.216.34", port))]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
    ],
)
def test_network_proxy_rejects_unsafe_urls(url: str) -> None:
    proxy = CodeAgentNetworkProxy(SearchBackend(), Client([]))
    with pytest.raises(NetworkPolicyError):
        proxy.read(url)


def test_network_proxy_revalidates_redirect_target() -> None:
    client = Client(
        [Response(302, headers={"location": "http://127.0.0.1/private"}), Response()]
    )
    proxy = CodeAgentNetworkProxy(SearchBackend(), client, resolver=public_resolver)
    with pytest.raises(NetworkPolicyError):
        proxy.read("https://example.com/start")
    assert client.urls == ["https://example.com/start"]


def test_network_proxy_rejects_non_text_and_limits_body() -> None:
    binary = CodeAgentNetworkProxy(
        SearchBackend(),
        Client([Response(headers={"content-type": "application/octet-stream"})]),
        resolver=public_resolver,
    )
    with pytest.raises(NetworkPolicyError, match="text"):
        binary.read("https://example.com/file")

    limited = CodeAgentNetworkProxy(
        SearchBackend(),
        Client([Response(text="x" * 100)]),
        resolver=public_resolver,
        max_body_bytes=16,
    )
    result = limited.read("https://example.com/page")
    assert result["text"] == "x" * 16
    assert result["truncated"] is True


def test_network_proxy_search_normalizes_results() -> None:
    proxy = CodeAgentNetworkProxy(SearchBackend(), Client([]), resolver=public_resolver)
    result = proxy.search("python", max_results=3)
    assert result == {
        "query": "python",
        "results": [
            {
                "title": "Title",
                "url": "https://example.com/page",
                "snippet": "Snippet",
                "summary": "",
            }
        ],
        "result_count": 1,
    }


def test_network_proxy_rejects_private_dns_answer() -> None:
    def resolver(host, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, type, 6, "", ("192.168.1.20", port))]

    proxy = CodeAgentNetworkProxy(SearchBackend(), Client([]), resolver=resolver)
    with pytest.raises(NetworkPolicyError, match="address"):
        proxy.read("https://example.com/")


def test_network_proxy_pins_validated_address_for_connection() -> None:
    client = PinnedClient([Response()])
    proxy = CodeAgentNetworkProxy(SearchBackend(), client, resolver=public_resolver)

    proxy.read("https://example.com/")

    assert client.pins == [("93.184.216.34",)]
