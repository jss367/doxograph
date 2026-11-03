"""Browser URL host checks without opening a URL or contacting a provider."""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from browser_support import browser_session, run_browser
from doxograph import code_links, server, store


HOSTS = [
    ('999.999.999.999', False), ('256.0.0.1', False), ('1.2.3.256', False),
    ('1.2.3.4.5', False), ('1.2.65536', False), ('127.0.0.08', False),
    ('0x100.0.0.1', False), ('example.123', False), ('1..2', False),
    ('[fe80::1%25eth0]', False), ('[fe80::1%eth0]', False),
    ('127.0.0.1', True), ('127.0.0.1.', True), ('127.1', True),
    ('0x7f.0.0.1', True), ('0177.0.0.1', True), ('1.2.65535', True),
    ('[2001:db8::1]', True), ('[::1]', True),
    ('example.org', True), ('123.example.org', True), ('999.999.example.org', True),
]


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_cached_browser_url_parser_agrees_without_fetching(engine):
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            actual = await page.evaluate('''hosts => hosts.map(host => {
              try { new URL('https://User:Secret@' + host + ':443/Repo?Token=Value#Part'); return true; }
              catch { return false; }
            })''', [host for host, _ in HOSTS])
            assert actual == [valid for _, valid in HOSTS]
    run_browser(scenario())


@pytest.mark.parametrize('host,valid', HOSTS)
def test_code_link_host_agrees_with_inert_browser_url_parser(host, valid):
    address = f'https://User:Secret@{host}:443/Repo?Token=Value#Part'
    # Node's WHATWG URL parser is inert; it does not fetch the address.
    result = subprocess.run(['node', '-e',
                             'try { new URL(process.argv[1]); process.exit(0); } catch { process.exit(1); }',
                             address], capture_output=True, timeout=5)
    assert (result.returncode == 0) is valid
    store.save_paper(store.new_paper('synthetic', title='Synthetic host validation'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code':[address]})
    assert response.status_code == (200 if valid else 422)
    assert store.load_paper('synthetic')['code'] == ([address] if valid else [])


def test_numeric_hosts_keep_existing_port_and_url_identity_rules():
    links = ['https://User:Secret@[2001:DB8::AB]:443/Repo?Token=Value#Part',
             'HTTPS://User:Secret@[2001:db8::ab]:443/Repo?Token=Value#Part',
             'https://user:Secret@[2001:db8::ab]:443/Repo?Token=Value#Part',
             'https://User:Secret@[2001:db8::ab]:443/repo?Token=Value#Part',
             'https://127.0.0.1:8080/Repo?Token=Value',
             'https://127.0.0.1:8080/Repo?Token=value']
    assert code_links.normalize(links) == [links[0], *links[2:]]
    for host in ['127.0.0.1', '[::1]']:
        for port in ['bad', '-1', '65536']:
            with pytest.raises(ValueError):
                code_links.normalize([f'https://{host}:{port}/Repo'])
    # The list is JSON-safe with the original spelling, without fetching it.
    assert json.loads(json.dumps(code_links.normalize(links)))[0] == links[0]
