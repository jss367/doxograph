"""Offline validation of parsed ports and case-sensitive URL components."""
import pytest
from fastapi.testclient import TestClient

from doxograph import code_links, server, store


@pytest.mark.parametrize('port', ['bad', '-1', '65536', '80.5', '1:2'])
def test_invalid_parsed_port_is_rejected_by_paper_api(port):
    store.save_paper(store.new_paper('synthetic', title='Synthetic port control'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code': ['https://example.org:' + port + '/repo']})
    assert response.status_code == 422
    assert store.load_paper('synthetic')['code'] == []


def test_only_scheme_and_hostname_case_are_folded_for_deduplication():
    links = ['https://User:Secret@EXAMPLE.org/Repo?Token=Value#Part',
             'HTTPS://User:Secret@example.ORG/Repo?Token=Value#Part',
             'https://user:Secret@example.org/Repo?Token=Value#Part',
             'https://User:secret@example.org/Repo?Token=Value#Part',
             'https://User:Secret@example.org/repo?Token=Value#Part',
             'https://User:Secret@example.org/Repo?Token=value#Part',
             'https://User:Secret@example.org/Repo?token=Value#Part',
             'https://User:Secret@example.org/Repo?Token=Value#part']
    expected = [links[0], *links[2:]]
    store.save_paper(store.new_paper('synthetic', title='Synthetic URL case control'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code': links})
        assert response.status_code == 200
        assert response.json()['code'] == expected
    assert store.load_paper('synthetic')['code'] == expected


def test_query_slashes_and_case_distinct_repository_paths_are_preserved():
    assert code_links.normalize(['https://example.org/Repo?token=/', 'https://example.org/Repo?token=']) == [
        'https://example.org/Repo?token=/', 'https://example.org/Repo?token=']
    assert code_links.find('gitlab.com/Org/Repo and GITLAB.COM/Org/repo') == [
        'https://gitlab.com/Org/Repo', 'https://gitlab.com/Org/repo']


def test_valid_explicit_ports_are_kept_without_fetching_them():
    links = ['https://example.org:443/Repo', 'http://example.org:8080/Repo']
    assert code_links.normalize(links) == links


def test_trailing_path_slashes_are_distinct_in_offline_api_storage():
    links = ['https://example.org/Repo', 'https://example.org/Repo/', 'https://example.org/Repo//']
    store.save_paper(store.new_paper('synthetic', title='Synthetic path control'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code': links})
        assert response.status_code == 200
        assert response.json()['code'] == links
    assert store.load_paper('synthetic')['code'] == links
