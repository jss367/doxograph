"""The auto-merge gate's jq programs decide correctly on fixture API data.

.github/workflows/auto-merge.yml fetches GitHub state and hands it to the jq
programs in .github/auto-merge. These tests feed those programs the shapes
GitHub returns (paginated with --slurp, so a list of pages) and check every
verdict the workflow acts on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")

GATE = Path(__file__).resolve().parents[1] / ".github" / "auto-merge"
BOT = "chatgpt-codex-connector[bot]"
AUTHOR = "chatgpt-codex-connector"
HEAD = "c74e58bd7c5f9ea7e4b1429da7e930911b97caaa"
OLD = "4661c32e0f3c1b5d7a9e8f6b2c4d0a1e3f5b7c9d"


def jq(program: str, data, *, exit_status: bool = False, **args):
    """Run a gate program the way the workflow does and return its output."""
    cmd = ["jq", "-r"]
    if exit_status:
        cmd.append("-e")
    for name, value in args.items():
        if isinstance(value, str):
            cmd += ["--arg", name, value]
        else:
            cmd += ["--argjson", name, json.dumps(value)]
    cmd += ["-f", str(GATE / program)]
    done = subprocess.run(cmd, input=json.dumps(data), capture_output=True, text=True)
    if exit_status:
        return done.returncode == 0
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# --- Codex's review summary --------------------------------------------------

def review(commit=HEAD, status="completed", any_findings=False, unresolved=False):
    return {
        "commit_sha": commit,
        "has_any_findings": any_findings,
        "has_unresolved_findings": unresolved,
        "status": status,
    }


NO_REVIEW = {"commit_sha": None, "has_any_findings": None,
             "has_unresolved_findings": None, "status": None}


def summary(code=None, security=None, *, updated="2026-10-26T16:15:26Z",
            user=BOT, state=True, shown_commit=None):
    """A Codex summary comment, laid out as Codex writes it."""
    payload = {"code_review": code or review(), "security_review": security or NO_REVIEW}
    marker = f"<!-- codex-review-state:v1 {json.dumps(payload)} -->\n" if state else ""
    shown = shown_commit or (payload["code_review"]["commit_sha"] or "")[:7]
    body = (
        "<!-- codex-pull-request-review-summary -->\n"
        f"{marker}\n"
        "## Codex Review Summary\n\n"
        "| Review | Status | Commit | Review trigger |\n"
        "| --- | --- | --- | --- |\n"
        f"| 📝 **Code Review** | ✅ **Completed** | `{shown}` | Manual request |\n"
    )
    return {"user": {"login": user}, "body": body,
            "created_at": "2026-10-26T16:12:27Z", "updated_at": updated}


def human(body="LGTM", updated="2026-10-26T16:00:00Z"):
    return {"user": {"login": "jss367"}, "body": body,
            "created_at": updated, "updated_at": updated}


def codex_state(*pages, thumbs="2026-10-26T16:15:50Z", head=HEAD):
    return jq("codex_state.jq", list(pages), bot=BOT, head=head, thumbs=thumbs)


def test_a_clean_review_of_the_head_passes():
    assert codex_state([human(), summary()]) == "ok"


def test_a_stale_summary_of_an_older_commit_blocks():
    assert codex_state([summary(review(commit=OLD))]) == f"Codex reviewed {OLD}, not {HEAD}"


def test_a_backticked_head_does_not_stand_in_for_the_structured_commit():
    # The table shows the head, but the structured state names another commit.
    stale = summary(review(commit=OLD), shown_commit=HEAD[:7])
    assert codex_state([stale]) == f"Codex reviewed {OLD}, not {HEAD}"


def test_an_abbreviated_structured_commit_blocks():
    assert codex_state([summary(review(commit=HEAD[:10]))]).startswith("Codex reviewed")


def test_a_review_still_running_blocks():
    assert codex_state([summary(review(status="running"))]) == "Codex code review is running"


def test_unresolved_findings_block():
    current = summary(review(any_findings=True, unresolved=True))
    assert codex_state([current]) == f"Codex code review of {HEAD} has findings"


def test_resolved_findings_still_block_until_a_clean_rerun():
    current = summary(review(any_findings=True, unresolved=False))
    assert codex_state([current]) == f"Codex code review of {HEAD} has findings"


def test_missing_finding_flags_fail_closed():
    current = summary(review(any_findings=None, unresolved=None))
    assert codex_state([current]) == f"Codex code review of {HEAD} has findings"


def test_a_thumbs_up_older_than_the_summary_blocks():
    assert codex_state([summary()], thumbs="2026-10-26T16:15:00Z") == (
        "👍 predates the latest review summary")


def test_a_thumbs_up_in_the_same_second_as_the_summary_blocks():
    assert codex_state([summary()], thumbs="2026-10-26T16:15:26Z") == (
        "👍 predates the latest review summary")


def test_no_summary_blocks():
    assert codex_state([human()]) == "no Codex review summary"


def test_a_summary_written_by_someone_else_is_ignored():
    assert codex_state([summary(user="jss367")]) == "no Codex review summary"


def test_a_summary_without_structured_state_blocks():
    assert codex_state([summary(state=False)]) == "Codex summary has no readable review state"


def test_a_malformed_structured_state_blocks():
    broken = summary()
    broken["body"] = broken["body"].replace('"code_review": {', '"code_review": {{', 1)
    assert codex_state([broken]) == "Codex summary has no readable review state"


def test_the_latest_summary_decides():
    older = summary(updated="2026-10-26T16:10:00Z")
    newer = summary(review(status="running"), updated="2026-10-26T16:15:26Z")
    assert codex_state([newer, older]) == "Codex code review is running"


def test_a_summary_on_a_later_page_is_found():
    assert codex_state([human()] * 3, [summary()]) == "ok"


def test_a_clean_security_review_of_an_older_commit_passes():
    assert codex_state([summary(security=review(commit=OLD))]) == "ok"


def test_a_running_security_review_blocks():
    current = summary(security=review(status="running"))
    assert codex_state([current]) == "Codex security_review is not finished and clean"


def test_a_security_review_with_open_findings_blocks():
    current = summary(security=review(commit=OLD, any_findings=True, unresolved=True))
    assert codex_state([current]) == "Codex security_review is not finished and clean"


def test_an_unrecognized_review_entry_blocks():
    current = summary()
    current["body"] = current["body"].replace(
        '"security_review"', '"future_review": "pending", "security_review"', 1)
    assert codex_state([current]) == "Codex future_review is not finished and clean"


# --- other reviewers' verdicts ---------------------------------------------

def verdict(user, state, at, id_=1):
    return {"id": id_, "user": {"login": user}, "state": state, "submitted_at": at}


def reviews(*pages):
    return jq("reviews.jq", list(pages))


def test_no_reviews_pass():
    assert reviews([]) == "ok"


def test_codex_comments_do_not_block():
    assert reviews([verdict(BOT, "COMMENTED", "2026-10-26T16:15:23Z")]) == "ok"


def test_a_change_request_blocks():
    assert reviews([verdict("alice", "CHANGES_REQUESTED", "2026-10-26T10:00:00Z")]) == (
        "changes requested by alice")


def test_a_later_approval_clears_a_change_request():
    assert reviews([
        verdict("alice", "CHANGES_REQUESTED", "2026-10-26T10:00:00Z", 1),
        verdict("alice", "APPROVED", "2026-10-26T11:00:00Z", 2),
    ]) == "ok"


def test_a_later_change_request_overrides_an_approval():
    assert reviews([
        verdict("alice", "APPROVED", "2026-10-26T10:00:00Z", 1),
        verdict("alice", "CHANGES_REQUESTED", "2026-10-26T11:00:00Z", 2),
    ]) == "changes requested by alice"


def test_a_later_comment_does_not_clear_a_change_request():
    assert reviews([
        verdict("alice", "CHANGES_REQUESTED", "2026-10-26T10:00:00Z", 1),
        verdict("alice", "COMMENTED", "2026-10-26T11:00:00Z", 2),
    ]) == "changes requested by alice"


def test_a_dismissed_change_request_does_not_block():
    assert reviews([verdict("alice", "DISMISSED", "2026-10-26T10:00:00Z")]) == "ok"


def test_one_approval_does_not_outvote_another_reviewers_change_request():
    assert reviews(
        [verdict("alice", "CHANGES_REQUESTED", "2026-10-26T10:00:00Z", 1),
         verdict(BOT, "COMMENTED", "2026-10-26T10:30:00Z", 2)],
        [verdict("bob", "APPROVED", "2026-10-26T11:00:00Z", 3)],
    ) == "changes requested by alice"


# --- review threads ----------------------------------------------------------

def thread(author, resolved=False, outdated=False, path="doxograph/app.py", line=12):
    first = {"author": {"login": author} if author else None}
    return {"isResolved": resolved, "isOutdated": outdated, "path": path, "line": line,
            "comments": {"nodes": [first]}}


def thread_page(*threads, more=False):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": more, "endCursor": "abc"},
        "nodes": list(threads),
    }}}}}


def threads(*pages):
    return jq("threads.jq", list(pages), author=AUTHOR)


def test_no_threads_pass():
    assert threads(thread_page()) == "ok"


def test_an_open_codex_thread_is_left_to_codexs_own_state():
    assert threads(thread_page(thread(AUTHOR))) == "ok"


def test_an_open_thread_from_another_reviewer_blocks():
    assert threads(thread_page(thread(AUTHOR), thread("coderabbitai"))) == (
        "1 unresolved review thread(s): coderabbitai at doxograph/app.py:12")


def test_a_resolved_thread_passes():
    assert threads(thread_page(thread("alice", resolved=True))) == "ok"


def test_an_outdated_thread_passes():
    assert threads(thread_page(thread("alice", outdated=True))) == "ok"


def test_a_thread_without_a_readable_author_blocks():
    assert threads(thread_page(thread(None, line=None))) == (
        "1 unresolved review thread(s): unknown at doxograph/app.py:?")


def test_an_open_thread_on_a_later_page_blocks():
    assert threads(
        thread_page(thread(AUTHOR), more=True),
        thread_page(thread("alice", path="README.md", line=3)),
    ) == "1 unresolved review thread(s): alice at README.md:3"


# --- live PR state -----------------------------------------------------------

def pull(**changes):
    pr = {"state": "open", "draft": False, "labels": [{"name": "enhancement"}],
          "head": {"sha": HEAD}, "base": {"ref": "main"}}
    pr.update(changes)
    return pr


def live(pr):
    return jq("live.jq", pr, head=HEAD, default="main")


def test_an_open_ready_pr_at_the_reviewed_head_passes():
    assert live(pull()) == "ok"


@pytest.mark.parametrize("changes, reason", [
    ({"state": "closed"}, "closed"),
    ({"draft": True}, "draft"),
    ({"labels": [{"name": "no-automerge"}]}, "labelled no-automerge"),
    ({"head": {"sha": OLD}}, f"head moved to {OLD}"),
    ({"base": {"ref": "release"}}, "base changed to release"),
])
def test_live_state_blocks(changes, reason):
    assert live(pull(**changes)) == reason


# --- first-pass triage -------------------------------------------------------

def open_pr(number=7, **changes):
    pr = {
        "number": number, "headRefOid": HEAD, "baseRefName": "main",
        "isDraft": False, "mergeable": "MERGEABLE", "labels": {"nodes": []},
        "reactions": {"totalCount": 1, "nodes": [
            {"user": {"login": BOT}, "createdAt": "2026-10-26T16:15:50Z"}]},
        "reviews": {"nodes": [{"author": {"login": AUTHOR},
                               "submittedAt": "2026-10-26T16:00:00Z"}]},
    }
    pr.update(changes)
    return pr


def candidates(*prs):
    page = {"data": {"repository": {"defaultBranchRef": {"name": "main"},
                                    "pullRequests": {"nodes": list(prs)}}}}
    return jq("candidates.jq", page, bot=BOT, author=AUTHOR).splitlines()


def test_a_pr_with_a_fresh_thumbs_up_is_a_candidate():
    assert candidates(open_pr()) == [f"7\tcandidate\t{HEAD}"]


@pytest.mark.parametrize("changes, reason", [
    ({"isDraft": True}, "draft"),
    ({"labels": {"nodes": [{"name": "no-automerge"}]}}, "labelled no-automerge"),
    ({"baseRefName": "release"}, "targets release, not main"),
    ({"mergeable": "CONFLICTING"}, "not mergeable (CONFLICTING)"),
    ({"reactions": {"totalCount": 0, "nodes": []}}, "no 👍 from Codex yet"),
    ({"reviews": {"nodes": [{"author": {"login": AUTHOR},
                             "submittedAt": "2026-10-26T16:20:00Z"}]}},
     "👍 does not postdate Codex's latest findings"),
])
def test_triage_skips(changes, reason):
    assert candidates(open_pr(**changes)) == [f"7\tskip\t{reason}"]


def test_a_truncated_reaction_list_goes_on_to_the_full_lookup():
    truncated = {"totalCount": 150, "nodes": []}
    assert candidates(open_pr(reactions=truncated)) == [f"7\tcandidate\t{HEAD}"]


# --- CI run and jobs ---------------------------------------------------------

def run(run_number, *, pr=7, head=HEAD, base="main", created="2026-10-26T16:20:00Z",
        branch="feature", repo="jss367/doxograph"):
    return {
        "id": 1000 + run_number, "run_number": run_number, "event": "pull_request",
        "head_sha": head, "head_branch": branch, "head_repository": {"full_name": repo},
        "pull_requests": [{"number": pr}],
        "display_title": f"Tests for pull request #{pr} against {base}",
        "created_at": created, "status": "completed", "conclusion": "success",
    }


def ci_run(*runs, retargeted=""):
    out = jq("ci_run.jq", [{"workflow_runs": list(runs)}], head=HEAD,
             source_repo="jss367/doxograph", source_branch="feature", retargeted=retargeted,
             number=7, target_title="Tests for pull request #7 against main")
    return json.loads(out)


def test_the_newest_matching_run_is_chosen():
    assert ci_run(run(3), run(5), run(4))["run_number"] == 5


def test_another_prs_run_on_the_same_head_is_ignored():
    assert ci_run(run(3), run(9, pr=8))["run_number"] == 3


def test_a_run_against_an_old_base_is_ignored():
    assert ci_run(run(3), run(4, base="release"))["run_number"] == 3


def test_a_run_from_a_fork_with_the_same_branch_name_is_ignored():
    assert ci_run(run(3, repo="someone/doxograph")) is None


def test_runs_from_before_a_retarget_are_ignored():
    assert ci_run(run(3, created="2026-10-26T16:00:00Z"),
                  retargeted="2026-10-26T16:10:00Z") is None


def test_no_matching_run_gives_null():
    assert ci_run(run(3, head=OLD)) is None


def job(attempt, conclusion, id_, name="Python and browser tests", status="completed"):
    return {"id": id_, "name": name, "run_attempt": attempt, "status": status,
            "conclusion": conclusion}


def jobs_pass(*pages):
    return jq("ci_jobs.jq", [{"jobs": list(page)} for page in pages], exit_status=True)


def test_a_passing_job_passes():
    assert jobs_pass([job(1, "success", 1)])


def test_a_rerun_that_failed_overrides_an_earlier_success():
    assert not jobs_pass([job(1, "success", 1), job(2, "failure", 2)])


def test_a_rerun_that_passed_overrides_an_earlier_failure():
    assert jobs_pass([job(1, "failure", 1)], [job(2, "success", 2)])


def test_a_rerun_still_in_progress_blocks():
    assert not jobs_pass([job(1, "success", 1), job(2, None, 2, status="in_progress")])


def test_a_skipped_job_blocks():
    assert not jobs_pass([job(1, "skipped", 1)])


def test_a_missing_job_blocks():
    assert not jobs_pass([job(1, "success", 1, name="lint")])
