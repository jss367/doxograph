"""Encoded/Unicode hosts use inert browser parsing, never URL fetching."""
import subprocess

import pytest
from fastapi.testclient import TestClient
from browser_support import browser_session, run_browser
from doxograph import code_links, server, store
from test_code_link_host_identity import inert_identity_providers as inert_identity_providers


HOSTS = [
    ('exa%mple.com', False), ('a%2Fb.com', False), ('a%00b.com', False),
    ('a%5Cb.com', False), ('a%23b.com', False), ('a%7Fb.com', False),
    ('a%1Fb.com', False), ('%FF.com', False), ('%C3%28.com', False),
    ('example%252eorg', False), ('a%25b.com', False), ('a%3Ab.com', False),
    ('%39%39%39.0.0.1', False), ('９９９.0.0.1', False),
    ('xn--.com', False), ('xn--a.com', False), ('xn--abc-.com', False),
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
    # Node/ICU versions disagree on these ACE spellings. The application policy
    # is checked below and in both supported browsers, independently of Node.
    if host not in {'xn--.com', 'xn--a.com', 'xn--abc-.com'}:
        assert (result.returncode == 0) is valid
    store.save_paper(store.new_paper('synthetic', title='Synthetic encoded domain'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code':[address]})
    assert response.status_code == (200 if valid else 422)
    assert store.load_paper('synthetic')['code'] == ([address] if valid else [])


@pytest.mark.parametrize('node_exit', [0, 1])
def test_ace_application_policy_is_independent_of_node_version(monkeypatch, node_exit):
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, node_exit))
    test_encoded_host_offline_api_matches_url_constructor('xn--abc-.com', False)


def test_node_version_exception_does_not_mask_malformed_host_rejection(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0))
    with pytest.raises(AssertionError):
        test_encoded_host_offline_api_matches_url_constructor('a%2Fb.com', False)
    with pytest.raises(ValueError):
        code_links.normalize(['https://a%2Fb.com/Repo'])


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
            # Node and browser/ICU builds disagree on raw parsing of malformed
            # ACE. Production A-label validation is independently strict.
            variable_ace = {'xn--.com', 'xn--a.com', 'xn--abc-.com'}
            print('ACE constructor behavior', engine, {host:value for (host, _), value in zip(HOSTS, actual, strict=True) if host in variable_ace})
            for (host, valid), value in zip(HOSTS, actual, strict=True):
                if host not in variable_ace:
                    assert value is valid, host
    run_browser(scenario())


@pytest.mark.parametrize('host', ['xn--a.com', '%78n--a.com', 'xn--.com', 'xn--abc-.com'])
def test_malformed_alabel_is_rejected_after_idna_decoding(host):
    with pytest.raises(ValueError):
        code_links.normalize([f'https://{host}/Repo'])


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('host', ['xn--a.com', 'xn--.com', 'xn--abc-.com'])
def test_editor_rejects_malformed_alabel_and_keeps_the_original_record(engine, host):
    from playwright.async_api import expect
    from test_ask_browser import _server
    store.save_paper(store.new_paper('synthetic', title='Synthetic A-label editor', code=['https://example.org/Repo']))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url+'/#paper=synthetic')
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Code links', exact=True).click()
                address = f'https://{host}/Repo'
                await page.locator('#ask-lines').fill(address)
                await page.locator('#ask').get_by_role('button', name='OK', exact=True).click()
                await expect(page.locator('#ask-note')).to_contain_text('Could not save')
                await expect(page.locator('#ask-lines')).to_have_value(address)
                assert store.load_paper('synthetic')['code'] == ['https://example.org/Repo']
                await expect(page.locator('.paper-code a')).to_have_attribute('href', 'https://example.org/Repo')
    run_browser(scenario())


def test_decoding_for_validation_keeps_stored_url_identity_components():
    links = ['https://User:Secret@%65XAMPLE.org/Repo?Token=Value#Part',
             'HTTPS://User:Secret@%65xample.ORG/Repo?Token=Value#Part',
             'https://user:Secret@%65xample.org/Repo?Token=Value#Part',
             'https://User:Secret@%65xample.org/repo?Token=Value#Part',
             'https://User:Secret@%65xample.org/Repo?Token=value#Part']
    assert code_links.normalize(links) == [links[0], *links[2:]]
