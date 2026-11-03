"""Encoded/Unicode hosts use inert browser parsing, never URL fetching."""
import subprocess

import pytest
from fastapi.testclient import TestClient
from browser_support import browser_session, run_browser
from doxograph import code_links, server, store


HOSTS = [
    ('exa%mple.com', False), ('a%2Fb.com', False), ('a%00b.com', False),
    ('a%5Cb.com', False), ('a%23b.com', False), ('a%7Fb.com', False),
    ('a%1Fb.com', False), ('%FF.com', False), ('%C3%28.com', False),
    ('example%252eorg', False), ('a%25b.com', False), ('a%3Ab.com', False),
    ('%39%39%39.0.0.1', False), ('９９９.0.0.1', False),
    ('xn--.com', True), ('xn--a.com', True), ('xn--abc-.com', True),
    ('a\u200db.com', False), ('\u0301a.com', False), ('אa.com', False),
    ('%65xample.com', True), ('example%2eorg', True),
    ('b%C3%BCcher.de', True), ('bücher.de', True), ('faß.de', True),
    ('ＦＯＯ.com', True), ('example。org', True), ('%31%32%37.0.0.1', True),
    ('１２７.0.0.1', True), ('foo_bar.com', True), ('a..b.com', True),
    ('-a.com', True), ('a-.com', True), ('_bücher.de', True), ('☃.net', True),
    ('xn--n3h.net', True), ('א.com', True), ('[2001:db8::1]', True),
]


@pytest.mark.parametrize('host,valid', HOSTS)
def test_encoded_host_offline_api_matches_url_constructor(host, valid):
    address = f'https://User:Secret@{host}:443/Repo?Token=Value#Part'
    result = subprocess.run(['node', '-e',
                             'try { new URL(process.argv[1]); process.exit(0); } catch { process.exit(1); }',
                             address], capture_output=True, timeout=5)
    # Both supported cached browsers accept these ACE spellings, while this
    # Node build rejects them. Preserve existing browser-accepted inputs.
    node_valid = valid and host not in {'xn--.com', 'xn--a.com', 'xn--abc-.com'}
    assert (result.returncode == 0) is node_valid
    store.save_paper(store.new_paper('synthetic', title='Synthetic encoded domain'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code':[address]})
    assert response.status_code == (200 if valid else 422)
    assert store.load_paper('synthetic')['code'] == ([address] if valid else [])


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_cached_browsers_parse_encoded_and_unicode_hosts_offline(engine):
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            actual = await page.evaluate('''hosts => hosts.map(host => {
              try { new URL('https://User:Secret@' + host + ':443/Repo?Token=Value#Part'); return true; }
              catch { return false; }
            })''', [host for host, _ in HOSTS])
            assert actual == [valid for _, valid in HOSTS], [(host, value, valid) for (host, valid), value in zip(HOSTS, actual, strict=True) if value != valid]
    run_browser(scenario())


def test_decoding_for_validation_keeps_stored_url_identity_components():
    links = ['https://User:Secret@%65XAMPLE.org/Repo?Token=Value#Part',
             'HTTPS://User:Secret@%65xample.ORG/Repo?Token=Value#Part',
             'https://user:Secret@%65xample.org/Repo?Token=Value#Part',
             'https://User:Secret@%65xample.org/repo?Token=Value#Part',
             'https://User:Secret@%65xample.org/Repo?Token=value#Part']
    assert code_links.normalize(links) == [links[0], *links[2:]]
