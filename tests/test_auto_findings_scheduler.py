"""Findings ownership and recovery, with inert timers and mocked providers."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from doxograph import config, extract, job_queue, providers, server, store
from doxograph.analysis_types import ModelResponse


class ManualTimer:
    created = []

    def __init__(self, delay, function, args=()):
        self.delay, self.function, self.args = delay, function, args
        self.cancelled = self.started = False
        self.on_cancel = None
        self.daemon = False
        self.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        if self.on_cancel:
            self.on_cancel()
        self.cancelled = True

    def fire(self):
        self.function(*self.args)


class ControlledPool:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        future = Future()
        self.calls.append((fn, args, future))
        return future

    def run(self, index=0):
        fn, args, future = self.calls[index]
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn(*args))
            except BaseException as error:
                future.set_exception(error)
                raise


@pytest.fixture(autouse=True)
def inert_scheduler(monkeypatch):
    ManualTimer.created = []
    pool = ControlledPool()
    monkeypatch.setattr(job_queue, 'threading', SimpleNamespace(Timer=ManualTimer))
    monkeypatch.setattr(job_queue, '_auto_findings_timers', {})
    monkeypatch.setattr(job_queue, '_jobs', {})
    monkeypatch.setattr(job_queue, '_pool', pool)
    monkeypatch.setattr(job_queue, '_auto_findings_stopping', False)

    def forbidden(*args, **kwargs):
        pytest.fail('Unmocked model boundary forbidden in scheduler tests')

    monkeypatch.setattr(extract, 'client', forbidden)
    monkeypatch.setattr(providers, 'create', forbidden)
    yield pool
    for _, _, future in pool.calls:
        if not future.done():
            future.cancel()


def corpus():
    store.add_tag('synthetic-topic')
    for key in ['synthetic-a', 'synthetic-b']:
        store.save_paper(store.new_paper(key, title=key))
        store.add_claim(key, {'text': 'Synthetic claim ' + key, 'tags': ['synthetic-topic']})


def test_debounce_keeps_only_the_latest_timer_and_queues_once(inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    first = ManualTimer.created[-1]
    job_queue.schedule_findings()
    second = ManualTimer.created[-1]
    assert first.cancelled and not second.cancelled
    assert second.delay == 20 and second.daemon
    assert not inert_scheduler.calls
    second.fire()
    assert len(inert_scheduler.calls) == 1
    job = inert_scheduler.calls[0][1][0]
    assert job['kind'] == 'findings' and job['automatic'] is True
    assert job['workspace'] == 'default'
    assert inert_scheduler.calls[0][1][1:] == (['synthetic-topic'], False)


@pytest.mark.parametrize('kind', ['import', 'findings'])
def test_due_waits_for_existing_workspace_jobs(kind, inert_scheduler):
    corpus()
    active = job_queue._new_job('Synthetic work', kind=kind, state='reading')
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    assert not inert_scheduler.calls
    assert len(ManualTimer.created) == 2
    job_queue._set(active, state='done')
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


@pytest.mark.parametrize('setting', ['ai_enabled', 'auto_findings'])
@pytest.mark.parametrize('when', ['before-schedule', 'before-due'])
def test_live_disabled_settings_suppress_new_automatic_jobs(setting, when, inert_scheduler):
    corpus()
    setter = getattr(config, 'set_' + setting)
    if when == 'before-schedule':
        setter(False)
        job_queue.schedule_findings()
        assert not ManualTimer.created
    else:
        job_queue.schedule_findings()
        setter(False)
        ManualTimer.created[-1].fire()
    assert not inert_scheduler.calls


def test_timer_uses_its_captured_workspace_and_not_callers_current_workspace(inert_scheduler):
    other = config.create_workspace('Synthetic isolated workspace')['id']
    with config.use_workspace(other):
        corpus()
        job_queue.schedule_findings()
    assert config.workspace_id() == 'default'
    ManualTimer.created[-1].fire()
    assert inert_scheduler.calls[0][1][0]['workspace'] == other


def test_existing_job_in_another_workspace_does_not_block_due(inert_scheduler):
    corpus()
    other = config.create_workspace('Other')['id']
    with config.use_workspace(other):
        job_queue._new_job('Other work', state='reading', kind='import')
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


def test_deleted_workspace_drops_its_pending_callback(inert_scheduler):
    other = config.create_workspace('Temporary')['id']
    job_queue.schedule_findings(other)
    timer = ManualTimer.created[-1]
    config.delete_workspace(other)
    timer.fire()
    assert not inert_scheduler.calls and not job_queue._auto_findings_timers


def test_cancelled_callback_cannot_remove_replacement_or_fire_early(inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    first = ManualTimer.created[-1]
    entered, finished = Event(), Event()

    def callback_already_started():
        entered.set()
        first.fire()
        finished.set()

    thread = Thread(target=callback_already_started)

    def start_callback_before_cancellation_finishes():
        thread.start()
        assert entered.wait(2)

    first.on_cancel = start_callback_before_cancellation_finishes
    # schedule holds the real timer lock while the old callback starts and
    # waits for it; the replacement is installed before that callback proceeds.
    job_queue.schedule_findings()
    replacement = ManualTimer.created[-1]
    assert finished.wait(2)
    thread.join(2)
    assert first.cancelled and replacement.started and not replacement.cancelled
    assert not inert_scheduler.calls
    assert job_queue._auto_findings_timers['default'] is replacement
    replacement.fire()
    assert len(inert_scheduler.calls) == 1


def test_concurrent_automatic_admissions_have_one_owner(inert_scheduler):
    corpus()
    gate = Barrier(2)

    def admit():
        gate.wait(timeout=2)
        return job_queue.queue_findings(['synthetic-topic'], automatic_token='a' * 32)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(admit) for _ in range(2)]
        results = [future.result(timeout=3) for future in futures]
    assert sorted(results) == [False, True]
    assert len(inert_scheduler.calls) == 1


@pytest.mark.parametrize('force', [False, True])
def test_manual_during_automatic_pass_is_serial_and_preserves_force(monkeypatch, force):
    corpus()
    first, second, release = Event(), Event(), Event()
    manual_entered = Event()
    execution = job_queue._findings_execution

    @contextmanager
    def tracked_execution(job):
        if not job.get('automatic'):
            manual_entered.set()
        with execution(job) as owned:
            yield owned

    monkeypatch.setattr(job_queue, '_findings_execution', tracked_execution)
    calls, calls_lock = [], Lock()
    monkeypatch.setattr(extract, 'client', lambda: object())

    def mocked_provider(api, **kwargs):
        with calls_lock:
            calls.append(kwargs['messages'])
            (first if len(calls) == 1 else second).set()
        assert release.wait(3), 'controlled mocked-provider release timed out'
        return ModelResponse.from_text('{"tensions": []}')

    monkeypatch.setattr(providers, 'create', mocked_provider)
    monkeypatch.setattr(extract, 'find_agreements', lambda *a, **kw: {'added': 0})
    monkeypatch.setattr(extract, 'synthesize_topic', lambda *a, **kw: {'written': False})
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(job_queue, '_pool', pool)
        try:
            job_queue.schedule_findings()
            ManualTimer.created[-1].fire()
            assert first.wait(2)
            assert store.tension_pass('synthetic-topic') is None
            with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
                result = client.post('/api/findings', json={'topics': ['synthetic-topic'], 'force': force})
                assert result.status_code == 200 and result.json() == {'queued': 1}
                assert manual_entered.wait(2)
                assert not second.is_set()
            assert len(calls) == 1
            assert len([job for job in job_queue._jobs.values() if job.get('kind') == 'findings']) == 2
        finally:
            release.set()
    assert len(calls) == (2 if force else 1)
    assert all(job['state'] == 'done' for job in job_queue._jobs.values())


def test_pending_intent_is_visible_on_quit_and_resumes_after_restart(monkeypatch, inert_scheduler):
    corpus()
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        completed = job_queue._new_job('Synthetic completed read', state='done', key='synthetic-a')
        before = client.get('/api/health').json()
        job_queue.schedule_findings()
        timer = ManualTimer.created[-1]
        health = client.get('/api/health').json()
        assert health['busy'] == 1 and health['jobs'] == 0 and health['pending_findings'] == 1
        assert health['taken'] == before['taken']
        assert list(job_queue._jobs.values()) == [completed]
    assert timer.cancelled
    assert not job_queue._auto_findings_timers
    assert job_queue._auto_findings_path().is_file()
    # Model process restart by discarding only process-local registries, then
    # invoke the actual recovery routine against the same temporary data files.
    monkeypatch.setattr(job_queue, '_jobs', {})
    monkeypatch.setattr(job_queue, '_auto_findings_timers', {})
    job_queue._recover_jobs()
    job_queue.resume_auto_findings(startup=True)
    assert job_queue._auto_findings_timers
    assert not inert_scheduler.calls
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


def test_submit_failure_is_visible_but_not_automatically_retried(monkeypatch, inert_scheduler):
    corpus()

    def refused(*args, **kwargs):
        raise RuntimeError('Synthetic closed executor')

    job_queue.schedule_findings()
    with monkeypatch.context() as patch:
        patch.setattr(job_queue, '_pool', SimpleNamespace(submit=refused))
        ManualTimer.created[-1].fire()
    failed = list(job_queue._jobs.values())
    assert len(failed) == 1 and failed[0]['state'] == 'error'
    assert 'Synthetic closed executor' in failed[0]['detail']
    assert not job_queue._auto_findings_timers
    assert len(ManualTimer.created) == 1
    # Another explicit schedule can recover; no real model runs.
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


def mock_other_stages(monkeypatch):
    monkeypatch.setattr(extract, 'client', lambda: object())
    monkeypatch.setattr(extract, 'find_agreements', lambda *a, **kw: {'added': 0})
    monkeypatch.setattr(extract, 'synthesize_topic', lambda *a, **kw: {'written': False})


def test_mocked_provider_failure_is_recorded_without_timer_retry(monkeypatch, inert_scheduler):
    corpus()
    mock_other_stages(monkeypatch)
    calls = []

    def refused(api, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('Synthetic provider failure')

    monkeypatch.setattr(providers, 'create', refused)
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    inert_scheduler.run()
    job = inert_scheduler.calls[0][1][0]
    assert len(calls) == 1 and job['state'] == 'error'
    assert 'Synthetic provider failure' in job['detail']
    assert not job_queue._auto_findings_timers


@pytest.mark.parametrize('setting', ['ai_enabled', 'auto_findings'])
def test_disabled_after_admission_pauses_and_recovers_without_provider_work(monkeypatch, inert_scheduler, setting):
    corpus()
    mock_other_stages(monkeypatch)
    calls = []

    def response(api, **kwargs):
        calls.append(kwargs)
        return ModelResponse.from_text('{"tensions": []}')

    monkeypatch.setattr(providers, 'create', response)
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    getattr(config, 'set_' + setting)(False)
    inert_scheduler.run()
    job = inert_scheduler.calls[0][1][0]
    assert not calls
    assert job['state'] == 'done' and 'paused while disabled' in job['detail']
    assert job_queue._read_auto_findings()['pending'] is True
    getattr(config, 'set_' + setting)(True)
    job_queue.resume_auto_findings()
    ManualTimer.created[-1].fire()
    inert_scheduler.run(1)
    assert len(calls) == 1
    assert job_queue._read_auto_findings() is None


def test_queued_automatic_job_preserves_accepted_model_profile(monkeypatch, inert_scheduler):
    corpus()
    mock_other_stages(monkeypatch)
    calls = []

    def response(api, **kwargs):
        calls.append((config.workspace_id(), kwargs['model']))
        return ModelResponse.from_text('{"tensions": []}')

    monkeypatch.setattr(providers, 'create', response)
    config.save_analysis_settings('anthropic', 'synthetic-model-A', '')
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    config.save_analysis_settings('anthropic', 'synthetic-model-B', '')
    inert_scheduler.run()
    assert calls == [('default', 'synthetic-model-A')]


def test_successful_read_and_retag_request_inert_debounce(monkeypatch, inert_scheduler):
    corpus()
    monkeypatch.setattr(extract, 'extract_paper', lambda *args, **kwargs: None)
    monkeypatch.setattr(extract, 'retag_paper', lambda *args, **kwargs: {'changed': 1})
    read = job_queue._new_job('Synthetic read')
    job_queue._run_extract(read, 'synthetic-a', False)
    assert read['state'] == 'done' and len(ManualTimer.created) == 1
    first = ManualTimer.created[-1]
    retag = job_queue._new_job('Synthetic retag')
    job_queue._run_retag(retag, ['synthetic-a'])
    assert retag['state'] == 'done' and first.cancelled
    assert len(ManualTimer.created) == 2 and not inert_scheduler.calls


def test_queued_unstarted_intent_survives_restart_without_duplicate_live_rearm(monkeypatch, inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    assert job_queue._read_auto_findings()['pending'] is True
    job_queue.resume_auto_findings()
    assert not job_queue._auto_findings_timers  # The queued job already owns it.
    inert_scheduler.calls[0][2].cancel()
    job_queue.stop_auto_findings()
    monkeypatch.setattr(job_queue, '_jobs', {})
    job_queue.resume_auto_findings(startup=True)
    assert job_queue._auto_findings_timers
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 2


def test_started_intent_never_replays_an_uncertain_provider_call(monkeypatch, inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    job = inert_scheduler.calls[0][1][0]
    with job_queue._findings_execution(job):
        started = job_queue._read_auto_findings()
        assert started['pending'] is False
    # Restore the crash-time checkpoint: provider execution may have begun.
    store.write_json(job_queue._auto_findings_path(), started)
    job_queue.stop_auto_findings()
    monkeypatch.setattr(job_queue, '_jobs', {})
    job_queue.resume_auto_findings(startup=True)
    assert not job_queue._auto_findings_timers
    assert not job_queue._auto_findings_path().exists()
    assert len(inert_scheduler.calls) == 1


def test_new_read_during_running_pass_keeps_a_separate_follow_on(monkeypatch, inert_scheduler):
    corpus()
    mock_other_stages(monkeypatch)
    calls = []

    def response(api, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            store.add_claim('synthetic-a', {'text': 'Newly read evidence', 'tags': ['synthetic-topic']})
            job_queue.schedule_findings()
        return ModelResponse.from_text('{"tensions": []}')

    monkeypatch.setattr(providers, 'create', response)
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    old_token = inert_scheduler.calls[0][1][0]['automatic_token']
    inert_scheduler.run()
    new_pending = job_queue._read_auto_findings()
    assert new_pending['pending'] and new_pending['token'] != old_token
    ManualTimer.created[-1].fire()
    inert_scheduler.run(1)
    assert len(calls) == 2
    assert job_queue._read_auto_findings() is None


@pytest.mark.parametrize('setting', ['ai_enabled', 'auto_findings'])
def test_disabled_recovery_waits_until_settings_are_reenabled(setting, inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    job_queue.stop_auto_findings()
    setter = getattr(config, 'set_' + setting)
    setter(False)
    job_queue.resume_auto_findings(startup=True)
    assert not job_queue._auto_findings_timers and not inert_scheduler.calls
    assert job_queue._read_auto_findings()['pending']
    setter(True)
    job_queue.resume_auto_findings()
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


def test_settings_api_cancels_and_rearms_pending_intents(inert_scheduler):
    corpus()
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        job_queue.schedule_findings()
        old = ManualTimer.created[-1]
        assert client.put('/api/settings', json={'auto_findings': False}).status_code == 200
        assert old.cancelled and not job_queue._auto_findings_timers
        old.fire()
        assert not inert_scheduler.calls
        assert client.put('/api/settings', json={'auto_findings': True}).status_code == 200
        assert job_queue._auto_findings_timers
        ManualTimer.created[-1].fire()
        assert len(inert_scheduler.calls) == 1


def test_new_job_admission_failure_releases_pending_ownership(monkeypatch, inert_scheduler):
    corpus()
    job_queue.schedule_findings()

    def refused(*args, **kwargs):
        raise OSError('Synthetic journal failure')

    with monkeypatch.context() as patch:
        patch.setattr(job_queue, '_new_job', refused)
        ManualTimer.created[-1].fire()
    assert not job_queue._auto_findings_timers and not inert_scheduler.calls
    assert not job_queue._auto_findings_path().exists()
    job_queue.resume_auto_findings(startup=True)
    assert len(ManualTimer.created) == 1
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


def test_manual_submission_failure_does_not_block_later_automatic_work(monkeypatch, inert_scheduler):
    corpus()

    def refused(*args, **kwargs):
        raise RuntimeError('Synthetic executor refusal')

    with monkeypatch.context() as patch:
        patch.setattr(job_queue, '_pool', SimpleNamespace(submit=refused))
        with pytest.raises(RuntimeError, match='Synthetic executor refusal'):
            job_queue.queue_findings(['synthetic-topic'], True)
    assert all(job['state'] == 'error' for job in job_queue._jobs.values())
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


def test_manual_topics_and_force_are_kept_while_automatic_work_owns_workspace(inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    topics = ['a-specific-manual-topic']
    assert job_queue.queue_findings(topics, True)
    topics.append('later-caller-change')
    assert inert_scheduler.calls[1][1][1:] == (['a-specific-manual-topic'], True)
    assert not inert_scheduler.calls[1][1][0].get('automatic')


def test_shutdown_and_late_completion_preserve_intent_and_invalidate_old_timer(inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    old = ManualTimer.created[-1]
    job_queue.stop_auto_findings()
    job_queue.schedule_findings()  # An already accepted read finishes during shutdown.
    assert not job_queue._auto_findings_timers
    assert job_queue._read_auto_findings()['pending']
    job_queue.resume_auto_findings(startup=True)
    replacement = ManualTimer.created[-1]
    old.fire()
    assert not inert_scheduler.calls
    assert job_queue._auto_findings_timers['default'] is replacement
    replacement.fire()
    assert len(inert_scheduler.calls) == 1


def test_superseded_queued_automatic_job_cannot_consume_new_intent(monkeypatch, inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    old = inert_scheduler.calls[0][1][0]
    job_queue.schedule_findings()
    replacement = job_queue._read_auto_findings()
    inert_scheduler.run()
    assert old['state'] == 'done' and 'Superseded' in old['detail']
    assert job_queue._read_auto_findings() == replacement
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 2


def test_recovery_keeps_default_workspace_available_with_damaged_registry(monkeypatch, inert_scheduler):
    corpus()
    job_queue.schedule_findings()
    job_queue.stop_auto_findings()

    def broken_registry():
        raise ValueError('Synthetic damaged registry')

    monkeypatch.setattr(config, 'list_workspaces', broken_registry)
    job_queue.resume_auto_findings(startup=True)
    ManualTimer.created[-1].fire()
    assert len(inert_scheduler.calls) == 1


@pytest.mark.parametrize('setting', ['ai_enabled', 'auto_findings'])
def test_admitted_auto_job_disabled_while_waiting_for_pass_lock_keeps_intent(monkeypatch, inert_scheduler, setting):
    corpus()
    mock_other_stages(monkeypatch)
    calls, waiting = [], Event()
    original_lock = store.document_lock

    def response(api, **kwargs):
        assert job_queue._read_auto_findings()['pending'] is False
        calls.append(kwargs)
        return ModelResponse.from_text('{"tensions": []}')

    @contextmanager
    def tracked_lock(name, **kwargs):
        if name == 'findings-pass':
            waiting.set()
        with original_lock(name, **kwargs):
            yield

    monkeypatch.setattr(providers, 'create', response)
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    job = inert_scheduler.calls[0][1][0]
    with ThreadPoolExecutor(max_workers=1) as workers:
        with original_lock('findings-pass'):
            monkeypatch.setattr(store, 'document_lock', tracked_lock)
            future = workers.submit(inert_scheduler.run)
            assert waiting.wait(2)
            getattr(config, 'set_' + setting)(False)
            assert job_queue._read_auto_findings()['pending'] is True
            assert not calls
        future.result(timeout=3)
    assert job['state'] == 'done' and 'paused while disabled' in job['detail']
    assert not calls and job_queue._read_auto_findings()['pending'] is True
    getattr(config, 'set_' + setting)(True)
    job_queue.resume_auto_findings()
    ManualTimer.created[-1].fire()
    inert_scheduler.run(1)
    assert len(calls) == 1 and job_queue._read_auto_findings() is None


def test_reenable_racing_paused_job_completion_does_not_strand_intent(monkeypatch, inert_scheduler):
    corpus()
    mock_other_stages(monkeypatch)
    calls = []
    original_set = job_queue._set

    def response(api, **kwargs):
        calls.append(kwargs)
        return ModelResponse.from_text('{"tensions": []}')

    def reenable_before_terminal_notification(job, **fields):
        if fields.get('detail') == 'Automatic findings paused while disabled':
            config.set_auto_findings(True)
            job_queue.resume_auto_findings()
            assert not job_queue._auto_findings_timers  # Job still owns the token.
        original_set(job, **fields)

    monkeypatch.setattr(providers, 'create', response)
    monkeypatch.setattr(job_queue, '_set', reenable_before_terminal_notification)
    job_queue.schedule_findings()
    ManualTimer.created[-1].fire()
    config.set_auto_findings(False)
    inert_scheduler.run()
    assert not calls and job_queue._auto_findings_timers
    ManualTimer.created[-1].fire()
    inert_scheduler.run(1)
    assert len(calls) == 1


def test_manual_force_and_topics_still_run_with_automatic_updates_disabled(monkeypatch, inert_scheduler):
    corpus()
    mock_other_stages(monkeypatch)
    calls = []

    def response(api, **kwargs):
        calls.append(kwargs)
        return ModelResponse.from_text('{"tensions": []}')

    monkeypatch.setattr(providers, 'create', response)
    config.set_auto_findings(False)
    assert job_queue.queue_findings(['synthetic-topic'], True)
    assert inert_scheduler.calls[0][1][1:] == (['synthetic-topic'], True)
    inert_scheduler.run()
    assert len(calls) == 1 and inert_scheduler.calls[0][1][0]['state'] == 'done'
