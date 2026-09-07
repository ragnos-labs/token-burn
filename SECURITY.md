# Security

token-burn is a local developer tool. It is not an execution sandbox, a secret
redactor, a distributed lock service, or an authorization boundary against code
running as the same user.

## Report a vulnerability

Use GitHub's private vulnerability reporting on this repository's Security tab
when it is available. Do not post credentials, private archives, raw journals,
or exploitable details in a public issue. If private reporting is unavailable,
open an issue requesting a private reporting channel without disclosing details.

The current 0.0.10 alpha is the supported release line. Safety and data-loss reports
should include a minimal synthetic reproduction, platform and Git/Python versions.

## Trust boundaries

- Commands run with the caller's environment and ordinary authority.
- Journals, raw logs and Git archives can contain sensitive data. Keep them
  outside source control and shared ingestion. File permissions are not encryption.
- Network export is opt-in and confined to an explicitly configured local
  collector. The collector owns remote credentials and destinations.
- Telemetry and recovery artifacts never authorize a destructive action.
- The Python process, local OS, Git executable and supplied ownership adapter
  are trusted. Hashes detect content disagreement, not a malicious same-user
  actor who controls the process and all stored evidence.
- Local locks and last-moment checks cannot fence noncooperating filesystem writers.

Recovery refuses primary checkouts, unknown ownership, changed targets, hidden
index state, incomplete clones, and unavailable process scans. Do not work around
those holds with forced deletion. Review [the recovery contract](docs/recovery.md)
and choose an appropriate owner-controlled recovery procedure.

The package does not delete old evidence automatically. Retention and secure
disposal belong to the caller's storage policy.
