"""Exercise real pytest collection and timeout reporting in isolated subprocesses."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


CONFT = Path(__file__).with_name('conftest.py')


def run_pytest(directory, *args):
    return subprocess.run(
        [sys.executable, '-m', 'pytest', '-c', str(directory / 'pytest.ini'), *args],
        cwd=directory, env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
        capture_output=True, text=True, timeout=15,
    )


@pytest.fixture
def collection_suite(tmp_path):
    (tmp_path / 'pytest.ini').write_text('[pytest]\nmarkers =\n    browser: browser test\n')
    (tmp_path / 'conftest.py').write_text(CONFT.read_text() + '''

def pytest_collection_finish(session):
    from pathlib import Path
    Path('selected.txt').write_text('\\n'.join(item.nodeid for item in session.items))
''')
    for i in range(7):
        (tmp_path / f'test_module_{i}.py').write_text('''
import pytest

@pytest.mark.parametrize('value', range(3))
def test_python(value):
    pass

@pytest.mark.browser
def test_browser():
    pass
''')
    return tmp_path


@pytest.mark.parametrize('suite,mark', [('python', 'not browser'), ('browser', 'browser')])
def test_shards_cover_each_selected_test_once(collection_suite, suite, mark):
    directory = collection_suite

    def collect(*args):
        result = run_pytest(directory, '--collect-only', '-q', '-m', mark, *args)
        assert result.returncode == 0, result.stdout + result.stderr
        return (directory / 'selected.txt').read_text().splitlines()

    whole = collect()
    shards = [collect(f'--{suite}-shard={i}/4') for i in range(1, 5)]
    assert sorted(node for shard in shards for node in shard) == sorted(whole)
    assert all(shards)
    if suite == 'python':
        modules = [{node.split('::')[0] for node in shard} for shard in shards]
        assert sum(map(len, modules)) == len(set.union(*modules))
    else:
        # Preserve the established browser distribution.
        assert shards == [sorted(whole)[i::4] for i in range(4)]


@pytest.mark.parametrize('args', [
    ['--python-shard=0/4'], ['--python-shard=5/4'], ['--python-shard=1/0'],
    ['--python-shard=invalid'], ['--python-shard=1/4', '--browser-shard=1/4'],
    ['--python-shard=1/4', '-m', 'browser'],
    ['--browser-shard=1/4', '-m', 'not browser'],
    ['--python-shard=1/4'],  # An unfiltered collection includes browser tests.
])
def test_invalid_shards_fail_instead_of_silently_omitting_tests(collection_suite, args):
    result = run_pytest(collection_suite, '--collect-only', '-q', *args)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert 'ERROR:' in result.stderr


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
def test_timeout_retains_prior_timings_and_names_the_stalled_test(tmp_path, phase):
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    (tmp_path / 'test_stall.py').write_text(f'''
import time
import pytest

def stall():
    time.sleep(30)

@pytest.fixture
def held():
    if {phase!r} == 'setup':
        stall()
    yield
    if {phase!r} == 'teardown':
        stall()

def test_completed():
    pass

def test_stalled(held):
    if {phase!r} == 'call':
        stall()
''')
    result = run_pytest(tmp_path, '-p', 'pytest_timeout', '-p', 'pytest_reportlog',
                        '-v', '--timeout=0.3', '--timeout-method=thread', '--report-log=events.jsonl')
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert 'test_stalled' in output and 'Timeout' in output and 'stall' in output
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert any(event.get('nodeid') == 'test_stall.py::test_completed'
               and event.get('when') == 'call' and event.get('outcome') == 'passed'
               and event.get('duration', -1) >= 0 for event in events)
