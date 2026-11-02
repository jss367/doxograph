"""Opt-in pytest guard: -p no_live_providers.

Keep Python tests offline while allowing mock transports and real HTTP servers
opened by the current test. This guards this process, not child interpreters.
"""
import ipaddress
import socket
import subprocess
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def no_live_providers(monkeypatch):
    run = subprocess.run

    def guarded_run(args, *a, **kw):
        command = args[0] if isinstance(args, (list, tuple)) else args.split()[0]
        if Path(command).name in ('codex', 'claude'):
            pytest.fail('Live model CLI invocation blocked; mock the provider in this test')
        return run(args, *a, **kw)

    monkeypatch.setattr(subprocess, 'run', guarded_run)
    listening = set()
    listen = socket.socket.listen
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded_listen(self, *args, **kwargs):
        result = listen(self, *args, **kwargs)
        address = self.getsockname()
        if isinstance(address, tuple):
            listening.add(address[1])
        return result

    def check(address):
        if not isinstance(address, tuple):
            return
        host, port = address[:2]
        try:
            local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local or port not in listening:
            pytest.fail("Network connection outside this test's listening sockets blocked")

    def guarded_connect(self, address):
        check(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        check(address)
        return connect_ex(self, address)

    monkeypatch.setattr(socket.socket, 'listen', guarded_listen)
    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
