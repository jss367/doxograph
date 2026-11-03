"""Host aliases share identity while the first original URL remains intact."""
import pytest
from fastapi.testclient import TestClient
from browser_support import browser_session, run_browser
from doxograph import code_links, config, server, store


@pytest.fixture(autouse=True)
def inert_identity_providers(monkeypatch):
    from doxograph import agent, chat, claude_code, codex_cli, extract
    config.set_ai_enabled(False)

    def blocked(*args, **kwargs):
        pytest.fail('Unexpected provider in inert code-link identity test')

    for module, name in ((agent, 'answer'), (chat, 'answer'), (claude_code, '_run'),
                         (codex_cli, '_run'), (extract, 'client')):
        monkeypatch.setattr(module, name, blocked)


HOST_GROUPS = [
    ['example.org', '%65xample.org', 'EXAMPLE.ORG', 'ｅｘａｍｐｌｅ.org', 'example%2eorg', 'example。org'],
    ['bücher.de', 'b%C3%BCcher.de', 'xn--bcher-kva.de', 'XN--BCHER-KVA.DE'],
    ['faß.de', 'fa%C3%9F.de', 'xn--fa-hia.de'],
    ['_bücher.de', 'xn--_bcher-4ya.de'],
    ['☃.net', 'xn--n3h.net'],
    ['[2001:db8::1]', '[2001:0DB8:0000:0000:0000:0000:0000:0001]'],
    ['[::ffff:127.0.0.1]', '[0:0:0:0:0:ffff:7f00:1]'],
    ['127.0.0.1', '127.1', '0177.0.0.1', '0x7f.0.0.1', '0x7f000001', '2130706433',
     '%31%32%37.0.0.1', '１２７.0.0.1', '127.0.0.1.'],
    ['0.0.0.0', '0', '0x0', '00'],
]


@pytest.mark.parametrize('hosts', HOST_GROUPS)
def test_alias_group_keeps_first_original_url_in_offline_paper_storage(hosts):
    links = [f'https://User:Secret@{host}:443/Repo?Token=Value#Part' for host in hosts]
    assert code_links.normalize(links) == [links[0]]
    assert code_links.normalize(list(reversed(links))) == [links[-1]]
    store.save_paper(store.new_paper('synthetic', title='Synthetic host identity'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code':links})
    assert response.status_code == 200
    assert response.json()['code'] == [links[0]]
    assert store.load_paper('synthetic')['code'] == [links[0]]


def test_only_host_aliases_fold_and_other_url_components_remain_distinct():
    first = 'https://User:Secret@%65xample.org:443/Repo?Token=Value#Part'
    duplicate = first.replace('%65xample.org', 'EXAMPLE.ORG')
    distinct = [first.replace('User:', 'user:'), first.replace(':Secret@', ':secret@'),
                first.replace('/Repo', '/repo'), first.replace('Token=', 'token='),
                first.replace('Value', 'value'), first.replace('#Part', '#part'),
                first.replace(':443', ':444'), first.replace('https:', 'http:')]
    assert code_links.normalize([first, duplicate, *distinct]) == [first, *distinct]
    # DNS-like trailing dots and distinct IDNs are not URL host aliases.
    distinct_hosts = ['example.org', 'example.org.', 'faß.de', 'fass.de', '127.0.0.1', '[::ffff:127.0.0.1]']
    links = [f'https://{host}/Repo' for host in distinct_hosts]
    assert code_links.normalize(links) == links


def test_duplicate_aliases_do_not_exhaust_the_link_limit_but_distinct_links_do():
    domain = 'example.org'
    links = ['https://' + ''.join(f'%{ord(char):02x}' if mask & (1 << index) else char
                                  for index, char in enumerate(domain)) + '/Repo' for mask in range(25)]
    assert code_links.normalize(links) == [links[0]]
    with pytest.raises(ValueError, match='at most 20'):
        code_links.normalize([f'https://example.org/Repo{i}' for i in range(21)])
    with pytest.raises(ValueError, match='not a web address'):
        code_links.normalize([*links, 'https://a%2fb.com/Repo'])


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_supported_browsers_agree_each_identity_group_has_one_host_without_fetching(engine):
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            groups = await page.evaluate('groups => groups.map(hosts => [...new Set(hosts.map(host => new URL("https://" + host + "/Repo").hostname))])', HOST_GROUPS)
            assert [len(group) for group in groups] == [1] * len(HOST_GROUPS)
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_paper_link_displays_first_spelling_without_opening_the_external_address(engine):
    from playwright.async_api import expect
    from test_ask_browser import _server
    first = 'https://%65XAMPLE.org/Repo?Token=Value#Part'
    store.save_paper(store.new_paper('synthetic', title='Synthetic displayed link'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        assert client.patch('/api/papers/synthetic', json={'code':[first, first.replace('%65XAMPLE.org', 'example.org')]}).status_code == 200

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url+'/#paper=synthetic')
                link = page.locator('.paper-code a')
                await expect(link).to_have_count(1)
                await expect(link).to_have_attribute('href', first)
                await expect(link).to_have_text(first.removeprefix('https://'))
                assert store.load_paper('synthetic')['code'] == [first]
    run_browser(scenario())
