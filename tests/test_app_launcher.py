"""The desktop-window launcher's own logic: ports and the single-window rule. No window, no network."""

import socket
import threading

import pytest

from scripts import app


def taken_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


def test_the_preferred_port_is_used_when_it_is_free():
    s, port = taken_port()
    s.close()
    assert app.free_port(port) == port


def test_another_port_is_picked_when_the_preferred_one_is_taken():
    s, port = taken_port()
    try:
        chosen = app.free_port(port)
        assert chosen != port and chosen > 0
    finally:
        s.close()


@pytest.fixture
def instance_port(monkeypatch):
    s, port = taken_port()
    s.close()
    monkeypatch.setattr(app, "INSTANCE_PORT", port)
    return port


def test_with_no_window_open_nothing_answers(instance_port):
    assert app.already_open() is False


def test_a_second_launch_asks_the_open_window_to_come_forward(instance_port):
    shown = threading.Event()
    held = app.listen_for_second_launch(shown.set)
    try:
        assert held is not None and app.already_open() is True
        assert shown.wait(5)
    finally:
        held.close()


def test_only_one_window_can_hold_the_instance_port(instance_port):
    first = app.listen_for_second_launch(lambda: None)
    try:
        assert first is not None and app.listen_for_second_launch(lambda: None) is None
    finally:
        first.close()


def test_the_icon_is_a_real_ico_and_the_pages_link_it():
    data = app.ICON.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00" and int.from_bytes(data[4:6], "little") >= 4  # an .ico with several sizes
    from fastapi.testclient import TestClient

    from shelflife.web.app import app as web

    with TestClient(web) as client:
        html = client.get("/stats").text
        assert 'rel="icon" href="/static/shelflife.ico' in html
        assert client.get("/static/shelflife.ico").status_code == 200
