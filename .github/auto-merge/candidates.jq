# First-pass triage over one page of the open-PR GraphQL query. Prints one
# line per PR: "<number>\tskip\t<reason>" or "<number>\tcandidate\t<head>".
# Args: $bot (REST login with [bot]), $author (GraphQL review author login).
#
# --paginate advances pullRequests, not the nested reactions, so a PR
# carrying more thumbs-up than the page holds goes through as a candidate on
# the strength of totalCount alone. The authoritative reaction lookup in the
# workflow then decides it.
.data.repository as $repo
| $repo.pullRequests.nodes[]
| .number as $num
| ([.reactions.nodes[]
    | select(.user.login == $bot) | .createdAt] | sort | last) as $thumbedAt
| ([.reviews.nodes[]
    | select(.author.login == $author) | .submittedAt] | sort | last) as $findingsAt
| (.reactions.totalCount > (.reactions.nodes | length)) as $truncated
| if .isDraft then "\($num)\tskip\tdraft"
  elif [.labels.nodes[].name] | index("no-automerge") then
    "\($num)\tskip\tlabelled no-automerge"
  elif .baseRefName != $repo.defaultBranchRef.name then
    "\($num)\tskip\ttargets \(.baseRefName), not \($repo.defaultBranchRef.name)"
  elif .mergeable != "MERGEABLE" then
    "\($num)\tskip\tnot mergeable (\(.mergeable))"
  elif $thumbedAt == null and ($truncated | not) then
    "\($num)\tskip\tno 👍 from Codex yet"
  elif $thumbedAt != null and $findingsAt != null and $findingsAt >= $thumbedAt then
    "\($num)\tskip\t👍 does not postdate Codex's latest findings"
  else "\($num)\tcandidate\t\(.headRefOid)"
  end
