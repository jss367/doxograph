# Block when any reviewer's standing verdict is "changes requested".
# Input: the PR's reviews, as `gh api --paginate --slurp` pages.
# Prints "ok" or the reason to skip.
#
# As on GitHub, a reviewer's standing verdict is their latest approval,
# change request, or dismissal. Plain comments neither set nor clear it.
[ .[][]
  | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED") ]
| group_by(.user.login)
| map(max_by([.submitted_at, .id]))
| [ .[] | select(.state == "CHANGES_REQUESTED") | .user.login // "unknown" ] as $blocking
| if $blocking == [] then "ok"
  else "changes requested by \($blocking | join(", "))"
  end
