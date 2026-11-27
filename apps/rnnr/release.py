"""Produce a signed, notarized universal DMG and a matching Homebrew cask."""
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent


def run(*args):
    subprocess.run(args, check=True)


def main():
    version = os.environ.get("RNNR_VERSION", "")
    identity = os.environ.get("RNNR_SIGN_IDENTITY", "")
    profile = os.environ.get("RNNR_NOTARY_PROFILE", "")
    if not re.fullmatch(r"0\.[0-9]+\.[0-9]+", version) or not identity.startswith("Developer ID Application: ") or not profile:
        sys.exit("Set RNNR_VERSION, RNNR_SIGN_IDENTITY and RNNR_NOTARY_PROFILE for the release.")
    run(sys.executable, ROOT / "build.py")
    app = ROOT / ".build/rnnr.app"
    run(sys.executable, ROOT / "check_bundle.py", app)
    with (app / "Contents/Info.plist").open("rb") as source:
        assert plistlib.load(source)["CFBundleShortVersionString"] == version
    with tempfile.TemporaryDirectory(dir=ROOT / ".build") as temp:
        stage = Path(temp)
        zipfile = stage / "app.zip"
        run("ditto", "-c", "-k", "--keepParent", app, zipfile)
        run("xcrun", "notarytool", "submit", zipfile, "--keychain-profile", profile, "--wait")
        run("xcrun", "stapler", "staple", app)
        run("xcrun", "stapler", "validate", app)
        run("spctl", "--assess", "--type", "execute", app)
        contents = stage / "contents"
        contents.mkdir()
        run("ditto", app, contents / "rnnr.app")
        (contents / "Applications").symlink_to("/Applications")
        name = f"rnnr-{version}-universal.dmg"
        dmg = stage / name
        run("hdiutil", "create", "-volname", "rnnr", "-srcfolder", contents, "-format", "UDZO", dmg)
        run("codesign", "--timestamp", "--sign", identity, dmg)
        run("xcrun", "notarytool", "submit", dmg, "--keychain-profile", profile, "--wait")
        run("xcrun", "stapler", "staple", dmg)
        run("xcrun", "stapler", "validate", dmg)
        run("hdiutil", "verify", dmg)
        with dmg.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        release = ROOT / ".build" / f"release-{version}"
        release.mkdir(exist_ok=False)
        dmg.rename(release / name)
        (release / "rnnr.rb").write_text(
            f'cask "rnnr" do\n  version "{version}"\n  sha256 "{digest}"\n\n'
            '  url "https://github.com/agenthangar/t/releases/download/rnnr-v#{version}/rnnr-#{version}-universal.dmg"\n'
            '  name "rnnr"\n  desc "Coding agent sessions in the menu bar"\n'
            '  homepage "https://getrnnr.ai/"\n  depends_on macos: ">= :ventura"\n\n'
            '  app "rnnr.app"\n  binary "#{appdir}/rnnr.app/Contents/Resources/backend/bin/rn"\nend\n')
        print(release)


if __name__ == "__main__":
    main()
