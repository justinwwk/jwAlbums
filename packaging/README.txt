jwAlbums
========

jwAlbums copies the photos and videos from the iPhone Photos albums you choose into
ordinary Windows folders, one folder per album, over a USB cable. Nothing on the iPhone is
changed.


WHAT YOU NEED
-------------
- Windows 10 or 11 (64-bit)
- Apple Devices from the Microsoft Store (or iTunes). It provides the USB driver Windows
  needs to talk to an iPhone. Open it once with the iPhone connected and tap "Trust" on the
  iPhone.
- A USB cable that carries data (some cables only charge). Any USB port or hub works;
  USB 3 is faster than USB 2.
- The full-size photos on the iPhone. jwAlbums copies only what is stored on the iPhone.
  If you use iCloud Photos with "Optimize iPhone Storage", many full-size originals are
  only in iCloud. Before exporting, choose "Download and Keep Originals" in
  Settings > Apps > Photos (Settings > Photos on iOS 17 and earlier) and wait for the
  download to finish. The iPhone needs enough free space for them.

This folder is the whole app and there is nothing to install. Unzip it anywhere and run
jwAlbums.exe.

The first time you run it, Windows may say "Windows protected your PC" because the app is
not code-signed. Click "More info", then "Run anyway".


HOW TO USE
----------
1. Connect and unlock the iPhone. jwAlbums finds it by itself.
2. The album list is read from the iPhone. This takes a minute or two the first time;
   after that it is only read again when something has changed on the iPhone.
3. Tick the albums you want (or click "Select All"), choose a destination folder, and
   click "Export Selected Albums".

You get:
- Every photo and video exactly as the iPhone stores it (HEIC, JPG, PNG, MOV). Windows 11
  opens HEIC with Microsoft's free "HEIF Image Extensions".
- Edited photos and videos the way they look on the iPhone.
- Live Photo clips in a "live_videos" subfolder of each album (if that option is ticked).
- Each file's Windows "Date modified" and "Date created" set to when it was taken.
- Nothing overwritten. Running an export again copies only what is new.

The destination gets a hidden ".jwAlbums" folder. It is a list of what has been exported,
used to skip and resume. It holds no photos; leave it there.


PRIVACY
-------
jwAlbums never connects to the internet: no telemetry, no update checks, no uploads.
Its own data (settings, saved album lists, logs) stays on this PC in:

    %LOCALAPPDATA%\jwAlbums

To remove jwAlbums, delete this folder and that one.


CONTACT
-------
Questions and problems: https://github.com/justinwwk

The version number is in the window title, and in jwAlbums.exe's Properties > Details.


LICENSE
-------
Copyright (c) 2026 justinwwk

jwAlbums is free software under the GNU General Public License v3.0 or later; see
LICENSE.txt. You may use, share and change it. If you pass it on, pass on
jwAlbums-source.zip (its source code) with it.

jwAlbums includes other open-source software; their licenses are in
THIRD-PARTY-LICENSES.txt.

Apple and iPhone are trademarks of Apple Inc. jwAlbums is not affiliated with or endorsed
by Apple.
