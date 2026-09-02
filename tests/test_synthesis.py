"""Syntheses: what the papers, taken together, hold on a topic."""

from fastapi.testclient import TestClient

from doxograph import export, extract, server, store, __main__


def _paper(key, title, author, year, *claims):
    paper = store.new_paper(key, title=title, authors=[author], year=year)
    store.save_paper(paper)
    ids = []
    for text, tags in claims:
        ids.append(store.add_claim(key, {"text": text, "tags": tags, "reviewed": True})["id"])
    return ids


def build_corpus():
    store.add_tag("recovery-rate", "How often a model returns to task.")
    a, = _paper("doe2026recovery", "Recovery under steering", "Jane Doe", 2026,
                ("Llama-3 70B recovers in 46% of rollouts.", ["recovery-rate"]))
    b, c = _paper("li2025steer", "Steering does not wash out", "Bo Li", 2025,
                  ("Steered models almost never return to the task.", ["recovery-rate"]),
                  ("Recovery is unaffected by scale.", ["recovery-rate", "scaling"]))
    return a, b, c


def shown(topic):
    return {r["id"]: r for r in store.topic_claims(topic)}


class FakeClient:
    """A client whose one answer is the synthesis text given."""

    def __init__(self, text, captured=None):
        self.text = text
        self.captured = captured if captured is not None else {}

    class _Text:
        type = "text"
        def __init__(self, text): self.text = text

    @property
    def messages(self):
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.captured.update(kwargs)
                class Response:
                    stop_reason = "end_turn"
                    content = [outer._Text('{"text": %s}' % __import__("json").dumps(outer.text))]
                return Response()
        return Messages()


def test_default_topics_need_two_papers_but_any_topic_with_a_claim_can_be_named():
    build_corpus()
    assert store.synthesis_topics() == ["recovery-rate"]
    assert len(store.topic_claims("scaling")) == 1


def test_record_and_rows_carry_the_basis_and_flag_changes_as_stale():
    a, b, c = build_corpus()
    record = store.record_synthesis("recovery-rate", "Doe (2026) sees recovery [%s]." % a, shown("recovery-rate"))
    assert set(record["claims"]) == {a, b, c}
    assert record["source"] == "model"
    [row] = store.synthesis_rows()
    assert row["topic"] == "recovery-rate"
    assert row["stale"] is False
    assert (row["n_claims"], row["n_papers"]) == (3, 2)

    # Editing a claim's text makes it stale; reviewing it does not.
    store.update_claim("doe2026recovery", a, {"reviewed": False})
    assert store.synthesis_rows()[0]["stale"] is False
    store.update_claim("doe2026recovery", a, {"text": "Llama-3 70B recovers in 4.6% of rollouts."})
    assert store.synthesis_rows()[0]["stale"] is True

    # So does adding a claim to the topic, or taking one out of it.
    store.record_synthesis("recovery-rate", "again", shown("recovery-rate"))
    assert store.synthesis_rows()[0]["stale"] is False
    store.update_claim("li2025steer", c, {"tags": ["scaling"]})
    assert store.synthesis_rows()[0]["stale"] is True


def test_the_basis_is_what_the_model_was_shown_not_what_is_on_disk():
    """A claim edited during the call leaves the synthesis stale rather than
    silently current: the text on file describes claims nobody can see."""
    a, _, _ = build_corpus()
    before = shown("recovery-rate")
    store.update_claim("doe2026recovery", a, {"text": "Changed while the model thought."})
    store.record_synthesis("recovery-rate", "text", before)
    assert store.synthesis_rows()[0]["stale"] is True


def test_a_hand_edit_is_a_judgment_against_the_current_claims():
    a, _, _ = build_corpus()
    store.record_synthesis("recovery-rate", "model text", shown("recovery-rate"))
    store.update_claim("doe2026recovery", a, {"text": "Edited."})
    assert store.synthesis_rows()[0]["stale"] is True
    record = store.set_synthesis_text("recovery-rate", "  corrected by hand  ")
    assert record["text"] == "corrected by hand"
    assert record["source"] == "hand"
    assert store.synthesis_rows()[0]["stale"] is False

    import pytest
    with pytest.raises(ValueError):
        store.set_synthesis_text("recovery-rate", "   ")
    with pytest.raises(KeyError):
        store.set_synthesis_text("nonesuch", "text")
    with pytest.raises(KeyError):
        store.delete_synthesis("nonesuch")
    store.delete_synthesis("recovery-rate")
    assert store.synthesis_rows() == []


def test_a_topic_with_no_live_claims_is_not_written_and_not_shown():
    a, b, c = build_corpus()
    assert store.record_synthesis("nonesuch", "text", {}) is None
    store.record_synthesis("scaling", "one paper", shown("scaling"))
    assert [r["topic"] for r in store.synthesis_rows()] == ["scaling"]
    store.update_claim("li2025steer", c, {"tags": ["recovery-rate"]})
    assert store.synthesis_rows() == []            # still on file, out of view
    assert "scaling" in store.load_syntheses()


def test_renaming_a_tag_moves_the_synthesis_and_deleting_the_tag_drops_it():
    build_corpus()
    store.record_synthesis("recovery-rate", "text", shown("recovery-rate"))
    store.rename_tag("recovery-rate", "recovery")
    assert list(store.load_syntheses()) == ["recovery"]
    assert store.load_syntheses()["recovery"]["topic"] == "recovery"
    assert store.synthesis_rows()[0]["stale"] is False   # the claims moved too
    store.delete_tag("recovery")
    assert store.load_syntheses() == {}


def test_renaming_onto_a_topic_that_has_a_synthesis_keeps_the_existing_one():
    _, _, c = build_corpus()
    store.add_tag("scaling", "")
    store.record_synthesis("recovery-rate", "about recovery", shown("recovery-rate"))
    store.record_synthesis("scaling", "about scaling", shown("scaling"))
    store.rename_tag("recovery-rate", "scaling")
    assert store.load_syntheses()["scaling"]["text"] == "about scaling"
    assert list(store.load_syntheses()) == ["scaling"]


def test_a_topic_renamed_during_the_model_call_is_not_written_back():
    build_corpus()
    before = shown("recovery-rate")
    store.rename_tag("recovery-rate", "recovery")
    assert store.record_synthesis("recovery-rate", "late answer", before) is None
    assert store.load_syntheses() == {}


def test_a_hand_edit_during_the_model_call_wins():
    """The web app leaves edit enabled while a synthesis job runs. A correction
    saved meanwhile is newer than the model's answer and stands."""
    build_corpus()
    store.record_synthesis("recovery-rate", "first draft", shown("recovery-rate"))
    before = store.load_syntheses().get("recovery-rate")
    store.set_synthesis_text("recovery-rate", "corrected by hand")
    assert store.record_synthesis("recovery-rate", "late answer", shown("recovery-rate"), before=before) is None
    [row] = store.synthesis_rows()
    assert (row["text"], row["source"]) == ("corrected by hand", "hand")


def test_a_deletion_during_the_model_call_is_not_undone():
    build_corpus()
    store.record_synthesis("recovery-rate", "first draft", shown("recovery-rate"))
    before = store.load_syntheses().get("recovery-rate")
    store.delete_synthesis("recovery-rate")
    assert store.record_synthesis("recovery-rate", "late answer", shown("recovery-rate"), before=before) is None
    assert store.load_syntheses() == {}


def test_a_synthesis_written_during_the_model_call_is_kept_and_an_unchanged_one_is_replaced():
    build_corpus()
    # None on file when the call started; one written meanwhile is kept.
    store.record_synthesis("recovery-rate", "written meanwhile", shown("recovery-rate"))
    assert store.record_synthesis("recovery-rate", "late answer", shown("recovery-rate"), before=None) is None
    assert store.load_syntheses()["recovery-rate"]["text"] == "written meanwhile"
    # Found as the call left it: a rewrite the reviewer asked for goes through.
    before = store.load_syntheses().get("recovery-rate")
    record = store.record_synthesis("recovery-rate", "rewritten", shown("recovery-rate"), before=before)
    assert record is not None and store.load_syntheses()["recovery-rate"]["text"] == "rewritten"


def test_synthesize_topic_leaves_a_correction_made_while_the_model_thought(monkeypatch):
    build_corpus()
    store.record_synthesis("recovery-rate", "first draft", shown("recovery-rate"))

    class EditingClient(FakeClient):
        """Answers after the reviewer has corrected the synthesis by hand."""

        @property
        def messages(self):
            inner = super().messages

            class Messages:
                def create(self, **kwargs):
                    store.set_synthesis_text("recovery-rate", "corrected by hand")
                    return inner.create(**kwargs)
            return Messages()

    monkeypatch.setattr(extract, "client", lambda: EditingClient("late answer"))
    assert extract.synthesize_topic("recovery-rate") == {"written": False, "claims": 3, "papers": 2}
    [row] = store.synthesis_rows()
    assert (row["text"], row["source"]) == ("corrected by hand", "hand")


def test_an_unreadable_file_is_reported_and_never_written_over():
    import pytest
    build_corpus()
    store.syntheses_path().write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        store.record_synthesis("recovery-rate", "text", shown("recovery-rate"))
    assert store.syntheses_path().read_text(encoding="utf-8") == "{not json"


def test_synthesize_topic_skips_an_empty_topic_without_calling_the_model(monkeypatch):
    build_corpus()
    monkeypatch.setattr(extract, "client", lambda: (_ for _ in ()).throw(AssertionError("called")))
    assert extract.synthesize_topic("nonesuch") == {"written": False, "claims": 0, "papers": 0}


def test_synthesize_topic_shows_claims_tensions_and_review_state_and_records_the_answer(monkeypatch):
    a, b, c = build_corpus()
    store.update_claim("li2025steer", b, {"reviewed": False})
    store.record_tensions("recovery-rate", [
        {"claims": [a, b], "kind": "contradiction", "note": "Doe sees recovery; Li does not."},
        {"claims": [a, c], "kind": "tension", "note": "Dropped by the reviewer."},
    ], {r["id"]: r for r in store.claim_rows()})
    dismissed = next(t for t in store.tension_rows() if t["note"].startswith("Dropped"))
    store.set_tension_status(dismissed["id"], "dismissed")

    captured = {}
    monkeypatch.setattr(extract, "client", lambda: FakeClient(
        "Doe (2026) finds recovery in 46%% of rollouts [%s], while Li (2025) reports almost none [%s]." % (a, b),
        captured))
    result = extract.synthesize_topic("recovery-rate")
    assert result == {"written": True, "claims": 3, "papers": 2}
    prompt = captured["messages"][0]["content"]
    assert "How often a model returns to task." in prompt
    assert a in prompt and b in prompt and c in prompt
    assert f"{b} [finding]: Steered models almost never return to the task. (unreviewed extraction)" in prompt
    assert "(unreviewed extraction)" not in prompt.split(a)[1].split("\n")[0]
    assert "[open] contradiction between" in prompt and "Doe sees recovery; Li does not." in prompt
    assert "Dropped by the reviewer." not in prompt
    assert captured["output_config"]["format"]["schema"] is extract.SYNTHESIS_SCHEMA
    [row] = store.synthesis_rows()
    assert row["text"].startswith("Doe (2026) finds recovery")
    assert row["stale"] is False


def test_synthesize_topic_with_one_paper_still_writes(monkeypatch):
    build_corpus()
    monkeypatch.setattr(extract, "client", lambda: FakeClient("Only Li (2025) speaks to scale."))
    assert extract.synthesize_topic("scaling") == {"written": True, "claims": 1, "papers": 1}
    assert "No disagreements between these claims have been noted yet." in \
        extract._tension_block("scaling", store.claim_rows())


def test_api_state_carries_syntheses_and_they_can_be_edited_and_deleted():
    a, _, _ = build_corpus()
    store.record_synthesis("recovery-rate", "text [%s]" % a, shown("recovery-rate"))
    with TestClient(server.app) as client:
        [row] = client.get("/api/state").json()["syntheses"]
        assert row["topic"] == "recovery-rate" and row["stale"] is False

        r = client.patch("/api/syntheses/recovery-rate", json={"text": "by hand"})
        assert r.status_code == 200 and r.json()["source"] == "hand"
        assert client.patch("/api/syntheses/recovery-rate", json={"text": " "}).status_code == 422
        assert client.patch("/api/syntheses/nonesuch", json={"text": "x"}).status_code == 404

        assert client.delete("/api/syntheses/recovery-rate").status_code == 200
        assert client.delete("/api/syntheses/recovery-rate").status_code == 404
        assert client.get("/api/state").json()["syntheses"] == []


def test_api_synthesize_defaults_to_two_paper_topics_and_accepts_any_named_topic_with_claims(monkeypatch):
    build_corpus()
    submitted = []
    monkeypatch.setattr(server._pool, "submit", lambda fn, job, topics: submitted.append(topics))
    with TestClient(server.app) as client:
        assert client.post("/api/syntheses", json={}).json() == {"queued": 1}
        assert client.post("/api/syntheses", json={"topics": ["nonesuch"]}).json() == {"queued": 0}
        body = {"topics": ["scaling", "recovery-rate", "scaling", "nonesuch"]}
        assert client.post("/api/syntheses", json=body).json() == {"queued": 2}
        assert max(server._jobs.values(), key=lambda j: j["id"])["label"] == "synthesis of 2 topics"
    assert submitted == [["recovery-rate"], ["recovery-rate", "scaling"]]


def test_api_synthesize_queues_nothing_for_a_corpus_of_one_paper():
    _paper("solo2026", "Alone", "Ann Solo", 2026, ("Only claim.", ["recovery-rate"]))
    with TestClient(server.app) as client:
        assert client.post("/api/syntheses", json={}).json() == {"queued": 0}


def test_web_pass_goes_on_after_a_topic_fails_and_says_so(monkeypatch):
    asked = []

    def synth(topic):
        asked.append(topic)
        if topic == "recovery-rate":
            raise RuntimeError("synthesis refused for recovery-rate: no")
        return {"written": True, "claims": 1, "papers": 1}

    monkeypatch.setattr(extract, "synthesize_topic", synth)
    job = server._new_job("synthesis of 2 topics")
    server._run_syntheses(job, ["recovery-rate", "scaling"])
    assert asked == ["recovery-rate", "scaling"]
    assert job["state"] == "error"
    assert job["detail"] == ("1 of 2 topics failed, 1 written; "
                             "recovery-rate: RuntimeError: synthesis refused for recovery-rate: no")

    job = server._new_job("synthesis of 1 topics")
    server._run_syntheses(job, ["scaling"])
    assert (job["state"], job["detail"]) == ("done", "1 of 1 topics written")


def test_export_puts_the_synthesis_under_its_topic_with_citations_as_markers():
    a, b, _ = build_corpus()
    store.record_synthesis("recovery-rate",
                           "Doe (2026) sees recovery [%s] but Li (2025) does not [%s, %s]. See [Table 2]." % (a, b, a),
                           shown("recovery-rate"))
    html = export.render()
    topic_section = html.split("<h3>recovery-rate")[1].split("</section>")[0]
    assert '<div class="synth">' in topic_section
    assert topic_section.index('<div class="synth">') < topic_section.index('<div class="claim')
    assert 'title="Llama-3 70B recovers in 46% of rollouts."' in topic_section
    assert ">Doe 2026</span>" in topic_section and ">Li 2025</span>" in topic_section
    assert "[Table 2]" in topic_section                   # not claim ids: left alone
    assert "written by the model" in topic_section
    assert "claims changed since" not in html
    store.update_claim("doe2026recovery", a, {"text": "Edited."})
    assert "claims changed since" in export.render()


def test_export_escapes_synthesis_text():
    a, _, _ = build_corpus()
    store.record_synthesis("recovery-rate", "<script>alert(1)</script> [%s]" % a, shown("recovery-rate"))
    html = export.render()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_cli_lists_syntheses_without_calling_the_model(capsys, monkeypatch):
    a, _, _ = build_corpus()
    monkeypatch.setattr(extract, "client", lambda: (_ for _ in ()).throw(AssertionError("called")))
    store.record_synthesis("recovery-rate", "The synthesis.", shown("recovery-rate"))
    assert __main__.main(["synthesize", "--list"]) == 0
    out = capsys.readouterr().out
    assert "## recovery-rate" in out and "The synthesis." in out
    assert "1 syntheses, 0 stale" in out
    store.update_claim("doe2026recovery", a, {"text": "Edited."})
    assert __main__.main(["synthesize", "--list", "scaling"]) == 0
    assert "0 syntheses, 0 stale" in capsys.readouterr().out
    assert __main__.main(["synthesize", "--list"]) == 0
    assert "1 syntheses, 1 stale" in capsys.readouterr().out


def test_cli_synthesize_writes_and_fails_on_an_empty_topic(capsys, monkeypatch):
    build_corpus()
    monkeypatch.setattr(extract, "client", lambda: FakeClient("Written."))
    assert __main__.main(["synthesize"]) == 0
    assert "recovery-rate: written from 3 claims in 2 papers" in capsys.readouterr().out
    assert __main__.main(["synthesize", "nonesuch"]) == 1
    assert "nonesuch: no claims, nothing written" in capsys.readouterr().err
