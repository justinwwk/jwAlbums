# Architecture

This document describes the design as implemented.
Design decisions and clarifications are listed at the end.

## Stack

- Python 3.12 x64, Tkinter/ttk GUI, `sqlite3`, `asyncio`
- `pymobiledevice3==11.49.1` (official PyPI wheel, SHA-256 verified) for usbmux, Lockdown and AFC
- Apple Devices for Windows provides the USB driver and `usbmuxd` (127.0.0.1:27015)
- PyInstaller produces `jwAlbums.exe` (GUI) and `jwAlbumsCLI.exe` (console)

```
Tk main thread  <--queue.Queue--  worker thread (own asyncio loop)
   ui/                                app/worker.py
                                         |
            +----------------------------+-----------------------------+
            |                            |                             |
     iphone/usb_speed.py          photos/refresh.py              export/planner.py
     (SetupAPI/CfgMgr +           snapshot -> schema ->          export/exporter.py
      hub IOCTLs)                 parser -> index_db             export/transfer.py
                                         |                             |
                                         +--------> iphone/device.py <-+
                                                    ReadOnlyIPhone
                                                         |
                                                 iphone/readonly_afc.py  (only AfcService import)
                                                         |
                                       pymobiledevice3: usbmux -> Lockdown -> com.apple.afc
                                                         |
                                                  /var/mobile/Media (DCIM/, PhotoData/)
```

## Modules

Paths below are relative to `src/`. The repository layout is described in the README.

| Module | Responsibility |
|---|---|
| `iphone/readonly_afc.py` | The only importer of `AfcService`. Forwards only `stat`, `listdir`, `exists`, `fopen(…,"r")`, `fread`, `fseek`, `fclose`. The service is held in a name-mangled slot. |
| `iphone/device.py` | `ReadOnlyIPhone`: `connect/disconnect/device_info/exists/stat/listdir/open_read/read/seek/close/ping`. Validates every path (only `DCIM/`, `PhotoData/`, no `..`/absolute/backslash). Maps library errors to `RemoteFileNotFound`, `RemoteFileError` (one bad file) or `DeviceConnectionLost` (session dead). Per-operation 60 s timeout. |
| `iphone/connection.py` | usbmux discovery (USB only; Wi-Fi entries ignored), friendly errors, wait-for-reconnect polling. |
| `iphone/usb_speed.py` | UDID → `USB\VID_05AC…\<serial>` devnode → parent hub + port → `IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX` (+`_V2` flags). Pure `classify_link`/`summarize`; any failure → `Unknown`. |
| `iphone/benchmark.py` | Optional read-only throughput test: ≤128 MB from one existing large video, 20 s cap, data discarded. |
| `photos/snapshot.py` | Consistent local copy of `Photos.sqlite` + WAL (+SHM) in `temp\snapshot_<uuid>\`. |
| `photos/schema.py` | Table detection (`ZASSET`, fallback `ZGENERICASSET`) and dynamic join-table discovery; diagnostics. |
| `photos/album_parser.py` | Albums (`ZKIND=2`), folders (`4000`) and hierarchy via `ZPARENTFOLDER`; member assets. |
| `photos/index_db.py` | Our compact `album_index.sqlite`. |
| `photos/refresh.py` | Cache reuse by fingerprint, otherwise snapshot → parse → index → delete snapshot. |
| `export/planner.py` | Selected albums → files. Lists each needed DCIM directory once; finds Live Photo `.MOV` companions from that cached listing; they are planned into the album's `live_videos/` subfolder (`PlannedFile.subdir`). For assets the Photos DB marks as edited (`ZADJUSTMENTSSTATE` ≠ 0, or `ZHASADJUSTMENTS` on older iOS) it lists `PhotoData/Mutations/<DCIM dir>/<stem>/Adjustments/` and uses iOS's own `FullSizeRender.*` (and `FullSizeRender.mov` for Live motion) instead of the original; falls back to the original and reports it if no render exists. Stats only selected files. |
| `export/exporter.py` | Per-file transfer, naming, skip/collision, resume, error isolation, disconnect handling. |
| `export/transfer.py` | Streaming `.part` copy in ≤4 MiB chunks with `fsync`. |
| `export/resume.py` | Pure decisions for `.part` and existing-file handling. |
| `export/manifest.py` | `<dest>\.jwAlbums\manifest.sqlite`. |
| `export/path_safety.py` | Windows name sanitizing, root-containment (realpath, so junctions/symlinks resolve), device path validation. |
| `app/worker.py` | Worker thread with its own event loop; one task at a time; posts `app/messages.py` objects. |
| `app/network_guard.py` | Refuses non-loopback `connect`/`sock_connect`/name resolution at runtime. |

## Album index refresh

1. `stat` `PhotoData/Photos.sqlite` and `-wal`. If they equal the fingerprint stored in
   `album_index.sqlite` → `snapshot_info`, the cached index is used and nothing is downloaded.
2. Otherwise, snapshot:
   - Find a `temp\snapshot_*` whose recorded main-DB fingerprint equals the current one. If
     it exists, its `Photos.sqlite.part` is resumed with AFC `fseek` from the last offset
     recorded as fsynced (`db_synced` in `snapshot_state.json`). Every other snapshot
     directory is deleted. The WAL/SHM files in a reused directory are always deleted first.
   - If a read fails because the file shrank or vanished mid-copy (`RemoteFileError`), the
     copy is treated as a library change and retried. It is not a fatal error.
   - Copy the main DB. Then the WAL stage: delete local sidecars, stat the WAL, copy it,
     copy the SHM, and re-stat the WAL. If the WAL changed, redo only the WAL stage (at
     most 3 times).
   - Re-stat the main DB. If it changed, delete the directory and start again in a
     **new** `snapshot_<uuid>`. After 3 attempts, show "Photo library changed while
     reading the album index…".
3. Open the local copy only, read-only (`mode=ro` URI + `PRAGMA query_only = ON`; a
   read-write connection would fold the WAL into the copy on close), and detect the schema.
4. Write `album_index.sqlite` (atomically via `.tmp` + `os.replace`) and `fingerprint.json`,
   then close SQLite and delete the snapshot directory (the multi-GB copy is never kept).

The SHM is copied as the spec requires but is not trusted. SQLite's Windows VFS truncates
and rebuilds the WAL index when it is the first connection to open the database, so a
slightly newer SHM cannot be paired with our WAL.

## Join-table discovery

For every `Z_<n><NAME>` table: sample up to 50 000 rows, and for each integer column
measure the fraction of values that are album PKs and the fraction that are asset PKs.
A (album column, asset column) pair qualifies only when both rates are ≥ 98 %. Pairs are
scored on:

- the rates
- *discrimination*: the asset column must look less like album PKs than the album column
  does. This matters because album PKs usually fall inside the asset PK range.
- name hints (`ALBUM`/`ASSET`), weighted lightly
- validation: membership counts per user album must agree with `ZGENERICALBUM.ZCACHEDCOUNT`
  (within slack), or the albums must be non-empty when no cached count exists.

The best pair must be strong (a hint, ≥ 0.2 discrimination, or ≥ 80 % validation), and
must beat every other table/pair by ≥ 0.5. Otherwise we stop, write `schema_diagnostic.txt`
(structure and statistics only, no titles or filenames) and show the unsupported-format
message.

## Export of one file

**Ownership rules.**

- Each destination name belongs to exactly one (device, album, asset, role) in the
  manifest. Ownership is tracked across all devices, so two iPhones exporting into one
  folder never share a name or a `.part`.
- A name that is not ours is never written, resumed or deleted, and that includes
  `<name>.part`.

Steps:

1. Use the size/mtime from the planning `stat`. Re-stat after any reconnect.
2. Destination: `root \ sanitized(folder) \ … \ sanitized(album) \ name`. `safe_join`
   rejects unsafe components and proves the path, after `realpath`, is still under the root.
   Links, junctions and other non-regular files are rejected at the final path and at the
   `.part` path.
3. If the manifest records a name for this key, that name is ours:
   - If the final file exists and "skip" is on: skip it when we finished it with the same
     size, or when its head and tail bytes match the iPhone original.
   - If "skip" is off and the file is ours and complete: re-copy it and atomically
     replace it.
   - Anything else at our name means the name is no longer ours, so a new name is
     allocated.
4. Allocating a new name: try the original name, then `_2`, `_3`, and so on. Skip names
   owned by another key and names with a `.part` beside them. An existing file of equal
   size is *adopted* as "already exported" only if 1 MiB at its head and tail (or the
   whole file, up to 2 MiB) equals the iPhone original. Otherwise the next name is tried.
   **Nothing is ever overwritten.** Live companions (in `live_videos/`) take the primary's final stem
   (`IMG_1_2.JPG` → `IMG_1_2.MOV`).
5. `.part` resume: only when the manifest recorded the same source path, size and mtime
   for this destination. Resume starts at `min(part size, last fsynced offset)`;
   `stream_copy` fsyncs every 64 MiB and records that offset. Otherwise the file restarts.
   A fresh `.part` is opened with `xb` (exclusive create).
6. Stream ≤4 MiB chunks, `flush` + `fsync`, and verify the size. Then call
   `os.rename(part, final)`, which fails rather than overwrites on Windows.
   (`os.replace` is used only for the "re-copy our own file" case.)
7. Set the mtime: the Photos creation date if plausible, else the device file mtime.
   Mark it `done` in the manifest.

Error classes:

- `RemoteFileError` / unsafe path: the file is recorded as failed and the export continues.
  A `ping` first confirms the connection is still alive.
- `DeviceConnectionLost`: keep the `.part`, show "iPhone disconnected", poll usbmux for
  the same UDID, reconnect (waiting for unlock if needed), re-stat inside the retry loop,
  and retry the same file. A file fails only after 5 consecutive losses with **no**
  progress; any bytes copied reset the count.
- Local disk full: stop the export with a message.
- Cancel: stop after the current chunk. Handles are closed, the `.part` is fsynced and
  stays in the `copying` state, and the GUI becomes usable again.

## Threading

Tk runs only on the main thread. `Worker` owns a daemon thread running
`loop.run_forever()`. UI actions call `asyncio.run_coroutine_threadsafe`. Results come back
as dataclass messages on a `queue.Queue`, which the UI polls every 100 ms with
`root.after`. The blocking Windows USB query runs in `asyncio.to_thread`.

## Read-only enforcement

- Structural: only `readonly_afc.py` imports `AfcService`. The adapter exposes no mutation
  methods, and every device path passes `validate_device_path`.
- Tests (`tests/test_readonly_contract.py`):
  - AST scan for `AfcService` use outside the adapter.
  - No prohibited method calls anywhere in `iphone/`.
  - No prohibited calls on any device-like receiver anywhere in the app.
  - The adapter forwards only the allow-listed AFC calls, and `fopen` uses the `"r"`
    constant.
  - The public API surface is checked.
  - A runtime test drives the adapter against a recording fake `AfcService` that fails on
    any mutation.

See `SECURITY_AUDIT.md`.

## Clarifications / deviations

- **Device model.** The target phone reports `iPhone17,1` (iPhone 16 Pro), not a 15 Pro.
  The "maximum link speed" status comes from a model table: 15/16/17 Pro = 10 Gb/s, other
  models = 480 Mb/s. If Windows reports the device as SuperSpeed-capable, that raises the
  maximum.
- **Hidden and trashed items** are left out of album counts and exports, matching what the
  Photos app shows inside albums. They are still recorded in the index.
- **Pairing.** Lockdown uses `autopair=False`. The app reuses the trust relationship Apple
  Devices already has (usbmuxd's pair record) and **never initiates pairing**, so it never
  causes the iPhone to store a new pairing record. An untrusted phone is reported as
  "Trust this computer (unlock the iPhone, open Apple Devices and tap Trust)". A test
  enforces `autopair=False`. Pair records are never stored in the app folder or logged
  (pymobiledevice3 loggers are capped at WARNING, plus a redaction filter).
- **Skip verified existing files** means: skip when we exported the file before (manifest)
  or when size plus head/tail bytes match the original. When unchecked, files we exported
  earlier are re-copied in place. Files we did not create are never touched.
- **Settings** (`settings.json`) are global, under `%LOCALAPPDATA%\jwAlbums\`.
  The album index, `fingerprint.json`, `temp\` and `schema_diagnostic.txt` are per device.
- **Total bytes** for export progress come from a `stat` of each selected file during
  planning. This is proportional to the selection only.
- **Concurrency.** Version 1 uses a single AFC stream, as the spec allows.

## Export details

- Edited assets: the iPhone's own full-size render is copied byte for byte under the
  original's stem with the render's extension (`IMG_5085.HEIC` edited → `IMG_5085.JPG`).
  The render path is under `PhotoData/`, validated like every other device path.
- Dates: after the final rename, `fsutil.set_file_times` sets modified/accessed (os.utime) and
  created (SetFileTime, opened with FILE_FLAG_OPEN_REPARSE_POINT) to the capture time
  (`ZDATECREATED`), falling back to the device file's mtime.
- Source changed since export (photo edited or re-edited on the iPhone): if our manifest entry
  is DONE and the local file still has the size we wrote, the copy is replaced in place via a
  `.part` and `os.replace`. A file the user changed (different size) is left alone and the new
  version gets a numbered name.
- Live clips exported before `live_videos/` existed: if the manifest says we finished the clip in the
  album folder and it still has the size we wrote, it is renamed into `live_videos/` (same volume, so
  dates are kept; `os.rename` fails rather than overwrite). Otherwise it is left alone and a fresh copy
  goes into `live_videos/`.
