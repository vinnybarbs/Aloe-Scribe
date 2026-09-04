# Releasing Aloe Scribe

How a release ships on each platform, and the privacy rules every release
must pass. Read the privacy section first. It exists because v1.0.0 for Mac
shipped the maintainer's personal config file and had to be recalled the
same day.

## The privacy rule

The product's entire pitch is that nothing leaves the user's machine. A
release that leaks the builder's own data kills that pitch, so the rule is
absolute.

1. A build ships `config/config.toml.example` and nothing else from
   `config/`. The live `config.toml` is the builder's personal settings
   (folders, devices, calendar URL) and must never enter a bundle,
   installer, or archive.
2. The template ships with `local_dir = ""` and `ical_url = ""`. The app
   starts with no output folder on purpose. The user picks one.
3. User settings live outside the app: `~/Library/Application Support/Aloe
   Scribe/config.toml` on Mac, `%APPDATA%\Aloe Scribe\config.toml` on
   Windows. The app seeds them from the template on first launch, always
   with explicit UTF-8 (frozen Python defaults to ASCII and corrupts the
   seed otherwise).
4. Meeting transcripts from dev runs (`20*-*.md` in the repo root) are
   gitignored. Never force-add one.
5. No real employer names, usernames, or device setups in code comments or
   docs. Use invented examples.

These rules are enforced, not just written down. `scripts/release-mac.sh`
has an audit step that fails the release if the bundle's config directory
holds anything but the pristine template or contains a personal marker.
The Windows CI has the same check after the PyInstaller build. If an audit
fails, fix the cause. Do not weaken the check.

## Mac release

One command, run on the maintainer's Mac:

```bash
bash scripts/release-mac.sh <version>
```

It builds the app, signs every nested binary inside-out with the Developer
ID certificate, runs the privacy audit, packages the DMG, submits it to
Apple's notary service and waits for the automated scan (a few minutes,
no human review), staples the ticket, publishes the GitHub release with a
SHA-256, and updates `site/version.json` so the website's download button
points at the new DMG. Prerequisites, both already set up: the Developer
ID Application certificate in the keychain and the `aloe-notary`
keychain profile.

There is no App Store involvement. Nothing is re-uploaded to Apple beyond
the notarization scan, and the certificate is reused until it expires in
2031.

If a shipped release must be pulled:

```bash
gh release delete mac-v<version> --yes --cleanup-tag
```

then remove the `mac` entry from `site/version.json` and push. The site
falls back to the terminal install command until the next release restores
the entry.

## Windows release

Built by CI, not locally. Trigger the `windows-ci` workflow (workflow
dispatch) on GitHub. It builds the .exe with PyInstaller, runs the privacy
audit, compiles the Inno Setup installer, and uploads
`AloeScribeSetup.exe` as an artifact. Publish it under a `windows-v<n>`
release tag with its SHA-256 in the notes, then update the `windows` entry
in `site/version.json` by hand and push.

The installer is unsigned until Azure Trusted Signing is set up (about ten
dollars a month), so testers see SmartScreen warnings. That is the known
gap for Windows v2.

## After any release

Download the published file from the release URL and check it the way a
stranger would. Mount the DMG or run the installer, confirm the app starts
with no folder chosen, and confirm the bundled config directory holds only
the example template.
