# Block on review threads someone other than Codex left open on current code.
# Input: the PR's review threads, as `gh api graphql --paginate --slurp`
# pages. Arg: $author (Codex's GraphQL login, without the [bot] suffix).
# Prints "ok" or the reason to skip.
#
# Codex's own threads are gated by its structured state instead
# (has_unresolved_findings in codex_state.jq) and by the 👍 postdating its
# latest review, since it often leaves a fixed finding's thread open.
# Outdated threads sit on lines the PR has since changed, so they are left to
# the reviewer to reopen. A thread with no readable author fails closed.
[ .[].data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved == false and .isOutdated == false)
  | (.comments.nodes[0].author.login // null) as $by
  | select($by != $author)
  | "\($by // "unknown") at \(.path):\(.line // "?")" ] as $open
| if $open == [] then "ok"
  else "\($open | length) unresolved review thread(s): \($open | join(", "))"
  end
