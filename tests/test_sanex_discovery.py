"""Tests for local-network sanea endpoint discovery."""

import asyncio
import json
from typing import cast

import pytest

from sanex.sync.discovery import DiscoveryMode, SaneaDiscovery


class DiscoveryResponder(asyncio.DatagramProtocol):
    """Record a request and return configured datagrams to its sender."""

    def __init__(self, responses: tuple[bytes, ...]) -> None:
        self.responses = responses
        self.requests: list[bytes] = []
        self.received = asyncio.Event()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        self.requests.append(data)
        assert self.transport is not None

        for response in self.responses:
            self.transport.sendto(response, address)

        self.received.set()


async def responder(
    responses: tuple[bytes, ...],
) -> tuple[asyncio.DatagramTransport, DiscoveryResponder, int]:
    """Bind a disposable loopback responder and return its assigned port."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: DiscoveryResponder(responses),
        local_addr=("127.0.0.1", 0),
    )
    port = transport.get_extra_info("sockname")[1]
    return transport, protocol, port


@pytest.mark.parametrize(
    ("mode", "expected_request"),
    [
        (DiscoveryMode.SYNC, b"SANEA-DISCOVER"),
        (DiscoveryMode.REGISTRATION, b"SANEA-REGISTER"),
    ],
)
def test_discovers_first_valid_response(mode: DiscoveryMode, expected_request: bytes) -> None:
    asyncio.run(scenario_discovers_first_valid_response(mode, expected_request))


async def scenario_discovers_first_valid_response(
    mode: DiscoveryMode,
    expected_request: bytes,
) -> None:
    responses = (
        b"not-json",
        json.dumps({"port": 8_443}).encode(),
    )
    transport, protocol, port = await responder(responses)
    try:
        endpoint = await SaneaDiscovery(
            port=port,
            timeout=1,
            targets=("127.0.0.1",),
        ).discover(mode)
        await asyncio.wait_for(protocol.received.wait(), 1)
    finally:
        transport.close()

    assert protocol.requests == [expected_request]
    assert endpoint is not None
    assert endpoint.host == "127.0.0.1"
    assert endpoint.port == 8_443
    assert endpoint.base_url == "https://127.0.0.1:8443"
