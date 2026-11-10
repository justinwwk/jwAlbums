# Read-only Security Audit: jwAlbums

Date: 2026-11-03 · Code under audit: this repository · Dependency: `pymobiledevice3==11.49.1`
(official PyPI wheel, SHA-256 `351add0a5c1f988b5f6f8894dddeafe444a82dcae3d23a2df2c07608ea78a7ca`,
verified at install).

## Conclusion

No path was found by which the application can write to, delete from, rename on, or
otherwise change the iPhone's filesystem. The application also never initiates pairing,
so it never causes the phone to store a new pairing record. At runtime the only network
traffic is the loopback connection to Apple's usbmux service.

Three independent review passes found several Windows-side data-safety defects, listed
below. All of them are fixed and covered by regression tests. The latest review
(2026-11-10, end of this document) found no exploitable issue for anyone outside the
trust boundary below.

## Threat model

**Trusted (out of scope):** anyone who can use this PC's Windows account, an administrator,
and anyone who can modify the app's source, `.venv` or EXE. They already control the
photos, the exports and the phone's pairing (through Apple Devices), with or without this
app, so no application-level control can stop them. Malware running on the PC falls in the
same group.

**Untrusted (in scope):**

| Attacker | What they control |
|---|---|
| Internet / local network | Nothing, unless the app listens or connects |
| Other people whose content ends up on the iPhone | Photo and video bytes (AirDrop, Messages, WhatsApp, downloads, shared albums) |
| A tampered or jailbroken iPhone | Everything the phone reports: Photos.sqlite, file names, sizes, listings |
| Collaborators on a shared export folder (e.g. a shared OneDrive folder) | Files in the destination, including `.jwAlbums\manifest.sqlite` |
| Upstream package authors / PyPI | The dependency code, at the time it is pinned |

## Method

1. Structural design review. Device access is confined to two modules. `iphone/readonly_afc.py`
   is the only module that imports `AfcService`. `iphone/device.py` (`ReadOnlyIPhone`) is the
   only API the rest of the app uses, and it exposes only read operations.
2. Every pymobiledevice3 call the app makes was traced to the AFC/Lockdown request it sends
   (table below).
3. Automated contract tests in `tests/test_readonly_contract.py` run with every build;
   `build.bat` refuses to build if they fail.
4. Independent reviews. An initial general review, then four reviewers with separate lenses
   (export ownership, snapshot, read-only/device, paths/network/UI). An independent skeptic
   tried to refute each of their findings, and only confirmed findings were acted on.

## Device-facing call inventory

| Our call | pymobiledevice3 call | Device request | Mutates? |
|---|---|---|---|
| `list_usb_udids` | `usbmux.list_devices` | usbmuxd ListDevices (local daemon) | No |
| `open_lockdown` | `create_using_usbmux(..., connection_type="USB", autopair=False)` | QueryType, GetValue, StartSession using the existing pair record | No (no Pair, SetValue or RemoveValue) |
| `ReadOnlyAfc.open` | `AfcService.__aenter__` | lockdown StartService `com.apple.afc` | No |
| `stat` / `exists` | `AfcService.stat` | AFC `GET_FILE_INFO` | No |
| `listdir` | `AfcService.listdir` | AFC `READ_DIR` | No |
| `fopen_read` | `AfcService.fopen(path, "r")` | AFC `FILE_OPEN` mode `RDONLY (0x1)` | No |
| `fread` | `AfcService.fread` (≤ 4 MiB) | AFC `READ` | No |
| `fseek` | `AfcService.fseek` | AFC `FILE_SEEK` | No |
| `fclose` | `AfcService.fclose` | AFC `FILE_CLOSE` | No |
| USB speed | `DeviceIoControl` on the parent **hub** | `IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX(_V2)` | No; it queries the Windows hub driver, not the phone |

Every device path is validated before it reaches AFC: relative, below `DCIM/` or
`PhotoData/` only, with no `..`, no backslash, no NUL and no drive letter.

Reading a file may update its access time on the phone, depending on how iOS mounts the
media volume. That is an inherent side effect of any read and is not a write by this app.

## Automated enforcement (`tests/test_readonly_contract.py`)

- `AfcService` / `pymobiledevice3.services.afc` is used only in `iphone/readonly_afc.py`.
- An **allow-list of every pymobiledevice3 import** per module. Any new import fails the
  test until it has been reviewed.
- No call to a mutation method anywhere in `iphone/`: `fwrite`, `write`, `remove`, `rm`,
  `rename`, `mkdir`, `rmdir`, `truncate`, `set_file_contents`, `push`, `upload`, `unlink`,
  `make_link`, `set_file_time`, `lock`, `set_value`, `remove_value`, `pair`, `unpair`, and
  others.
- No mutation call on any device-like receiver (`afc`, `iphone`, `device`, `dev`,
  `lockdown`) anywhere in the app.
- Lockdown and service calls that could change device state, and dynamic imports
  (`importlib`, `__import__`), are banned app-wide.
- The adapter forwards only `stat/listdir/exists/fopen/fread/fseek/fclose`, and `fopen`
  always uses the `"r"` constant.
- `create_using_usbmux` must be called with `autopair=False` and `connection_type="USB"`.
- The public API of `ReadOnlyAfc` and `ReadOnlyIPhone` contains no mutation methods.
- `test_device_access_is_read_only` drives the adapter against a recording fake
  `AfcService` that fails the test on any mutation attempt, and checks the open mode and
  the 4 MiB read clamp.

## Pairing and credentials

- `autopair=False`: the app relies on the trust that already exists through Apple Devices.
  An untrusted phone is reported as "Trust this computer"; the app does not start pairing.
- Pair records and keys are never stored in `%LOCALAPPDATA%\jwAlbums\`. The
  record itself stays where Apple keeps it (`C:\ProgramData\Apple\Lockdown`). During each TLS
  handshake pymobiledevice3 (`lockdown.py` `ssl_file`) writes the host certificate and key to a
  `%TEMP%` file (user-only) and deletes it right after; a killed process can leave it behind.
- pymobiledevice3 loggers are capped at WARNING, and a redaction filter strips PEM blocks
  and pairing keys (`HostPrivateKey`, `RootPrivateKey`, `EscrowBag`, `HostID`, `SystemBUID`,
  and others) from any log line.
- The export manifest stores a SHA-256 prefix of the UDID, not the UDID itself. The
  per-device folder under `%LOCALAPPDATA%\jwAlbums\` is named with the UDID.

## Network

`app/network_guard.py` is installed by both the GUI and CLI entry points. It refuses:

- non-loopback `socket.connect` / `connect_ex`
- non-loopback asyncio `sock_connect` (Proactor and Selector loops)
- `sendto` to non-loopback addresses on plain sockets (asyncio datagram endpoints and
  `loop.sock_sendto` are not wrapped; no code path the app runs uses them)
- name resolution for anything other than localhost (`getaddrinfo`, `gethostbyname*`,
  `gethostbyaddr`)

The app contains no telemetry, update checks or remote logging.

This guard is defense in depth, not a sandbox. A native extension could bypass Python's
socket layer. None of the modules the app loads makes network calls in the code paths
used.

## Findings and fixes

| # | Area | Finding | Fix |
|---|---|---|---|
| 1 | Pairing | `autopair=True` could start pairing, so the phone might store a pairing record, possibly an orphaned one after a timeout | `autopair=False` plus a `paired` check; enforced by a test |
| 2 | Contract tests | Tests could be bypassed through lockdown `set_value`/`pair` or other AFC-based services | pymobiledevice3 import allow-list, banned-call list, autopair check |
| 3 | Destination | Two iPhones exporting into one folder could resume each other's `.part` and produce a mixed, corrupted file | Name ownership is tracked across all devices; foreign `.part` files are never touched |
| 4 | Destination | A user's own `<name>.part` file could be deleted | Only `.part` files at names the manifest says we own are deleted |
| 5 | Destination | A same-size but different file was treated as "already exported" | Unrecorded files are adopted only if their head and tail bytes match the original |
| 6 | Resume | After a power loss, an unsynced `.part` tail could be trusted | fsync every 64 MiB; resume only from the recorded durable offset, and only for in-progress entries |
| 7 | Resume | A `DONE` entry's byte count was trusted for a stale `.part`, which could corrupt a good file on re-copy | Durable offset trusted only for `copying`/`failed` entries; stale `.part` removed on skip |
| 8 | Resume | An interrupted in-place re-copy produced numbered duplicates | Our own file is recognised by sample match, and the `.part` is resumed |
| 9 | Overwrite | "Skip existing" off could overwrite a user's same-size in-place edit | In-place replace only when the file still matches the original |
| 10 | Destination | A `.part` that is a symlink or junction could be followed | Links and non-regular files rejected; fresh `.part` opened with exclusive create |
| 11 | Destination | Different albums whose sanitized names collide shared one folder | Sibling folders made unique (`Trip (2)`) in stable album-id order |
| 12 | Paths | Non-idempotent sanitizing and over-long names could fail whole albums | Sanitizer is a fixed point (fuzzed with 20 000 inputs); length-safe numbering; clear "path too long" error |
| 13 | Reliability | A second disconnect during re-stat aborted the export | Re-stat moved inside the retry loop; retry count resets on progress |
| 14 | Reliability | One file with odd metadata was treated as a dead connection (endless reconnect loop) | Errors classified by transport type plus a raw liveness probe; reconnect loops bounded |
| 15 | Snapshot | A WAL/DB change mid-copy was fatal; WAL give-up deleted the complete DB copy; the optional SHM could fail the snapshot; a persistent read error was mislabelled | Retries kept the DB copy; SHM best effort; real read errors reported as such |
| 16 | Snapshot/index | A corrupt cached index leaked a file handle and blocked the rebuild | Connection closed on failure; index fsynced before replace |
| 17 | UI | A folder cycle in Photos.sqlite could stop UI message handling | Cycle flattened in the parser; tree insert tolerant; poll loop always re-armed |
| 18 | Network | Guard gaps (`gethostbyname`, UDP); guard missing on the CLI path | Guard extended and installed in the CLI |

Album join-table detection also now ignores Core Data `Z_FOK_*` ordering columns, so ordering
keys that coincide with asset primary keys cannot make detection ambiguous.

## Residual risks

- **Dependency trust.** pymobiledevice3 itself contains write-capable AFC code. The app
  never calls it, which the tests enforce, but a compromised dependency would bypass any
  application-level control. Mitigations: an exact pin, a hash-verified lock file, and no
  automatic updates.
- **Adoption heuristic.** An unrecorded existing file is considered identical if its size
  and 2 MiB of head and tail bytes match. A file that differs only in its middle would be
  skipped, never overwritten, so data loss is impossible.
- **Network guard** is Python-level defense in depth, not an OS firewall.

## Addendum (2026-11-09): edited versions, dates, database copy

- **Edited renders.** The planner additionally calls `listdir` on
  `PhotoData/Mutations/<DCIM dir>/<stem>/Adjustments` and reads `FullSizeRender.*` with the
  same read-only `stat`/`fopen("r")`/`fread` path as every other file. It does this only for
  assets the Photos database marks as edited, inside the selected albums. No new AFC
  operation was added; the read-only contract tests still pass unchanged.
- **Photos.sqlite copy.** The multi-GB copy contains all Photos metadata (GPS, people names,
  faces, memories). It is deleted right after the album index is built.
  The local copy is opened with SQLite `mode=ro`, so it is never modified while in use.
- **File dates.** `SetFileTime` is called on a handle opened with `FILE_WRITE_ATTRIBUTES` and
  `FILE_FLAG_OPEN_REPARSE_POINT`, only on the regular file the exporter just renamed into place
  inside the destination root.
- **In-place update of our own copy** happens only when the manifest says we finished that file
  and its size is still what we wrote; anything else gets a new name, so user files are never
  overwritten. (This relies on the manifest; see "Shared export folder" below.)

## Review (2026-11-10): realistic attackers

Two independent reviewers audited the whole code base (untrusted input and filesystem;
device, network, supply chain and EXE). Proof-of-concept scripts were run against the real
modules. Findings are rated against the threat model above.

**Internet / network: no attack surface.** The app opens no listening socket and makes no
outbound connection. Nothing contacts the network at import or startup: no telemetry, no update
check, no crash upload (pymobiledevice3 imports `requests`, but only an unused download helper
calls it). The guard in `app/network_guard.py` is installed before anything else in both
entry points.

**Content from other people: no attack surface.** Media bytes are copied, never decoded or
parsed. The app is no more exposed to a malicious image than a file copy is; any viewer that
later opens the file is a separate matter. File names are assigned by iOS when items are saved
to the library. Shared albums are not read (only album kinds 2 and 4000).

**The iPhone cannot be harmed.** Only read operations reach the device (contract tests,
10/10). No pairing (`autopair=False` returns before `pair()`), no backup, no other service.

**A tampered or jailbroken iPhone: robustness only, Low.**
- Every device path and every exported name is validated and sanitized. `..`, absolute,
  UNC, `\\?\`, drive letters, `:` streams, `CON`/`NUL`/`COM1` (with extensions and trailing
  spaces), trailing dots and over-long names are all neutralized. Nothing is written outside
  the destination. No SQL injection: identifiers are quoted, values are bound. SQLite opens
  the copy `mode=ro` with no extensions or ATTACH. Tk receives all text as data.
- A crafted Photos.sqlite with hundreds of extra columns can make schema detection run for
  minutes. Non-UTF-8 text fails the album read with an error. A crafted file name could use
  Unicode direction characters or a non-media extension. None of these is reachable from a
  normal iPhone.

**Shared export folder: no gain for the attacker, Low.** Someone who can edit the
destination can make the manifest claim one of the user's files as ours. The next export then
replaces or moves that file inside the album folder. Such a person can already delete or
change those files directly, so they gain nothing. If others can edit the export folder, treat
its contents as theirs too.

**Supply chain: mitigated.** All 100 packages are pinned with SHA-256 hashes and match the
installed environment. pymobiledevice3 matches the official wheel hash. Installs use
`--require-hashes --no-deps`. PyInstaller 6.22.3 includes the fixes for CVE-2023-49797 and
CVE-2025-59042. The residual risk is a package that was already malicious when it was pinned.
CVE status was not re-checked online.

**Out of scope by the threat model (recorded for completeness):**
- other Windows accounts can modify `D:\` (the default ACL), including this folder
- link or junction races in the destination by a logged-in user
- the unsigned EXE
- the database copy that stays after a cancelled read until the next Refresh (by design, to
  resume)
- album names in `album_index.sqlite` and the logs
