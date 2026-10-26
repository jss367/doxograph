# Pick the newest test-suite run for this PR's exact head, source branch, and
# target. Input: workflow runs, as `gh api --paginate --slurp` pages.
# Args: $head, $source_repo, $source_branch, $retargeted (time of the last
# base change, or ""), $number (the PR), $target_title (the run-name the test
# suite gives a run for this PR and base). Prints the run, or null.
#
# Another PR using the same head can test a different base's merge ref, so
# its success is insufficient. The run name captures its target at creation
# time, which disambiguates old and new targets when GitHub timestamps a
# retarget and the run it triggers in the same second.
[ .[].workflow_runs[]
  | select(.head_sha == $head and .event == "pull_request")
  | select(.head_repository.full_name == $source_repo and .head_branch == $source_branch)
  | select(any(.pull_requests[]?; .number == $number))
  | select(.display_title == $target_title)
  | select($retargeted == "" or .created_at >= $retargeted) ]
| max_by(.run_number) // null
