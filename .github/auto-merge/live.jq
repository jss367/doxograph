# Recheck mutable PR state after the slower review lookups.
# Input: `gh api repos/{repo}/pulls/{n}`. Args: $head (the commit every
# earlier check vouched for), $default (the default branch).
# Prints "ok" or the reason to skip.
#
# GitHub only atomically guards the head SHA (via --match-head-commit), so
# this narrows but cannot close the race for base, draft status, and labels.
if .state != "open" then "closed"
elif .draft != false then "draft"
elif [.labels[].name] | index("no-automerge") then "labelled no-automerge"
elif .head.sha != $head then "head moved to \(.head.sha)"
elif .base.ref != $default then "base changed to \(.base.ref)"
else "ok"
end
