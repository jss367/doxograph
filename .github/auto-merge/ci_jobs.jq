# Require every required job's newest execution to have passed.
# Input: a run's jobs (?filter=all), as `gh api --paginate --slurp` pages.
# Exits nonzero (with jq -e) unless they all passed.
#
# Rerun-failed-jobs can retain a successful job from an earlier attempt.
# Require each job's newest execution, not an arbitrary earlier success or
# only the jobs retried in the final attempt.
[ .[].jobs[] ] | group_by(.name)
| map(max_by([.run_attempt, .id])) as $jobs
| ["Python and browser tests", "Static quality checks", "Native macOS checks"] | all(. as $name |
    any($jobs[]; .name == $name and .status == "completed" and .conclusion == "success"))
