# Fleet Remote

Control many Amazon Fire TV devices (Cube, Stick) and Apple TVs from one window, by IP
address, on Windows or Mac. No ADB install needed.

Current version: **v4.04** (the number is in the file name: `fleet_remote_v4.04.py`).

## What it does

| Part of the window | What you can do |
|---|---|
| **Devices** | Add devices by IP (**+ Add device**), sort them into groups (**+ Add group**), fold a group away by clicking it, **Connect all**. Click a device to control it. Double-click its name or IP to change its name, IP address, group, SDI or HDMI input. |
| **Videohub** (SDI router) | If your devices feed a Blackmagic Videohub whose one output goes to a QC monitor: the **QC monitor** tile under the device list shows the router, the QC output and what the monitor is showing; click it (or **Router settings** in the bar at the top of the **Video input** view; **SDI** or **HDMI** brings the picture back) to open the settings in place of the video box: the router's IP address and a grid of its outputs, click the one the QC monitor is on, then **Save**. Give each device its **SDI input** (double-click it): from then on, clicking that device to control it also puts its input on the QC monitor. The tile also shows what the monitor is showing right now (also when someone routes it on the router itself). Talks the Videohub Ethernet Protocol (TCP 9990); nothing to install on the router. |
| **HDMI router** (AVPro matrix) | The same for an AVPro Edge HDMI matrix (AC-MX-88 and family) your devices' HDMI outputs feed: its section in **Router settings** takes the matrix's IP address and the output the QC monitor (or capture box) is on; give each device its **HDMI input** (double-click it). The **SDI** / **HDMI** bar in the **Video input** view picks which router is in use: clicking a device routes it on that one only, and the QC monitor tile shows that router. Only the chosen QC output is ever changed, on either router. Talks the AVPro telnet API (TCP 23). |
| **Remote** | D-pad, OK, Back, Home, Menu, rewind / play-pause / fast forward, volume. The **gear** opens the TV's Settings page, **restart** (the circular arrow; click it twice) reboots a Fire TV, which is back in about a minute, and the **power** button puts a Fire TV in standby or wakes it. Your keyboard works too: arrows, Enter = OK, Backspace = Back, Space = Play/Pause. |
| **Device screen** (Fire TV) | What the Fire TV Stick or Cube itself is showing: **Screenshot**, **Auto** (a new screenshot every 3 s) or **Live video** (with Low / Normal / High quality). Protected video (Netflix etc.) shows as black: Fire TV keeps it out of captures. An Apple TV can't share its screen. |
| **Video input** | The second view in the same place: click **Video input** at the top of the card, **Device screen** to come back. Reserved for a video input device (a capture box taking, for example, the SDI feed the QC monitor shows); empty until one is added. The bar at the top: **SDI** / **HDMI** (which router the picture comes through) / **Router settings** (both routers' settings open here, in place of the picture). |
| **Keyboard** | **Type + Send** a line of text, or **Live typing**: your keys go straight to the TV (Esc stops it). Backspace, Clear field, Enter. |
| **HDMI Signal & Sleep** (Fire TV) | **Never sleep**: Enabled keeps it awake (no screen saver, no sleep), Disabled puts its own timers back. What it sends over HDMI right now: video mode, colour, HDR, audio format and the audio channels present. **Download EDID** saves the EDID of the TV (or EDID emulator) it is plugged into to your Downloads folder, as a `.bin` (raw bytes, for an EDID tool or emulator) and a `.txt` (decoded, with a hex dump). On a Fire TV Stick the audio format comes from Android's audio service, the EDID is the copy Fire OS saved when it last detected the TV, and the colour and HDR lines show what Fire OS set up for the HDMI link ("up to 10-bit", the TV's HDR formats) rather than the live signal (the Stick's HDMI driver can't be read). |

Apple TV: remote keys, Settings and power (no restart from here: on the TV, Settings >
System > Restart). Screen, Never sleep, HDMI info and EDID are Fire TV only (Apple TV
doesn't allow them).

## Before you start

- **Fire TV:** turn on **ADB debugging**: Settings > My Fire TV > **Developer options** >
  ADB debugging > ON. No Developer options in the menu? Open **About**, highlight the first
  line (the device name) and press **Select 7 times** until it says you are a developer; then
  go back one step and Developer options is there. The first time you connect, the TV asks
  "Allow USB debugging?": tick **Always allow** and choose OK.
- **Apple TV:** the first time you connect, the TV shows a 4-digit code; type it into Fleet Remote.
- The computer must be on the same network as the devices (or reach them over VPN).

## Install and start

You need **Python 3.11 to 3.15** from https://www.python.org/downloads/ (the packages Fleet
Remote pins have ready-made builds for these; a newer Python may need a newer Fleet Remote)

1. Install Python. **Windows:** tick **"Add python.exe to PATH"** in the installer.
2. Download this repository: green **Code** button > **Download ZIP**, then unzip it
   somewhere you will keep it. Windows: for example your Documents folder. Mac: your home
   folder (for example `/Users/<you>/Fleet Remote`); in Documents, Desktop or Downloads the
   Mac keeps asking for permission.
3. Start it:
   - **Windows:** double-click **`Fleet Remote.pyw`**. The first time it also makes a
     **`Fleet Remote`** shortcut with the app's icon in the same folder. From then on, start
     it from that shortcut (you can pin it to the taskbar or Start, or copy it to the Desktop).
   - **Mac:** the first time only, right-click **`Fleet Remote.pyw`** > **Open With** >
     **Python Launcher** (in Applications > Python 3.x). A Terminal window flashes up and
     closes by itself. This makes **`Fleet Remote.app`** in the same folder, with the app's
     icon (the `.pyw` file itself always shows Python's). From then on double-click the app
     (you can drag it to the Dock); the `.pyw` way keeps working too.
     Keep `Fleet Remote.app` inside the Fleet Remote folder: it starts the files next to it.

The very first start installs what Fleet Remote needs, quietly in the background, so the
window takes about a minute to appear (a Mac shows a notification that it has begun). After
that it opens straight away.
Everything it installs goes into a `venv` folder next to the app (its own private copy of
Python's packages), so it never touches the Python packages of other programs and other
programs never change its. The exact versions are listed in `requirements.txt` and
`constraints.txt` (every package, including the ones those pull in); if one is missing or
different, the launcher puts the right one back. If the `venv` folder is broken (for example
after Python was uninstalled), the launcher rebuilds it.

## Updating

Close Fleet Remote first. Then download the new ZIP and copy its files into your Fleet
Remote folder (replace them when asked). The launcher always starts the newest
`fleet_remote_v*.py`, so you can delete the old one or leave it. Your devices and settings
are kept: they are not in the app folder (see below). If a new version needs different
packages, the launcher installs them at that next start (no window for up to a minute); it
can't do that while an older copy is still open, so it then asks you to close it and start
again.

## Options

The launcher passes these on, and so does running the app file directly:

| Option | What it does |
|---|---|
| `--browser` | Opens Fleet Remote in your web browser instead of its own window (use this if the window doesn't open) |
| `--port 9000` | Uses a specific port (default 8756; a free one is picked if that's taken) |
| `--share` | Also lets other computers on the network open it in a browser. They need the address and access code shown in Fleet Remote's status line (also printed in a console, and saved in `share_code.txt`) |

How to use them:
- **Windows:** open a Command Prompt in the folder and run `pyw "Fleet Remote.pyw" --share`,
  or add the option at the end of the shortcut's **Target** (right-click > Properties).
- **Mac:** open Terminal in the folder and run `python3 "Fleet Remote.pyw" --share`.
- Or run the app file itself: `python fleet_remote_v4.04.py --browser` (Windows) /
  `python3 fleet_remote_v4.04.py --browser` (Mac).

## Your devices and settings

They are kept in a hidden folder called **`.firecube-fleet`** in your home folder:
Windows `C:\Users\<you>\.firecube-fleet`, Mac `/Users/<you>/.firecube-fleet`
(in Finder press Shift + Command + . to show hidden folders).

| File | What it is |
|---|---|
| `devices.json` | Your devices: name, IP, group, Apple TV pairing keys, and the sleep timers a Fire TV had before Never sleep |
| `groups.json` | Your groups |
| `videohub.json` | The Videohub's IP address and the QC monitor's output (only when you set one up) |
| `hdmirouter.json` | The HDMI matrix's IP address and the QC monitor's output (only when you set one up) |
| `adbkey`, `adbkey.pub` | The key your Fire TVs approved ("Always allow") |
| `share_code.txt` | The `--share` access code |
| `error.log`, `setup.log` | Details when something fails at start or during the first-start install (`error.log` also gets warnings from inside the app; it is trimmed when it passes 500 KB) |
| `running.json` | The address the running copy is on, so a second start can find it (removed when it closes) |
| `scrcpy-server-v5.0.1`, `webview` | Live video helper (downloaded from GitHub the first time you use Live video) and the window's own storage |

**Moving to another computer:** copy `devices.json`, `groups.json`, `videohub.json`, `adbkey`
and `adbkey.pub` into the same folder there. The Fire TVs then know the new computer
already; Apple TVs may need pairing again.

**Keep this folder to yourself.** The ADB key (`adbkey`) and the Apple TV pairing keys in
`devices.json` are stored as plain files, not encrypted: anyone who copies them can
control your devices the way Fleet Remote does. On a Mac the folder is made readable by
your user only; on Windows it is inside your own user profile. Don't share it, back it up
only to places only you can open, and if a device must stop trusting an old computer,
turn ADB debugging off and on again on that Fire TV (Apple TV: pair it again).

## Files in this folder

| File | What it is |
|---|---|
| `fleet_remote_v4.04.py` | The app |
| `Fleet Remote.pyw` | The launcher: installs what's needed the first time, makes the shortcut / Mac app, starts the newest app file |
| `requirements.txt` | The Python packages it installs |
| `constraints.txt` | The exact version of every package, including the ones they pull in, for Windows and Mac |
| `icon.ico` | The app's icon |
| `Fleet Remote.lnk` / `Fleet Remote.app` | Made by the launcher on your computer (not part of the download) |
| `venv/` | Made by the launcher on your computer: the packages Fleet Remote installed for itself (not part of the download) |
| `.gitignore` | Keeps the computer-specific files above (and `venv`) out of the GitHub repository |

## Troubleshooting

- **Started it twice:** only one Fleet Remote runs at a time; starting it again brings the
  open window to the front (it finds the running copy through `running.json`, even when that
  copy is on a different port). If it says *"Fleet Remote is already running (or still
  closing) but doesn't answer"*, the earlier copy is still shutting down or has frozen: wait
  a few seconds and open it again; if that keeps happening, end `python` / `pythonw` in
  Task Manager (Mac: Activity Monitor) and start it once more.
- **"Fleet Remote v3.85 is still running. Close it, then start Fleet Remote again":** you
  started a newer version while the old one was open. Close the old one (its window, or
  Quit) and start again; the launcher then finishes installing and opens the new version.
- **"Fleet Remote is already running, but its own window is not open":** the running copy
  could not open its window (the reason is in `.firecube-fleet/error.log`, usually the
  WebView2 runtime on Windows) and was opened in your browser instead. *"Fleet Remote is
  still closing"* means the previous copy is on its way out: wait a moment and open it again.
- **The shortcut stopped working after a Python update:** double-click `Fleet Remote.pyw`
  once; it rebuilds the shortcut for the new Python (re-pin it to the taskbar if you pinned it).
- **It opened in the browser instead of its own window (Windows):** the window needs the
  Microsoft Edge WebView2 runtime, which Windows 10 and 11 normally have. Install it from
  https://developer.microsoft.com/microsoft-edge/webview2/ if it's missing.
- **No window appears:** start it with `--browser` (see Options).
- **Mac: quitting:** Cmd+Q, the red close button and Dock > Quit all stop Fleet Remote
  properly (live video on the TVs is ended first).
- **"Fleet Remote could not install what it needs":** check the internet connection; the
  details are in `.firecube-fleet/setup.log`.
- **A Fire TV won't connect:** check ADB debugging is on (see Before you start), the IP is
  right and reachable (ping it), and accept the prompt on the TV. If you once chose Deny,
  turn ADB debugging off and on again on the TV.
- **Accents or emoji don't arrive:** Fire TV can't type them with Type + Send; turn on Live
  video to type accented letters (emoji don't work either way).
- **Live video doesn't start:** it needs internet the first time (it downloads a small
  helper from GitHub). On a Mac it needs macOS 13.3 or newer. If it says the encoder is
  stuck, restart the Fire TV.
- **Mac: devices can't be reached:** allow **Python** under System Settings > Privacy &
  Security > **Local Network**. With `--share`, also choose **Allow** when the Mac asks
  about incoming network connections.
- **Mac: "Fleet Remote.app can't be opened":** right-click it > Open the first time.
- **Hover ticks:** the short click when the pointer moves over a button is switched with the
  **Ticks on / off** button in the header (remembered per computer). Browsers only allow sound
  after a first click or key press in the page, so the ticks begin after that.
- **Mac: "Python would like to access files in your Downloads folder":** that's Download
  EDID saving its files. Choose Allow (or later: System Settings > Privacy & Security > Files and Folders).
- **Apple TV shows no power state, or standby/on doesn't update:** tvOS 26 needs pyatv
  0.18 or newer. The launcher (`Fleet Remote.pyw`) installs the version in `requirements.txt`
  into the app's `venv` folder by itself (delete `venv` and start the launcher again to
  reinstall everything from scratch).
- **Mac: live video shows "decoder error" but screenshots work:** v3.58 reshapes the video
  frames for the Mac's decoder and tries a second form if the first one fails (seen failing
  once with v3.56). If it still happens, send the lines starting with `page:` from
  `.firecube-fleet/error.log` (they name the form tried, the codec and the picture size).
- **Windows: other computers can't open it with `--share`:** allow Python in the Windows
  firewall prompt. On a network set to "Public", Windows blocks them anyway: set it to
  "Private" (Settings > Network & internet) or ask IT.
- **`--share` safety:** the access code goes over the network unencrypted. Fine on a studio
  or lab network; don't use `--share` on guest or public Wi-Fi. On VPN the address shown
  may be the VPN one; use the computer's office network IP instead.

Developed and tested on Windows; tested on a Mac (macOS 15, Apple silicon) with a Fire TV
Cube. Apple TV power is untested.
