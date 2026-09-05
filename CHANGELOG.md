# Changelog

All notable changes to Aloe Scribe are documented in this file. Dates are in
UTC. Versions refer to the macOS `mac-v*` releases; Windows and Linux ship
from the same `main` branch without separate version tags yet.

## Unreleased

- Download page on aloescribe.ai now captures email addresses into our own
  database instead of just linking out.

## 1.0.2 - 2026-09-04

- Fixed the post-move DMG eject: the app now retries ejecting the DMG after
  a move to Applications, since the first attempt can lose a race with
  Gatekeeper.
- The app offers to move itself to Applications when launched straight from
  the mounted DMG, since settings cannot save from the read-only DMG.
- Tightened the CI privacy audit to use an exact string compare.
- README rewrite: download-first structure, documents the new config
  location, and reflects the current release pipeline.

## 1.0.1 - 2026-09-04

- Privacy guardrails: releases are audited before publishing, app bundles
  ship only the config template (never a real config), and Windows config
  moved to `%APPDATA%`.
- Fixed a frozen-app launch bug: config seeding now forces UTF-8, and a
  missing `output_dir` no longer crashes startup.
- Cleaned up a leftover example path in a code comment.

## 1.0.0 - 2026-09-04

First tagged macOS release.

- Settings now live in `~/Library/Application Support/Aloe Scribe/` instead
  of inside the app bundle, so they survive rebuilds and updates.
- The app never picks a default save location. It ships with no output
  folder chosen and refuses to record until you choose one, and the bundle
  ships only the config template, never a real config.
- Notarization fixed in two rounds: removed stale PyQt6 remnants, signed
  framework binaries directly, pruned PySide6, and signed the bundle
  inside-out with the right entitlements.
- The bundled app keeps its own icon in the Dock instead of a generic one.

## Earlier changes (pre-tagging)

Work before the first tagged release, in the order it landed:

- Marketing site: launched aloescribe.ai on GitHub Pages, added comparison
  pages against Otter, MacWhisper, and Granola, an FAQ, SEO and credibility
  passes, and a two-tone wordmark matching the app.
- Audio pipeline: switched native mic capture off ffmpeg (it was
  time-compressing the mic channel by 11%), added a shared level meter for
  both audio sources reading in decibels, gated meters at the room noise
  floor, and made voice processing opt-in with passive native capture as
  the default.
- Reliability: added a VPIO watchdog so silence during a call is not
  treated as failure, and an in-app Update button that closes, updates,
  and relaunches with no terminal needed.
- Speaker handling: merges leftover anonymous speaker clusters once the
  attendee roster is fully assigned, restricts voice-processing capture to
  the built-in mic, and detects silence.
- Meeting notes: fixed notes-window annoyances (field resets, one-stroke
  completion, a final popup), added a faithfulness pass so the Summary and
  Action items stay grounded in the actual notes, and added Obsidian vault
  mode with wikilinked attendees and a meeting tag.
- UI: swapped PyQt6 for PySide6 across the app, styled checkboxes in the
  brand palette, and added a local-summary toggle in settings.
- Decisions: decisions in meeting notes now record what was chosen rather
  than what was rejected, require explicit group agreement, and are
  verified so a rejected option can never read back as adopted.
- Security and release process: added a security page and disclosure
  policy, a weekly pip-audit workflow for both dependency sets, and the
  Mac release pipeline (signing, audit, notarization, publish), gated to
  fire once a Developer ID account was available.
