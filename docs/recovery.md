# Recovery contract

The recovery commands operate on one explicitly named linked Git worktree.
The caller authorizes the action. The toolkit checks whether the available
ownership, filesystem and recovery evidence supports doing it now.

## Ownership

`worktree claim` creates a local lease with a unique ID and generation. The
same ID is required to release it. A new claim after release creates a new ID
and advances the generation. Malformed records hold the worktree; they are not
silently repaired. No timer, expiry, background worker, or inference releases work.

An application can provide an `Authority` implementation instead of `LocalLeases`.
Its `inspect(Path)` returns `Approval(lease_id, generation, released)`. The
application owns the truth of those facts and any distributed writer fencing.
An API token, telemetry event, or archive path is not that authority.

The default local state directory is `~/.local/state/token-burn/worktrees`.
`TOKEN_BURN_WORKTREE_STATE_DIR` selects another private directory. Keep owner
records and recovery data available until your own retention policy retires them.

## What an archive contains

```text
recovery-001/
├── manifest.json    # Target identity, owner generation and file snapshot
├── history.bundle   # Original HEAD history and a staged snapshot commit
├── files.tar.gz     # Tracked, untracked and ignored leaf contents and modes
└── complete.json    # Digests of the three retained files
```

These files can contain source code, repository history and secrets. They are
created owner-private, with file permissions `0600` and directory permissions
`0700`. Store the directory outside the target and outside shared ingestion.
No archive command performs a remote push or uploads a file.

Archive creation:

1. Reads exact target identity and a released owner lease under the local mutex.
2. Snapshots all tracked leaf bytes and modes, index differences, untracked files
   and ignored files. Git-clean files are included because Git configuration can
   hide mode differences and line-ending conversion.
3. Uses a private copy of the Git index to create a staged tree and recovery commit.
4. Anchors a unique local recovery ref and writes a self-contained Git bundle.
5. Saves actual leaf bytes and modes, verifies payloads and flushes the files.
6. Restores the archive into a fresh temporary repository with an independent
   object store, checks the staged patch, leaf bytes and modes, and runs Git fsck.
7. Rechecks the original HEAD, index bytes, target contents and owner lease.
8. Removes only its own temporary ref after success. On failure it retains the
   recovery files and any anchored ref; a complete-looking directory is not a
   successful operation receipt.

Default limits are 10,000 archived paths and 512 MiB. The byte cap covers both
retained archive size and expanded leaf payload size. Git bundle creation can
write a larger file before the completed size is checked; this is not a disk quota.
Creation failures leave inspectable partial artifacts. Do not automatically remove
them as part of handling the failure.

## Removal

`worktree remove` previews the checks. `--apply` performs the action using fresh
evidence. The preview is not a reusable removal authorization.

The actual removal acquires a single-use permit under the local repository mutex,
verifies the archive and proves restoration, then rechecks the target, owner,
generation, bytes, modes, Git locks and process activity immediately before
calling Git. Released, consumed, expired, wrong-target or stale permits fail.
Branches, remote refs, recovery archives and unrelated checkouts remain intact.

Only registered linked worktrees are eligible. Primary checkouts, orphan folders,
locked worktrees, submodules, sparse/hidden index state, incomplete clones,
alternate object stores and locally configured Git command filters require a
separate recovery procedure. Recovery Git commands ignore global/system Git
configuration, disable hooks and allow only local file transport.

The process check covers a cwd at or below the target. macOS also checks open
files in the root directory. Missing tools, unreadable process information, or
an incomplete scan hold removal. Linux systems that restrict access to `/proc`
may therefore hold cleanup. The toolkit never escalates privileges to bypass a hold.

## Restore

`worktree restore ARCHIVE --destination NEW_PATH` creates an independent repository.
It checks out the original HEAD, restores the saved staged tree into the index,
and overlays verified working file contents. No original checkout or remote is
required. Existing destinations are refused; a failed restore can leave a partial
new destination for inspection.

Restoration covers file contents, staged state, symlink targets and file modes.
It does not preserve branch checkout identity, reflogs, local Git configuration,
ACLs, extended attributes, directory metadata or empty untracked directories.
Hashes establish content consistency, not authenticity against a malicious user
who controls both the files and the running process.

Local locks coordinate cooperating callers. Unrelated filesystem writers can
still race a final check. Systems requiring stronger exclusion must supply it
through their existing ownership and execution controls.
