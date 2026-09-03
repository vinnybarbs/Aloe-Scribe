# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/vinnybarbs/Aloe-Scribe/security/advisories/new)
on this repository. You will get a first response within five business days.
Please do not open public issues for security reports.

## The product's network surface

Aloe Scribe processes all meeting content on the user's device. The complete
list of network calls the product makes:

1. Downloading the app and model files from this repository's GitHub
   releases, over HTTPS, at install and update time.
2. Checking this repository for updates when the user asks for an update.

No meeting audio, transcript, summary, voice profile, or analytics data is
transmitted anywhere, and the vendor operates no servers. Changes to this
list will be documented here and in release notes.

## Dependency and code scanning

GitHub vulnerability alerts with automated security fixes, CodeQL analysis,
and a weekly pip-audit of both platform dependency sets
(.github/workflows/security-audit.yml).
