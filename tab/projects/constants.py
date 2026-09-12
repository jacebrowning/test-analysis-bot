import re
from datetime import timedelta

ALL_BRANCHES = "all"
DEFAULT_SUITE = "default"

ANSI_ESCAPE = re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")
PYTEST_DIFF_PLUS = re.compile(r"^(E\s+)(\s*\+.*)$")
PYTEST_DIFF_MINUS = re.compile(r"^(E\s+)(\s*\-.*)$")
UNIFIED_DIFF_PLUS = re.compile(r"^(\s*)(\+.*)$")
UNIFIED_DIFF_MINUS = re.compile(r"^(\s*)(-.*)$")
PYTEST_APPROX_OBTAINED = re.compile(r"^(E\s+)(Obtained:.*)$")
PYTEST_APPROX_EXPECTED = re.compile(r"^(E\s+)(Expected:.*)$")
CHECKOUT_COMMAND = (
    "git fetch origin && git checkout {branch} && git reset --hard origin/{branch}"
)

PENDING_THRESHOLD = timedelta(minutes=15)  # minimum duration before reporting failures
EXPIRED_THRESHOLD = PENDING_THRESHOLD * 2  # maximum duration to expect suite runs
FAILURE_RATE_EPSILON = 0.001  # small value to keep tests disabled for a bit longer
RESTORATION_THRESHOLD = timedelta(days=3)  # minimum duration to keep tests disabled

DURATION_CACHE_KEY = "projects:duration"
DURATION_CACHE_TIMEOUT = timedelta(minutes=1).total_seconds()

ACTIVE_BRANCHES_CACHE_KEY = "projects:active_branches"
ACTIVE_BRANCHES_CACHE_TIMEOUT = int(timedelta(hours=1).total_seconds())


def get_default_branches():
    return ["main"]
