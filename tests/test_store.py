import pytest

from doxograph import store


def test_citekey_skips_stopwords_and_uses_surname():
    key = store.citekey("On the Measurement of Steering Resistance", ["Jane Q. Doe", "Bo Li"], 2026)
    assert key == "doe2026measurement"


def test_citekey_handles_comma_names_and_missing_year():
    assert store.citekey("A Study", ["Doe, Jane"], None) == "doendstudy"


def test_unique_key_suffixes_on_collision():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    assert store.unique_key("doe2026study") == "doe2026studya"


def test_claim_lifecycle_and_status():
    paper = store.new_paper("doe2026study", title="A Study")
    store.save_paper(paper)
    assert store.load_paper("doe2026study")["status"] == "fetched"

    created = store.add_claim("doe2026study", {"text": "A holds for B.", "tags": ["alpha"]})
    assert store.load_paper("doe2026study")["status"] == "reviewed"  # hand-written claims are reviewed

    store.update_claim("doe2026study", created["id"], {"reviewed": False})
    assert store.load_paper("doe2026study")["status"] == "extracted"

    store.update_claim("doe2026study", created["id"], {"text": "Edited.", "bogus": 1})
    reloaded = store.load_paper("doe2026study")["claims"][0]
    assert reloaded["text"] == "Edited."
    assert "bogus" not in reloaded

    store.delete_claim("doe2026study", created["id"])
    assert store.load_paper("doe2026study")["claims"] == []

    with pytest.raises(KeyError):
        store.delete_claim("doe2026study", "missing")


def test_claim_ids_stay_unique_after_deletion():
    store.save_paper(store.new_paper("doe2026study"))
    first = store.add_claim("doe2026study", {"text": "One."})
    second = store.add_claim("doe2026study", {"text": "Two."})
    store.delete_claim("doe2026study", first["id"])
    third = store.add_claim("doe2026study", {"text": "Three."})
    assert third["id"] != second["id"]


def test_rename_tag_rewrites_claims_and_merges():
    store.add_tag("alpha", "first")
    store.add_tag("beta", "second")
    store.save_paper(store.new_paper("doe2026study"))
    store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha", "beta"]})

    store.rename_tag("alpha", "beta")
    claim = store.load_paper("doe2026study")["claims"][0]
    assert claim["tags"] == ["beta"]
    assert "alpha" not in store.tag_names()


def test_delete_tag_strips_it_from_claims():
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study"))
    store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha"]})
    store.delete_tag("alpha")
    assert store.load_paper("doe2026study")["claims"][0]["tags"] == []
    assert store.tag_names() == []


def test_tag_counts_ranks_by_use():
    store.save_paper(store.new_paper("doe2026study"))
    store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha", "beta"]})
    store.add_claim("doe2026study", {"text": "Y.", "tags": ["beta"]})
    assert list(store.tag_counts(store.claim_rows())) == ["beta", "alpha"]
