# Server Assistant

Runs on your media PC and gives you a dashboard on your phone with four tabs:

- **Server**: Plex status and what's playing right now (who, on which device, direct play or transcoding, progress, with a Stop button), what Plex is busy with (scans etc.), CPU, RAM, network, disk activity, drive space, GPU, Plex's own CPU/RAM use, library counts with Scan buttons, recently added, recently watched, remote-access status and Plex updates.
- **Music**: new releases from your artists.
- **Downloads**: paste magnet links / .torrent links or upload .torrent files; they're sent to qBittorrent.
- **Subtitles**: what was fetched for your movies.

What it does:

0. **Downloads what you give it**: paste a magnet link or .torrent link (or upload a .torrent file), on a release card or in the Downloads tab. qBittorrent downloads it. **As soon as it finishes, the torrent is removed from qBittorrent (files kept) so it doesn't seed**, and music is moved into your library. Choose "Other" for non-music downloads: those stop when done and stay in qBittorrent's download folder.
1. **Lists new releases** (albums, singles, EPs) from every artist in your music library, using MusicBrainz. Tap **Get it** or **Skip**.
2. **Sorts your downloads**: anything you save to your music download folder is moved into `Music/<Artist>/<Year> - <Album>/` automatically, using the files' tags (or the folder name, like `Megadeth - Dystopia (2016)`). Existing artist folders are reused even if the spelling or capitals differ. Zip files are unpacked first. Cover art, .cue and .log files are moved along with the music.
3. **Downloads missing subtitles** (English + Persian by default) for your movies and saves them next to the video as `Movie.en.srt` / `Movie.fa.srt`, which Plex picks up on its own. It looks in several places, in the order you choose under **Settings → Subtitle sources**:
   - built-in websites: subf2m (Persian), YIFY Subtitles, Podnapisi, SubSource
   - built-in APIs: OpenSubtitles.com, SubDL (need a free key)
   - **your own websites and APIs**: "+ Add website" / "+ Add API", with an address like `https://site.com/search?q={title}+{year}`. Placeholders: `{title}` `{year}` `{imdb}` `{lang}` `{lang_name}` `{title_slug}`.
   Every source has a **Test** button that searches a movie and shows what it found. Sites protected by Cloudflare usually block automatic access, and the test tells you when that happens. Movies are matched using the IMDb id from Plex.

## Setup (Windows)

1. Install **Python 3.11 or newer** from python.org (tick "Add Python to PATH").
2. Unzip this folder on the server PC, e.g. `C:\ServerAssistant`.
3. Double-click **start.bat**. The first time it installs what it needs (a minute or two).
4. It then runs in the background. Look for the **Server Assistant icon in the tray** by the clock (it may be under the ^ arrow).
   - Double-click the icon to open the dashboard.
   - Right-click it for **Start / Stop / Restart**, **Start with Windows**, **Open log** and **Quit**.
5. Open **Settings** in the dashboard. Music and movie folders are taken from Plex automatically. Add your qBittorrent and OpenSubtitles details there.
6. On your phone (same Wi-Fi), open the address shown under Settings → Get the app, or install the Android app.

If Windows Firewall asks, allow Python on **private networks** so the phone can connect.
The dashboard also has **Restart** and **Stop** under Settings → Server program. After Stop, start it again from the tray icon or start.bat.
`start-with-window.bat` runs it in a visible window instead, which is handy for seeing errors.

## Apps

**Android phone:** on the phone (home Wi-Fi), open `http://<server address>:8765/app.apk` in the browser, or copy `ServerAssistant.apk` to the phone. Open it and allow "Install unknown apps" for your browser/file manager when Android asks. On first start the app searches your Wi-Fi for the server by itself (or type the address). Extras:
- Tap a **magnet link** anywhere on the phone (or open a downloaded **.torrent** file, or **Share** one to the app). Choose *Music* or *Other* and it's sent straight to the server.
- The ⚙ button in the app changes which server it connects to.

**This PC / any Windows PC on the network:** open the dashboard in a browser, go to **Settings → Install on this PC**, and run the downloaded file. (Or use `desktop-app\Install desktop app.bat` from this folder.) It finds the server (or asks for its address) and adds **Server Assistant** to your desktop and Start menu. It opens in its own window with a wider layout.

## Using it

- **New**: fresh releases from your artists. **Get it** moves a release to *To get*, and **Skip** hides it.
- **To get**: your list, with links to Bandcamp and Qobuz. When the album lands in your download folder and is sorted, it's ticked off automatically.
- **Sorted**: everything that was moved, and anything it couldn't work out (left in place for you to check).
- **Subtitles**: what was downloaded or not found. Films with no match are retried after a week.
- **Check now** runs a check right away. Otherwise new releases are checked every 12 hours and subtitles every 6 hours.

The first release check takes about 2–3 seconds per artist, because MusicBrainz limits how fast it can be queried. After that the artist IDs are remembered.

## Notes

- No seeding: finished torrents are removed from qBittorrent straight away. While a download is still running, BitTorrent always shares small pieces with others. `upload_limit_kb = 1` keeps that close to zero.
- Stopping someone's stream needs Plex Pass.

- Your library is expected to be laid out as one folder per artist. Albums already in an artist's folder are recognized and won't show up as new.
- Artists that MusicBrainz can't match confidently are listed at the top of the dashboard.
- To start it with Windows, right-click the tray icon and tick **Start with Windows**.
- Keep it on your home network. Don't port-forward 8765 to the internet: the dashboard has no login.

## Folders

By default the music and movie folders are taken **from Plex**: whatever folders your Plex music and movie libraries use. You can switch to your own folders under **Settings** (the Browse button lists the server's drives).


## Finding the server without an IP address
- Same network: `http://serverassistant.local:8765` or `http://<computer-name>:8765` (the start window prints both).
- The desktop shortcut made by Settings → Install on this PC opens a small page that finds the server by itself,
  even if the router gave it a new IP. `Open Server Assistant.html` (download link in Settings) does the same without installing.
- For a permanent IP, set a DHCP reservation for the server PC in your router.

## Music sources
New releases are looked up on MusicBrainz, Deezer and iTunes (all free, no login). Pick which ones in Settings → Music downloads.
"Skip artist" on a release stops checking that artist until you remove them from Music → Skipped.

## Downloads
The app has its own downloader (libtorrent), so qBittorrent is optional. Paste a magnet link or choose a .torrent file on the Downloads tab.
Music is saved in the app's own folder and sorted into the artist's folder when it finishes; nothing keeps sharing afterwards.
You can switch to qBittorrent in Settings → Downloads.

## Installing and updating
- `ServerAssistant-Setup.exe` installs for your Windows account (Start menu, desktop icon, Apps and features). Run a newer Setup to update; settings and history are kept in `%APPDATA%\ServerAssistant`.
- Without Setup, unzip the folder and run `start.bat`. Settings → Update can apply a newer .zip from inside the app.
