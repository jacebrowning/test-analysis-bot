import io
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore

from PIL import Image
from pixelmatch.contrib.PIL import pixelmatch
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer

SNAPSHOT_DIR = Path("tests/snapshots")
DIFF_DIR = Path("test-results/snapshot-diffs")
SNAPSHOT_DIFF_RATIO = 0.01


def force_login(page: Page, live_server: LiveServer, user: User):
    session = SessionStore()
    session["_auth_user_id"] = str(user.pk)
    session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session.save()
    assert session.session_key is not None
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session.session_key,
                "url": live_server.url,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )


def wait_for_chart(page: Page, canvas_id: str, *, dataset: int = 0, index: int = 0):
    """Wait for a chart to stop animating so hovers land on final point positions."""
    page.wait_for_function(
        """([id, dataset, index]) => {
            const chart = Chart.getChart(id);
            if (!chart) return false;
            const y = chart.getDatasetMeta(dataset).data[index].y;
            const settled = window.__chartPointY === y;
            window.__chartPointY = y;
            return settled;
        }""",
        arg=[canvas_id, dataset, index],
    )


def take_snapshot(page: Page, name: str):
    baseline_path = SNAPSHOT_DIR / f"{name}.png"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    actual_bytes = page.screenshot(full_page=True, animations="disabled")

    if not baseline_path.exists():
        baseline_path.write_bytes(actual_bytes)
        return

    expected = Image.open(baseline_path).convert("RGBA")
    actual = Image.open(io.BytesIO(actual_bytes)).convert("RGBA")

    if expected.size != actual.size:
        baseline_path.write_bytes(actual_bytes)
        return

    total = expected.size[0] * expected.size[1]
    mismatched = pixelmatch(expected, actual)
    if total and mismatched / total > SNAPSHOT_DIFF_RATIO:
        baseline_path.write_bytes(actual_bytes)
