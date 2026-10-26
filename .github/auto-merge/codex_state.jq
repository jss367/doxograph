# Decide whether Codex's review summary vouches for the head commit.
# Input: the PR's issue comments, as `gh api --paginate --slurp` pages.
# Args: $bot (REST login), $head (40-character head SHA), $thumbs (time of
# Codex's latest 👍). Prints "ok" or the reason to skip.
#
# The 👍 carries no commit, so the commit comes from the machine-readable
# state Codex embeds in its summary comment:
#   <!-- codex-review-state:v1 {"code_review":{"commit_sha":...,"status":...,
#        "has_any_findings":...,"has_unresolved_findings":...},
#        "security_review":{...}} -->
# Anything missing, unparsable, or not exactly as expected fails closed.
def latest_summary:
  [ .[][] | select(.user.login == $bot)
    | select(.body | contains("codex-pull-request-review-summary")) ]
  | sort_by(.updated_at) | last;

# A summary without the marker yields no match, hence the trailing // null.
def parse_state:
  (try (.body
        | capture("<!-- codex-review-state:v1 (?<json>\\{.*?\\}) -->")
        | .json | fromjson)
   catch null) // null;

def clean: .has_any_findings == false and .has_unresolved_findings == false;

latest_summary as $summary
| if $summary == null then "no Codex review summary"
  # Second precision on both sides, so a tie means the order is unknown.
  elif ($thumbs > $summary.updated_at | not) then
    "👍 predates the latest review summary"
  else
    ($summary | parse_state) as $state
    | ($state.code_review? // null) as $code
    | if ($state | type) != "object" or ($code | type) != "object" then
        "Codex summary has no readable review state"
      elif $code.commit_sha != $head then
        "Codex reviewed \($code.commit_sha // "no commit"), not \($head)"
      elif $code.status != "completed" then
        "Codex code review is \($code.status // "not started")"
      elif ($code | clean | not) then
        "Codex code review of \($head) has findings"
      else
        # Other review types (today only security_review) run only when
        # someone asks for them, so they need not cover the head commit.
        # One that was requested must have finished with nothing open.
        [ $state | to_entries[] | select(.key != "code_review")
          | select((.value | type) != "object"
                   or (.value.status != null
                       and (.value.status != "completed"
                            or .value.has_unresolved_findings != false)))
          | .key ] as $blocking
        | if $blocking == [] then "ok"
          else "Codex \($blocking | join(", ")) is not finished and clean"
          end
      end
  end
