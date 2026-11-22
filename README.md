# Fleet Remote

Control many Amazon Fire TV Cubes and Apple TVs from one window, by IP address.
Send remote keys to one device or a group, launch apps, type text, run shell
commands, see live screenshots, keep Fire TVs from going to sleep
(Never sleep) and see what a Fire TV sends over HDMI (resolution, frame rate,
audio format and channels). No ADB install needed.

Written in Python. Developed and tested on Windows. The Mac steps below should
work but have not been tested yet (live video needs macOS 13.3 or newer).

The number in the file name is the version (fleet_remote_v3.39.py = v3.39).
If your copy has a different number, use that file name in the commands below.

## Before you start

- Fire TV: turn on **ADB debugging** (Settings > My Fire TV > Developer Options).
  The first time you connect, the TV asks you to allow the computer. Choose Always allow.
- Apple TV: you pair once with a 4 digit code shown on the TV.
- The computer must be on the same network as the devices (or reach it over VPN).

## Install on Windows

1. Install Python 3.10 or newer from https://www.python.org/downloads/
   (tick "Add python.exe to PATH" in the installer).
2. Download this repository: green **Code** button > **Download ZIP**, then unzip it.
3. Open the unzipped folder, click the address bar in File Explorer, type `cmd`, press Enter.
4. Run these lines one at a time:

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python fleet_remote_v3.39.py
```

Next time: open `cmd` in the folder and run
`venv\Scripts\activate` then `python fleet_remote_v3.39.py`.

## Install on a Mac

1. Install Python 3.10 or newer from https://www.python.org/downloads/
   (download the macOS installer, open it, click through).
2. Download this repository: green **Code** button > **Download ZIP**.
   It lands in your Downloads folder as `Fleet-Remote-main` (double-click the zip if it did not unzip).
3. Open **Terminal** (press Command + Space, type Terminal, press Enter).
4. Type `cd ` (with a space after it), drag the `Fleet-Remote-main` folder from Finder
   into the Terminal window, and press Enter.
5. Run these lines one at a time:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python fleet_remote_v3.39.py
```

Next time: open Terminal, `cd` to the folder the same way, then run
`source venv/bin/activate` and `python fleet_remote_v3.39.py`.

## Starting it with a double-click

- **Windows:** double-click `Fleet Remote.pyw` (or make a shortcut to it on the Desktop).
  It starts the newest `fleet_remote_v*.py` in the folder, without a console window,
  using the `venv` from the install steps when there is one.
- **Mac:** once, in Terminal in the folder: `chmod +x "Fleet Remote.command"`.
  After that, double-click `Fleet Remote.command` in Finder.

Always start the app through the launcher (or the newest versioned file): after an
update the old `fleet_remote_v*.py` is gone and a shortcut to it would break.

## Updating

Click **Check for updates** next to the version number at the top of the app. When a
newer version is on GitHub the button turns into **Update to vX.Y**. Click it: Fleet
Remote downloads the new file into its folder, installs any new packages, restarts on
the new version and removes the old file. Your devices and settings are not touched.

## Options

| Command | What it does |
|---|---|
| `python fleet_remote_v3.39.py` | Opens the app in its own window |
| `python fleet_remote_v3.39.py --browser` | Opens it in your web browser instead (use this if the window does not open) |
| `python fleet_remote_v3.39.py --port 9000` | Use a specific port |
| `python fleet_remote_v3.39.py --share` | Also lets other computers on the network open it in a browser; they must enter the access code printed in the console |

## Your devices and settings

Saved in a hidden folder called `.firecube-fleet` in your home folder
(on Windows `C:\Users\<you>\.firecube-fleet`):

- `devices.json`: your device list (and, for a Fire TV set to Never sleep, the
  sleep timers it had before, so Disabled can put them back)
- `adbkey`: the key the Fire TVs approved

To move to another computer, copy both files into the same folder there.
On a Mac, press Shift + Command + . in Finder to show hidden folders.

## Troubleshooting

- **No window appears:** run with `--browser`.
- **"python3: command not found" or version too old:** install Python from python.org.
- **A device will not connect:** check ADB debugging is on and the IP is reachable (try ping).
- **Mac asks to allow incoming network connections:** choose Allow.
