import plistlib
from pathlib import Path

from doxograph import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_app_and_package_share_one_version():
    # The app warns when the server it starts reports a different release, so
    # a release that bumps one and not the other ships that warning.
    with (ROOT / "native" / "Info.plist").open("rb") as f:
        plist = plistlib.load(f)
    assert plist["CFBundleShortVersionString"] == __version__
