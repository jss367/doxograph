"""Existing writing saves without a submit, including edits made in flight."""
import asyncio

import httpx

import pytest
from playwright.async_api import expect
from doxograph import store

from browser_support import browser_session, run_browser, inert_tools as inert_tools
from test_ask_browser import _server
from test_core_workspace_boundary_browser import editor, seed
from test_quit_manual_editors_browser import stored
from test_browser import _answer


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
@pytest.mark.parametrize('failure', [False, True])
def test_existing_writing_autosaves_without_closing_editor(engine, kind, failure):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url + '/#view=claims')
                field, identity = await editor(page, kind, claims)
                path = f'/api/papers/study/claims/{identity}' if kind == 'claim' else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/context'
                started, release = asyncio.Event(), asyncio.Event()
                requests = []

                async def hold(route):
                    requests.append(route.request.post_data_json)
                    if len(requests) == 1:
                        started.set()
                        await release.wait()
                    if failure:
                        await route.fulfill(status=503, json={'detail': 'Synthetic offline save'})
                    else:
                        await route.continue_()

                await page.route('**' + path, hold)
                await field.fill('First automatic edit')
                await asyncio.wait_for(started.wait(), 5)
                await expect(field).to_be_enabled()
                await field.fill('Newer writing during save')
                release.set()
                status = page.locator(f'[data-writing-status="{kind}"]').first
                if failure:
                    await expect(status).to_contain_text('Couldn’t save')
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    await expect(field).to_have_value('Newer writing during save')
                    await page.unroute('**' + path, hold)
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
                else:
                    await expect(status).to_have_text('Saved')
                    assert len(requests) >= 2
                await expect(field).to_be_visible()
                await expect(field).to_have_value('Newer writing during save')
                assert stored(url, kind, identity) == 'Newer writing during save'
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                assert not errors
                await page.reload()
                assert stored(url, kind, identity) == 'Newer writing during save'

    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
def test_workspace_switch_flushes_then_retires_writing_timers(engine, kind):
    from doxograph import config
    _, claims, _ = seed()
    other = config.create_workspace('Second workspace')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#view=claims')
                field, identity = await editor(page, kind, claims)
                await field.fill('Save before switching')
                await page.evaluate('id => switchWorkspace(id)', other)
                assert await page.evaluate('workspaceClient.workspaceId') == other
                assert await page.evaluate('window.doxographSaveDrafts()') is True, await page.evaluate('({drafts:V.drafts, synth:V.synthDrafts, research:V.researchDraft, edit:V.editing, writing:hasQuitWriting(), pending:workspaceClient.pendingMutations, entries:[...writingAutosaves.values()].map(e=>({active:e.owner===V.drafts,saving:e.saver.saving,unconfirmed:e.saver.unconfirmed,failure:e.saver.failure}))})')
                assert stored(url, kind, identity) == 'Save before switching'
                await page.evaluate("switchWorkspace('default')")
                assert await page.evaluate('window.doxographSaveDrafts()') is True, await page.evaluate('({drafts:V.drafts, synth:V.synthDrafts, research:V.researchDraft, edit:V.editing, writing:hasQuitWriting(), pending:workspaceClient.pendingMutations, entries:[...writingAutosaves.values()].map(e=>({active:e.owner===V.drafts,saving:e.saver.saving,unconfirmed:e.saver.unconfirmed,failure:e.saver.failure}))})')
                assert stored(url, kind, identity) == 'Save before switching'
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research', 'focused-claim'])
def test_failed_done_can_be_cancelled_then_discarded(engine, kind):
    """Discard closes a failed editor, retires retries, and preserves saved data."""
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#view=claims')
                paper = 'study'
                if kind == 'focused-claim':
                    # The review button exists before the initial state fetch
                    # has populated visibleClaims; wait for a seeded claim.
                    await expect(page.locator(f'[data-act="edit"][data-claim="{claims[0]}"]').first).to_be_visible()
                    await page.locator('#btn-focus-review').click()
                    await page.locator('[data-focus="edit"]').click()
                    field = page.locator('#focused-review textarea[name="text"]')
                    identity = await page.locator('#focused-review form[data-form]').get_attribute('data-form')
                    paper = await page.locator('#focused-review .edit-wrap').get_attribute('data-paper')
                    done = page.locator('#focused-review button[type="submit"]')
                else:
                    field, identity = await editor(page, kind, claims)
                    selector = f'form[data-form="{identity}"] button[type="submit"]' if kind == 'claim' else '[data-act="save-synth"]' if kind == 'synthesis' else '#research-form button[type="submit"]'
                    done = page.locator(selector).first
                saved_kind = 'claim' if kind == 'focused-claim' else kind
                original = stored(url, saved_kind, identity)
                path = f'/api/papers/{paper}/claims/{identity}' if saved_kind == 'claim' else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/context'
                requests = []

                async def reject(route):
                    requests.append(route.request.method)
                    await route.fulfill(status=400, json={'detail': 'Synthetic validation failure'})

                await page.route('**' + path, reject)
                await field.fill('Unsavable writing')
                await done.click()
                await _answer(page, 'Cancel')
                await expect(field).to_have_value('Unsavable writing')
                await done.click()
                await _answer(page, 'Discard changes')
                await expect(field).to_have_count(0)
                count = len(requests)
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                await page.wait_for_timeout(700)
                assert len(requests) == count
                assert stored(url, saved_kind, identity) == original
                assert await page.evaluate('([kind, id]) => kind === "claim" ? !V.drafts[id] && !V.quitClaimBases[id] && !V.unconfirmedClaims[id] : kind === "synthesis" ? !V.synthDrafts[id] && !V.quitSynthBases[id] && !V.unconfirmedSyntheses[id] : V.researchDraft === null', [saved_kind, identity])
                await page.unroute('**' + path, reject)
                if kind == 'focused-claim':
                    await page.locator('[data-focus="edit"]').click()
                else:
                    field, _ = await editor(page, kind, claims)
                await expect(field).to_have_value(original)
                await field.fill('Valid writing after discard')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                assert stored(url, saved_kind, identity) == 'Valid writing after discard'
    run_browser(asyncio.wait_for(scenario(), 30))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_removing_a_claim_link_autosaves_after_navigation(engine):
    """A link-only edit saves the containing claim without a Done or quit flush."""
    _, claims, _ = seed()
    identity = claims[0]

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                assert httpx.put(url + '/api/ledger', json={'claims': [{'id': 'L1', 'text': 'My claim'}]}).status_code == 200
                path = f'/api/papers/study/claims/{identity}'
                assert httpx.patch(url + path, json={'ledger_links': [{'claim': 'L1', 'relation': 'supports', 'note': ''}]}).status_code == 200
                await page.goto(url + '/#view=claims')
                await editor(page, 'claim', claims)
                form = page.locator(f'form[data-form="{identity}"]')
                async with page.expect_response(lambda response: response.url.endswith(path) and response.request.method == 'PATCH') as saved:
                    await form.locator('[data-act="drop-link"]').first.click()
                    await page.locator('#btn-library-view').click()
                assert (await saved.value).status == 200
                row = next(row for row in httpx.get(url + '/api/state').json()['claims'] if row['id'] == identity)
                assert row['ledger_links'] == []
                assert await page.evaluate('writingAutosave("claim", "").generation') == 0
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('review_succeeds', [False, True])
def test_review_completion_resumes_blocked_claim_autosave(engine, review_succeeds):
    """A review overlapping the debounce cannot strand parked writing."""
    _, claims, _ = seed()
    identity = claims[0]
    # Handwritten seed claims default to reviewed. This scenario exercises
    # marking an unreviewed claim, and retaining False when that request fails.
    store.update_claim('study', identity, {'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#view=claims')
                field, _ = await editor(page, 'claim', claims)
                path = f'/api/papers/study/claims/{identity}'
                started, release = asyncio.Event(), asyncio.Event()
                writes = []

                async def hold_review(route):
                    if route.request.method != 'PATCH':
                        await route.continue_()
                        return
                    patch = route.request.post_data_json
                    # Edit history adds optimistic-concurrency metadata to
                    # every claim PATCH, including a review-only mutation.
                    if set(patch) - {'expected_revision'} == {'reviewed'}:
                        assert patch['reviewed'] is True
                        started.set()
                        await release.wait()
                        if not review_succeeds:
                            await route.fulfill(status=503, json={'detail': 'Synthetic review failure'})
                            return
                    else:
                        writes.append(patch)
                    await route.continue_()

                await page.route('**' + path, hold_review)
                await field.fill('Writing held behind a review')
                await page.evaluate('id => writingAutosave("claim", id).settle()', identity)
                await page.evaluate('id => { window.reviewing = toggleReviewed(S.claims.find(row => row.id === id)); }', identity)
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await page.evaluate('id => writingAutosave("claim", id).resume()', identity)
                    await expect(page.locator('[data-writing-status="claim"]').first).to_contain_text('Another change is still saving')
                    await page.locator('#btn-library-view').click()
                    async with page.expect_response(lambda response: response.url.endswith(path) and response.request.method == 'PATCH' and set(response.request.post_data_json) - {'expected_revision'} == {'reviewed'}) as reviewed_response, page.expect_response(lambda response: response.url.endswith(path) and response.request.method == 'PATCH' and 'text' in response.request.post_data_json) as saved_response:
                        release.set()
                        review_result = await page.evaluate('window.reviewing')
                    assert (await reviewed_response.value).status == (200 if review_succeeds else 503)
                    assert review_result is review_succeeds
                    assert (await saved_response.value).status == 200
                    assert len(writes) == 1
                    assert writes[0]['reviewed'] is review_succeeds
                    row = next(row for row in httpx.get(url + '/api/state').json()['claims'] if row['id'] == identity)
                    assert row['text'] == 'Writing held behind a review'
                    assert row['reviewed'] is review_succeeds
                finally:
                    release.set()
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('failure_stage', ['settle', 'delete'])
def test_failed_paper_removal_resumes_claim_autosave(engine, failure_stage):
    """Both early and final removal failures restore the pending autosave."""
    removed, claims, _ = seed()
    identity = claims[0]

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#view=claims')
                started, release = asyncio.Event(), asyncio.Event()
                rejected_path = f'/api/papers/study/claims/{removed}' if failure_stage == 'settle' else '/api/papers/study'

                async def reject_removal(route):
                    if route.request.method == 'DELETE':
                        started.set()
                        await release.wait()
                        await route.fulfill(status=503, json={'detail': 'Synthetic removal failure'})
                    else:
                        await route.continue_()

                await page.route('**' + rejected_path, reject_removal)
                if failure_stage == 'settle':
                    await page.locator(f'[data-act="del"][data-claim="{removed}"]').first.click()
                field, _ = await editor(page, 'claim', claims)
                await field.fill('Writing survives failed removal')
                await page.evaluate('id => writingAutosave("claim", id).settle()', identity)
                await page.evaluate("() => { window.removing = removePaper('study'); }")
                await _answer(page, 'Remove')
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await page.evaluate('id => writingAutosave("claim", id).resume()', identity)
                    await expect(page.locator('[data-writing-status="claim"]').first).to_contain_text('paper is being removed')
                    path = f'/api/papers/study/claims/{identity}'
                    async with page.expect_response(lambda response: response.url.endswith(path) and response.request.method == 'PATCH'):
                        release.set()
                        await page.evaluate('window.removing')
                    assert stored(url, 'claim', identity) == 'Writing survives failed removal'
                finally:
                    release.set()
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['paper', 'claim', 'synthesis'])
def test_confirmed_deletion_retires_uncertain_autosave(engine, kind):
    """Deleted writing cannot leave a hidden failed saver blocking quit."""
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#view=claims')
                writing_kind = 'synthesis' if kind == 'synthesis' else 'claim'
                field, identity = await editor(page, writing_kind, claims)
                path = f'/api/papers/study/claims/{identity}' if writing_kind == 'claim' else '/api/syntheses/recovery'

                async def fail_save(route):
                    if route.request.method == 'PATCH':
                        await route.fulfill(status=503, json={'detail': 'Synthetic save failure'})
                    else:
                        await route.continue_()

                await page.route('**' + path, fail_save)
                await field.fill('Unconfirmed writing')
                await expect(page.locator(f'[data-writing-status="{writing_kind}"]').first).to_contain_text('Couldn’t save')
                assert await page.evaluate('writingUnconfirmed()') is True
                if kind == 'paper':
                    await page.evaluate("() => { window.removing = removePaper('study'); }")
                    await _answer(page, 'Remove')
                else:
                    await page.locator('#btn-library-view').click()
                    await page.locator('#btn-claims-view').click()
                    selector = f'[data-act="del"][data-claim="{identity}"]' if kind == 'claim' else '[data-act="del-synth"][data-topic="recovery"]'
                    await page.locator(selector).first.click()
                    await page.evaluate('() => { window.removing = flushTrash(); }')
                await page.evaluate('window.removing')
                assert await page.evaluate('writingUnconfirmed()') is False
                assert await page.evaluate('window.doxographSaveDrafts()') is True
    run_browser(asyncio.wait_for(scenario(), 25))
