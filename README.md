# Fleet Remote

Control many Amazon Fire TV devices (Cube, Stick) and Apple TVs from one window, by IP
address, on Windows or Mac. No ADB install needed.

Current version: **v3.58** (the number is in the file name: `fleet_remote_v3.58.py`).

## What it does

| Part of the window | What you can do |
|---|---|
| **Devices** | Add devices by IP (**+ Add device**), sort them into groups (**+ Add group**), fold a group away by clicking it, **Connect all**. Click a device to control it. Double-click its name or IP to change its name, IP address or group. |
| **Remote** | D-pad, OK, Back, Home, Menu, rewind / play-pause / fast forward, volume. The **gear** opens the TV's Settings page and the **power** button puts a Fire TV in standby or wakes it. Your keyboard works too: arrows, Enter = OK, Backspace = Back, Space = Play/Pause. |
| **Screen** (Fire TV) | **Screenshot**, **Auto** (a new screenshot every 3 s) or **Live video** (with Low / Normal / High quality). Protected video (Netflix etc.) shows as black: Fire TV keeps it out of captures. |
| **Keyboard** | **Type + Send** a line of text, or **Live typing**: your keys go straight to the TV (Esc stops it). Backspace, Clear field, Enter. |
| **HDMI Signal & Sleep** (Fire TV) | **Never sleep**: Enabled keeps it awake (no screen saver, no sleep), Disabled puts its own timers back. What it sends over HDMI right now: video mode, colour, HDR, audio format and the audio channels present. **Download EDID** saves the EDID of the TV (or EDID emulator) it is plugged into to your Downloads folder, as a `.bin` (raw bytes, for an EDID tool or emulator) and a `.txt` (decoded, with a hex dump). |

Apple TV: remote keys, Settings and power. Screen, Never sleep, HDMI info and EDID are
Fire TV only (Apple TV doesn't allow them).

## Before you start

- **Fire TV:** turn on **ADB debugging** (Settings > My Fire TV > Developer Options).
  The first time you connect, the TV asks to allow the computer: tick **Always allow** and choose OK.
- **Apple TV:** the first time you connect, the TV shows a 4-digit code; type it into Fleet Remote.
- The computer must be on the same network as the devices (or reach them over VPN).

## Install and start

You need **Python 3.10 or newer** from https://www.python.org/downloads/

1. Install Python. **Windows:** tick **"Add python.exe to PATH"** in the installer.
2. Download this repository: green **Code** button > **Download ZIP**, then unzip it
   somewhere you will keep it. Windows: for example your Documents folder. Mac: your home
   folder (for example `/Users/<you>/Fleet Remote`); in Documents, Desktop or Downloads the
   Mac asks for permission every time.
3. Start it:
   - **Windows:** double-click **`Fleet Remote.pyw`**. The first time it also makes a
     **`Fleet Remote`** shortcut with the app's icon in the same folder. From then on, start
     it from that shortcut (you can pin it to the taskbar or Start, or copy it to the Desktop).
   - **Mac:** the first time only, right-click **`Fleet Remote.pyw`** > **Open With** >
     **Python Launcher** (in Applications > Python 3.x). This makes **`Fleet Remote.app`** in
     the same folder. From then on double-click that (you can drag it to the Dock).
     Keep `Fleet Remote.app` inside the Fleet Remote folder: it starts the files next to it.

The very first start installs what Fleet Remote needs, quietly in the background, so the
window takes about a minute to appear (a Mac shows a notification that it has begun). After
that it opens straight away.
If Python refuses to install packages (Homebrew Python on a Mac), Fleet Remote makes its
own `venv` folder next to the app and installs there by itself.

## Updating

Download the new ZIP and copy its files into your Fleet Remote folder (replace them when
asked). The launcher always starts the newest `fleet_remote_v*.py`, so you can delete the
old one or leave it. Your devices and settings are kept: they are not in the app folder
(see below).

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
- Or run the app file itself: `python fleet_remote_v3.58.py --browser` (Windows) /
  `python3 fleet_remote_v3.58.py --browser` (Mac).

## Your devices and settings

They are kept in a hidden folder called **`.firecube-fleet`** in your home folder:
Windows `C:\Users\<you>\.firecube-fleet`, Mac `/Users/<you>/.firecube-fleet`
(in Finder press Shift + Command + . to show hidden folders).

| File | What it is |
|---|---|
| `devices.json` | Your devices: name, IP, group, Apple TV pairing keys, and the sleep timers a Fire TV had before Never sleep |
| `groups.json` | Your groups |
| `adbkey`, `adbkey.pub` | The key your Fire TVs approved ("Always allow") |
| `share_code.txt` | The `--share` access code |
| `error.log`, `setup.log` | Details when something fails at start or during the first-start install |
| `scrcpy-server-v5.0.1`, `webview` | Live video helper (downloaded from GitHub the first time you use Live video) and the window's own storage |

**Moving to another computer:** copy `devices.json`, `groups.json`, `adbkey` and
`adbkey.pub` into the same folder there. The Fire TVs then know the new computer
already; Apple TVs may need pairing again.

## Files in this folder

| File | What it is |
|---|---|
| `fleet_remote_v3.58.py` | The app |
| `Fleet Remote.pyw` | The launcher: installs what's needed the first time, makes the shortcut / Mac app, starts the newest app file |
| `requirements.txt` | The Python packages it installs |
| `icon.ico` | The app's icon |
| `Fleet Remote.lnk` / `Fleet Remote.app` | Made by the launcher on your computer (not part of the download) |
| `.gitignore` | Keeps the computer-specific files above (and `venv`) out of the GitHub repository |

## Troubleshooting

- **Started it twice:** only one Fleet Remote runs at a time; starting it again brings the
  open window to the front.
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
- **A Fire TV won't connect:** check ADB debugging is on, the IP is right and reachable
  (ping it), and accept the prompt on the TV. If you once chose Deny, turn ADB debugging
  off and on again on the TV.
- **Accents or emoji don't arrive:** Fire TV can't type them with Type + Send; turn on Live
  video to type accented letters (emoji don't work either way).
- **Live video doesn't start:** it needs internet the first time (it downloads a small
  helper from GitHub). On a Mac it needs macOS 13.3 or newer. If it says the encoder is
  stuck, restart the Fire TV.
- **Mac: devices can't be reached:** allow **Python** under System Settings > Privacy &
  Security > **Local Network**. With `--share`, also choose **Allow** when the Mac asks
  about incoming network connections.
- **Mac: "Fleet Remote.app can't be opened":** right-click it > Open the first time.
- **Mac: "Python would like to access files in your Downloads folder":** that's Download
  EDID saving its files. Choose Allow (or later: System Settings > Privacy & Security > Files and Folders).
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

Developed and tested on Windows. The Mac version is written to work the same, but has not
been tested on a Mac yet. Apple TV power is untested.
