"""Repository extraction stays offline and respects each host's namespace shape."""
import pytest
from fastapi.testclient import TestClient
from doxograph import code_links, config, server, store


@pytest.mark.parametrize('address,expected', [
    ('https://gitlab.com/acme/platform/service', 'https://gitlab.com/acme/platform/service'),
    ('gitlab.com/acme/platform/deep/service', 'https://gitlab.com/acme/platform/deep/service'),
    ('https://gitlab.com/acme/platform/service/-/tree/main', 'https://gitlab.com/acme/platform/service'),
    ('https://gitlab.com/acme/platform/service/-/blob/main/train.py', 'https://gitlab.com/acme/platform/service'),
    ('https://gitlab.com/acme/platform/service/-/issues/42', 'https://gitlab.com/acme/platform/service'),
    ('https://gitlab.com/acme/platform/service.git.', 'https://gitlab.com/acme/platform/service'),
    ('https://GITLAB.COM/Acme/Platform/Service/-/tree/main', 'https://gitlab.com/Acme/Platform/Service'),
    ('https://gitlab.com/acme/service/-/tree/main', 'https://gitlab.com/acme/service'),
    ('https://github.com/acme/service/tree/main', 'https://github.com/acme/service'),
    ('https://github.com/acme/service/blob/main/train.py', 'https://github.com/acme/service'),
    ('https://bitbucket.org/acme/service/src/main', 'https://bitbucket.org/acme/service'),
    ('https://codeberg.org/acme/service/src/branch/main', 'https://codeberg.org/acme/service'),
])
def test_repository_path_uses_host_namespace_rules(address, expected):
    assert code_links.find('Code: ' + address) == [expected]


def test_nested_gitlab_paths_keep_order_case_and_deduplicate_only_identical_projects():
    assert code_links.find(' '.join([
        'https://gitlab.com/Acme/Platform/Service/-/tree/main',
        'https://github.com/acme/service',
        'https://gitlab.com/Acme/Platform/Service.git',
        'https://gitlab.com/Acme/Platform/service',
    ])) == ['https://gitlab.com/Acme/Platform/Service', 'https://github.com/acme/service',
            'https://gitlab.com/Acme/Platform/service']
    assert code_links.find('gitlab.com/acme github.com/acme https://example.org/acme/service') == []


def test_nested_gitlab_abstract_migration_and_listing_roundtrip_offline():
    link = 'https://gitlab.com/acme/platform/service'
    for key, legacy in [('new', False), ('legacy', True)]:
        paper = store.new_paper(key, title='Synthetic GitLab project', abstract='Code: ' + link + '/-/tree/main')
        if legacy:
            del paper['code']
        store.save_paper(paper)
        assert store.load_paper(key)['code'] == [link]
    config.set_ai_enabled(False)
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.get('/api/state')
        assert response.status_code == 200
        papers = response.json()['papers']
        # Listing carries the same complete project path through the store facade.
        assert {paper['key']:paper['code'] for paper in papers} == {'new':[link], 'legacy':[link]}
