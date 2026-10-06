"""Isolated network checks for the sanea UDP discovery listener."""

import socket
import threading
from collections.abc import Callable

import pytest

from sanea.network.discovery import (
    DISCOVERY_REQUEST,
    REGISTRATION_REQUEST,
    DiscoveryServer,
)


def _run_server(
    registration_available: Callable[[], bool],
) -> tuple[DiscoveryServer, threading.Thread]:
    server = DiscoveryServer(
        "127.0.0.1",
        0,
        18_443,
        registration_available,
    )
    server.prepare()
    thread = threading.Thread(target=server.serve)
    thread.start()
    return server, thread


def _request(server: DiscoveryServer, payload: bytes) -> bytes:

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(0.3)
        client.sendto(payload, server.bind_addr)
        response, sender = client.recvfrom(257)

    assert sender[0] == "127.0.0.1"
    return response


def _stop_server(server: DiscoveryServer, thread: threading.Thread) -> None:
    server.stop()
    thread.join(1)
    assert not thread.is_alive()


def test_discovery_returns_compact_https_port() -> None:
    server, thread = _run_server(lambda: False)
    try:
        response = _request(server, DISCOVERY_REQUEST)
    finally:
        _stop_server(server, thread)

    assert response == b"{\"port\":18443}"


@pytest.mark.parametrize("available", [False, True])
def test_registration_discovery_requires_available_registration(
    available: bool,
) -> None:
    server, thread = _run_server(lambda: available)
    try:

        if available:
            assert _request(server, REGISTRATION_REQUEST) == b"{\"port\":18443}"

        else:

            with pytest.raises(TimeoutError):
                _request(server, REGISTRATION_REQUEST)
    finally:
        _stop_server(server, thread)


def test_discovery_ignores_unknown_and_oversized_requests() -> None:
    server, thread = _run_server(lambda: False)
    try:

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.sendto(b"UNKNOWN", server.bind_addr)
            client.sendto(b"X" * 65, server.bind_addr)

        assert _request(server, DISCOVERY_REQUEST) == b"{\"port\":18443}"
    finally:
        _stop_server(server, thread)
