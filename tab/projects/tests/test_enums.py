import pytest

from ..enums import Browser, Platform, Status, Target


def describe_status(expect):
    def describe_normalize(expect):
        def it_detects_expected_failures():
            expect(
                Status.normalize(
                    "failed",
                    markers=["fail"],
                    message="",
                    error_indicators=[],
                    skipped_indicators=[],
                )
            ) == "xfailed"

        def it_detects_unexpected_passes():
            expect(
                Status.normalize(
                    "passed",
                    markers=["fail"],
                    message="",
                    error_indicators=[],
                    skipped_indicators=[],
                )
            ) == "xpassed"

        @pytest.mark.parametrize("marker", ["fixme", "disabled"])
        def it_detects_disabled_tests(marker):
            options: dict = dict(
                markers=[marker],
                message="",
                error_indicators=[],
                skipped_indicators=[],
            )
            expect(Status.normalize("failed", **options)) == "disabled"
            expect(Status.normalize("error", **options)) == "disabled"
            expect(Status.normalize("timedOut", **options)) == "disabled"
            expect(Status.normalize("skipped", **options)) == "disabled"
            expect(Status.normalize("passed", **options)) == "passed"

        def it_detects_setup_errors():
            expect(
                Status.normalize(
                    "failed",
                    markers=[],
                    message="Call log:\n  - waiting for getByTestId('overlay-menu')",
                    error_indicators=["waiting for getByTestId('overlay-menu')"],
                    skipped_indicators=[],
                )
            ) == "error"

        def it_detects_skipped_tests():
            expect(
                Status.normalize(
                    "passed",
                    markers=[],
                    message="Skipping disabled sample: multi-axis-robot",
                    error_indicators=[],
                    skipped_indicators=["Skip"],
                )
            ) == "skipped"


def describe_target(expect):
    def it_normalizes_values():
        expect(Target.normalize("browser")) == "web"
        expect(Target.normalize("web")) == "web"
        expect(Target.normalize("Website")) == "web"
        expect(Target.normalize("desktop")) == "desktop"
        expect(Target.normalize("Electron")) == "desktop"


def describe_platform(expect):
    def it_normalizes_values():
        expect(Platform.normalize("macOS")) == "macos"
        expect(Platform.normalize("darwin")) == "macos"
        expect(Platform.normalize("Windows")) == "windows"
        expect(Platform.normalize("win32")) == "windows"
        expect(Platform.normalize("linux")) == "linux"
        expect(Platform.normalize("Ubuntu")) == "linux"


def describe_browser(expect):
    def it_capitalizes_known_names():
        expect(Browser.normalize("chromium")) == "Chromium"
        expect(Browser.normalize("CHROME")) == "Chrome"
        expect(Browser.normalize("firefox")) == "Firefox"
        expect(Browser.normalize("webkit")) == "WebKit"
        expect(Browser.normalize("Safari")) == "Safari"
        expect(Browser.normalize("msedge")) == "Edge"

    def it_leaves_unknown_names_unchanged():
        expect(Browser.normalize("chrome-beta")) == "chrome-beta"
        expect(Browser.normalize("MyHeadless")) == "MyHeadless"
