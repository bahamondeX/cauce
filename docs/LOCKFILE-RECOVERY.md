# Home lockfile recovery investigation

Milestone 9 rewrote `/Users/oscarbahamonde/uv.lock`. Milestone 10 checked
recovery sources read-only. The APFS Data volume reported zero snapshots.
The system volume listed OS update snapshots. Time Machine reported zero
configured destinations and no machine backup directory. The home directory
has no Git history. Spotlight and exact-path VS Code and Cursor local history
checks yielded no copy attributable to the pre-incident home lockfile. The
accessible Trash and iCloud paths yielded no matching candidate.

The pre-incident file remains unrecovered. The existing file was retained and
its observed SHA-256 is
`555739e7a99c4620a02e42a59241a57a0413779ec182f5bb3fa1a86ab6e81d2c`.
Other projects' lockfiles have separate provenance. The checks cover available
local recovery sources and provide no evidence about disconnected backups.

[Repository execution boundaries](../AGENTS.md).
