"""Literal code-server addresses are validated and stored without fetching them."""
import pytest
from fastapi.testclient import TestClient

from doxograph import code_links, server, store


@pytest.mark.parametrize('address', [
    'https://[2001:db8::1]/Repo?ref=A,B',
    'http://[::1]:8080/Repo',
    'https://User:Secret@[2001:DB8::1]:443/Repo?Token=Value#Part',
])
def test_ipv6_code_link_survives_offline_paper_patch(address):
    store.save_paper(store.new_paper('synthetic', title='Synthetic IPv6 link'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code': [address]})
    assert response.status_code == 200
    assert response.json()['code'] == [address]
    assert store.load_paper('synthetic')['code'] == [address]


@pytest.mark.parametrize('address', [
    'https://localhost/Repo', 'https://[not-ipv6]/Repo',
    'https://[2001:db8::1]:bad/Repo', 'https://[::1]:65536/Repo',
    'https://2001:db8::1/Repo', 'javascript://[::1]/Repo',
])
def test_ipv6_support_keeps_hostname_scheme_and_port_rejections(address):
    with pytest.raises(ValueError):
        code_links.normalize([address])


def test_ipv6_identity_folds_host_case_but_keeps_credentials_path_and_query_case():
    first = 'https://User:Secret@[2001:DB8::AB]:443/Repo?Token=Value#Part'
    duplicate = 'HTTPS://User:Secret@[2001:db8::ab]:443/Repo?Token=Value#Part'
    different = [first.replace('User:', 'user:'), first.replace('/Repo', '/repo'),
                 first.replace('Token=Value', 'Token=value'), first.replace(':443', ':444')]
    assert code_links.normalize([first, duplicate, *different]) == [first, *different]
