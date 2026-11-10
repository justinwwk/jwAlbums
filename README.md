# jwAlbums

jwAlbums copies the photos and videos from your iPhone **Photos albums** into ordinary
Windows folders. It makes one folder per album and keeps your Photos folder structure.

```
iPhone Photos                 Windows
  Family            ->        D:\iPhone Photos\Family\
  Travel / Japan    ->        D:\iPhone Photos\Travel\Japan\
  Baby              ->        D:\iPhone Photos\Baby\
```

It only reads from the iPhone and never writes, deletes or changes anything on it. It
doesn't use iCloud, doesn't make a backup, and never connects to the internet.

## What you need

- Windows 10 or 11 (64-bit)
- **Apple Devices** from the Microsoft Store (or iTunes). It provides the USB driver
  Windows needs to talk to an iPhone.
- A USB cable that carries data (some cables only charge). Any USB port or hub works.
  USB 2 is slower; an iPhone 15 Pro or later copies fastest with a USB 3 (10 Gb/s) cable
  in a USB 3 port.

## Using it

1. Plug the iPhone in and unlock it. If you have never used it with this PC, open
   **Apple Devices** once and tap **Trust** on the iPhone. jwAlbums finds the iPhone by
   itself. **Rescan** checks again right away.
2. Open `jwAlbums.exe`. The window shows the iPhone, its iOS version and the USB
   connection speed.
3. The album list is read from the iPhone. The first time takes a minute or two and shows
   a progress window with a **Cancel** button. After that, the saved list loads instantly
   unless something changed on the iPhone. If you cancel, the next **Refresh Albums**
   continues where it stopped.
4. Tick the albums you want, or use **Select All** / **Select None**. Ticking a folder
   ticks every album in it. **Hide empty albums** (on by default) hides albums with no
   items, such as albums whose photos are all in Recently Deleted.
5. Choose a destination folder and click **Export Selected Albums**. A progress window with
   a **Stop** button shows how far it has got.

If an export is stopped, or the cable comes out, run it again: files already copied are
skipped and a half-copied file continues where it stopped. If the cable comes out during
an export, jwAlbums waits for the same iPhone to come back and then carries on.

### What you get

- **Every file is copied exactly as the iPhone stores it.** Nothing is converted or
  recompressed: HEIC (camera set to *High Efficiency*), JPG (*Most Compatible*), PNG
  (screenshots), MOV (videos). The information inside each file, such as capture time,
  location and camera, is kept. Windows 11 opens HEIC with Microsoft's free
  *HEIF Image Extensions*.
- **Edited photos and videos look the way they do on the iPhone.** iOS keeps both your
  original and a full-size copy of your edit (crop, filter, trim and so on). For an edited
  item jwAlbums copies the edit, under the normal name (`IMG_5085.JPG`), not the original.
  If the iPhone has no edited copy of an item marked as edited, the original is saved and
  the summary says so. If you edit a photo again after exporting it, the next export
  updates the exported copy, unless you have changed that copy yourself.
- **File dates.** Each file's *Date modified* and *Date created* in Windows are set to when
  the photo or video was taken.
- **Nothing is overwritten.** If a different file already has the same name, the new one is
  saved as `IMG_1234_2.JPG`. A photo that is in several of the selected albums is copied
  into each album's folder.

### Options

| Option | What it does |
|---|---|
| Include videos | Exports the videos in the albums as well as the photos. |
| Include Live Photo motion | A Live Photo is a still photo (`IMG_1234.HEIC`) plus a short video clip (`IMG_1234.MOV`). When this is on, the clips go in a `live_videos` subfolder of each album (`Trip/live_videos/IMG_1234.MOV`), so the album folder shows only photos and real videos. When it is off, only the still photo is saved. Clips that an earlier version saved next to the photos are moved into `live_videos` on the next export, unless you have changed them. |
| Skip files already exported | Skips files that are already in the destination, so running an export again copies only what is new. |

### USB speed

The **USB** box shows the speed Windows connected the iPhone at: USB 2 (480 Mb/s), USB 3
(5 Gb/s) or USB 3 (10 Gb/s). Every speed works; a faster one just copies sooner. If the
box shows USB 2 for an iPhone that supports more, a different cable or port may help.
**Run Speed Test** reads about 128 MB from one video on the iPhone and reports how fast
copying actually goes.

## Where things are stored

- `%LOCALAPPDATA%\jwAlbums\` holds the settings, the logs and the saved album list for
  each iPhone. iPhone pairing keys are never stored there.
- While the album list is being read, a copy of the iPhone's photo database
  (`Photos.sqlite`, which can be a few GB) is saved in
  `%LOCALAPPDATA%\jwAlbums\<device>\temp\`. It is deleted as soon as the album list has
  been read. If you cancel, it stays there until the next **Refresh Albums** finishes the
  download.
- `<destination>\.jwAlbums\manifest.sqlite` is a hidden list of what has been exported,
  used to skip files and resume. It holds no photos.

Folders from before the rename (`iPhoneAlbumExporter`, `.iPhoneAlbumExporter`) are renamed
automatically the first time jwAlbums uses them.

## Troubleshooting

| Message | What to do |
|---|---|
| Apple Devices is not installed/running | Install Apple Devices from the Microsoft Store and open it once. |
| No iPhone connected | Check the cable (it must carry data). jwAlbums connects as soon as Windows sees the iPhone. |
| Unlock iPhone | Unlock the iPhone. |
| Trust this computer | Unlock the iPhone, open Apple Devices and tap **Trust** on the iPhone. |
| Photo library changed while reading the album index | Close the Photos app on the iPhone, wait a moment, then click **Refresh Albums**. |
| Album database format is different from the supported version | A newer iOS changed the Photos database. Details are saved in `%LOCALAPPDATA%\jwAlbums\<device>\schema_diagnostic.txt`. |

Log file: `%LOCALAPPDATA%\jwAlbums\logs\app.log`

## Development

```
jwAlbums/
├── src/                     application source (main.py + app, export, iphone, photos, ui)
├── tests/                   pytest suite
├── docs/                    architecture.md, SECURITY_AUDIT.md
├── packaging/               PyInstaller spec, README.txt for users
├── build.bat  run.bat       build the release / run from source
├── .github/workflows/       GitHub builds and drafts a release for each version tag
├── requirements*.txt        pinned, hash-locked dependencies
├── LICENSE                  GPL-3.0
│
│   generated, not in git:
├── .venv/                   Python environment (created by run.bat / build.bat)
├── build/                   PyInstaller work files and the developer CLI EXE (build/dist/)
└── release/                 what users get
    ├── jwAlbums/            jwAlbums.exe, README.txt, LICENSE.txt, THIRD-PARTY-LICENSES.txt,
    │                        jwAlbums-source.zip
    └── jwAlbums-1.0.0.zip   the same folder, zipped for download (named with the version)
```

The release folder needs nothing else: the EXE includes Python and every library and reads
nothing from this project. On the user's PC it only needs Apple Devices (or iTunes).

```
run.bat                  # GUI (creates .venv from the hash-pinned lock file on first run)
run.bat --smoke          # read-only real-device smoke test
run.bat --acceptance     # real-device acceptance test (copies one image to .\test_export\)
run.bat --list-albums    # print albums and item counts
run.bat --export-album "Travel/Japan" --dest "D:\iPhone Photos"
build.bat                # run the tests, build the EXEs, create release\ and release\jwAlbums-<version>.zip
.venv\Scripts\python -m pytest
```

`build\dist\jwAlbumsCLI.exe` is a developer tool that is not shipped to users. It takes the
same command-line options as `run.bat`. In VS Code, F5 runs the release EXE from its own
folder; "Build + Run EXE" builds first.

### Versions and releases

The version is set in one place, [src/app/version.py](src/app/version.py), as
MAJOR.MINOR.PATCH. It shows in the window title, in `jwAlbumsCLI.exe --version`, at the top
of each log session, in the EXE's *Properties > Details*, and in the download zip's name.

The EXE and zips are not kept in git. GitHub builds them
([.github/workflows/release.yml](.github/workflows/release.yml)). To publish a version:

1. Change `__version__` in `src/app/version.py` (for example `1.0.0` to `1.1.0`).
2. Run `build.bat` to check the tests pass and the EXE works.
3. Commit, tag and push: `git tag v1.1.0`, then `git push origin main v1.1.0`.
4. GitHub runs the tests, builds the EXE on Windows and creates a **draft** release with
   `jwAlbums-1.1.0.zip` and its `.sha256` checksum (Actions tab shows progress, about
   5 minutes). Open **Releases** on GitHub, check the draft, and click **Publish release**.

The build stops if the tag and `version.py` disagree. A build without a release (to try it)
is under **Actions > Release > Run workflow**; the zip is attached to that run.

## Credits

Designed, written and tested by [justinwwk](https://github.com/justinwwk), with AI coding assistance from Claude
(Anthropic) and Codex (OpenAI).

Contact: https://github.com/justinwwk

## License

Copyright (c) 2026 justinwwk

jwAlbums is free software under the GNU General Public License v3.0 or later (see
[LICENSE](LICENSE)). It has to be: it is built on pymobiledevice3, which is GPL-3.0.
Anyone may use, share and change it, but anyone who passes on a copy, changed or not,
must pass on its source under the same license. `build.bat` puts the source in
`jwAlbums-source.zip` next to the EXE for that reason.

The EXE includes Python and the libraries it uses from `requirements-lock.txt`, each under
its own license. `build.bat` collects the license texts of exactly what went into the EXE
into `THIRD-PARTY-LICENSES.txt` in the release folder.

Apple, iPhone and iCloud are trademarks of Apple Inc. jwAlbums is an independent project and
is not affiliated with, endorsed by or sponsored by Apple.
