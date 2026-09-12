TEST_PROMPT = """
The following is exported from the Test Analysis Bot (TAB).

Use this information to:

1. Classify the failure
2. Assess whether it is a real regression, flaky, or infra-related
3. Explain which fields most strongly support that conclusion
4. Suggest the most likely causes
5. Propose a minimal fix if appropriate
6. Verify the fix or ask the user to do so

## Test identity

- Repository: my-org/my-repo
- Name: my-suite › my-test
- Markers: ["disabled"]
- Date created: 2024-01-01T12:00:00+00:00
- Added in branch: my-branch
- Added in commit: abc123
- Maintainer: maintainer@example.com

## Historical signals

- Failure rate: 9.9%
- Block rate: 8.8%
- Average duration: 4.2s

_Failure rate: Total failure rate on significant branches including reruns_
_Block rate: Effective failure rate with reruns and ignored failures excluded_
_Average duration: Seconds duration from recent runs on significant branches_

## Override behavior

- Disabled: true
- Disabled since: 2024-06-01T12:00:00+00:00
- Reason: Waiting on infra.
- Tracker: https://example.com/ticket/1
- Last updated by: user@example.com

_TAB has a feature to suppress failures in known broken or flaky tests._
_This turns blocking failures into a non-blocking status to let PRs merge._

## Result details

- Status: failed
- Reported at: 2024-01-01T12:00:00+00:00
- Duration: 12.3s
- Branch: my-branch
- Commit: abc123
- Target: Desktop
- Platform: macOS
- Browser: Chromium
- New failure: true

_New failure: History data indicates the test is only blocking this branch._

## Failure message

```text
AssertionError: expected 1 == 2
```

## Rerun locally

```shell
git fetch origin && git checkout my-branch && git reset --hard origin/my-branch


# then


make test-e2e-desktop E2E_GREP="my-suite.*my-test"
```

_Use this to reproduce the failure and validate fixes._
_Do not discard uncommitted changes without user approval._

## Additional logs

```json
[
  {
    "step": "build",
    "rc": 1
  }
]
```
""".lstrip()
