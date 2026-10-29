"""Legacy process ownership probes never signal a Windows worker."""

import sys
from types import SimpleNamespace

import pytest

from doxograph import job_journal


@pytest.mark.parametrize(('status', 'alive'), [(0, False), (258, True)])
def test_windows_legacy_owner_is_queried_without_signaling(monkeypatch, tmp_path, status, alive):
    calls = []
    api = SimpleNamespace(
        SYNCHRONIZE=0x00100000, WAIT_OBJECT_0=0,
        OpenProcess=lambda access, inherit, pid: calls.append(('open', access, inherit, pid)) or 41,
        WaitForSingleObject=lambda handle, timeout: calls.append(('wait', handle, timeout)) or status,
        CloseHandle=lambda handle: calls.append(('close', handle)),
    )
    monkeypatch.setattr(job_journal, '_WINDOWS', True)
    monkeypatch.setitem(sys.modules, '_winapi', api)
    monkeypatch.setattr(job_journal.os, 'kill', lambda *args: pytest.fail('A liveness check sent a signal'))
    assert job_journal._owner_alive({'version': 1, 'pid': 123}, tmp_path) is alive
    assert calls == [('open', api.SYNCHRONIZE, False, 123), ('wait', 41, 0), ('close', 41)]


@pytest.mark.parametrize(('error', 'alive'), [(87, False), (5, True)])
def test_windows_legacy_owner_open_failure_is_conservative(monkeypatch, error, alive):
    def inaccessible(*args):
        failure = OSError('Process access failed')
        failure.winerror = error
        raise failure

    api = SimpleNamespace(SYNCHRONIZE=0x00100000, OpenProcess=inaccessible)
    monkeypatch.setattr(job_journal, '_WINDOWS', True)
    monkeypatch.setitem(sys.modules, '_winapi', api)
    assert job_journal._pid_alive(123) is alive


def test_windows_failed_owner_wait_still_closes_its_handle(monkeypatch):
    closed = []

    def failed_wait(*args):
        raise OSError('Cannot query process state')

    api = SimpleNamespace(
        SYNCHRONIZE=0x00100000, WAIT_OBJECT_0=0,
        OpenProcess=lambda *args: 41, WaitForSingleObject=failed_wait,
        CloseHandle=closed.append,
    )
    monkeypatch.setattr(job_journal, '_WINDOWS', True)
    monkeypatch.setitem(sys.modules, '_winapi', api)
    with pytest.raises(OSError, match='Cannot query process state'):
        job_journal._pid_alive(123)
    assert closed == [41]
