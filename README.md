# Fleet Remote

Control many Amazon Fire TV Cubes and Apple TVs from one window, by IP address.
Send remote keys to one device or a group, launch apps, type text, run shell
commands, see live screenshots, keep Fire TVs from going to sleep
(Never sleep) and see what a Fire TV sends over HDMI (resolution, frame rate,
audio format and channels). No ADB install needed.

Written in Python. Developed and tested on Windows. The Mac steps below should
work but have not been tested yet (live video needs macOS 13.3 or newer).

The number in the file name is the version (fleet_remote_v3.40.py = v3.40).
You only need the file name for the command-line options below.

## Before you start

- Fire TV: turn on **ADB debugging** (Settings > My Fire TV > Developer Options).
  The first time you connect, the TV asks you to allow the computer. Choose Always allow.
- Apple TV: you pair once with a 4 digit code shown on the TV.
- The computer must be on the same network as the devices (or reach it over VPN).

## Install and start (Windows and Mac)

1. Install Python 3.10 or newer from https://www.python.org/downloads/
   - Windows: tick "Add python.exe to PATH" in the installer.
   - Mac: after the installer, open Applications > Python 3.x and double-click
     `Install Certificates.command` once.
2. Download this repository: green **Code** button > **Download ZIP**, then unzip it.
3. **Windows:** double-click **`Fleet Remote.pyw`** in the unzipped folder.
4. **Mac:** a Mac opens Python files in the IDLE editor by default, so set this up once:
   1. In Finder, right-click `Fleet Remote.pyw` > **Get Info**.
   2. Under **Open with**, choose **Python Launcher** (in Applications > Python 3.x;
      if it is not in the list, pick **Other...** and find it there).
   3. Click **Change All...** > **Continue**.

   Now double-click `Fleet Remote.pyw`. A Terminal window may open too; you can close it,
   the app keeps running. (If IDLE still opens instead, press **F5** in the window with
   the code to start it.)

The first start installs what Fleet Remote needs (a small window says so, about a
minute). After that it opens straight away. Make a shortcut (Windows) or an alias
(Mac) to `Fleet Remote.pyw` on your Desktop if you like.

The launcher always starts the newest `fleet_remote_v*.py` in the folder, so keep
starting the app through it: after an update the old versioned file is gone.
If the setup fails, the details are in `.firecube-fleet/setup.log` in your home folder.

## Updating

Click **Check for updates** next to the version number at the top of the app. When a
newer version is on GitHub the button turns into **Update to vX.Y**. Click it: Fleet
Remote downloads the new file into its folder, installs any new packages, restarts on
the new version and removes the old file. Your devices and settings are not touched.

## Options

| Command | What it does |
|---|---|
| `python fleet_remote_v3.40.py` | Opens the app in its own window |
| `python fleet_remote_v3.40.py --browser` | Opens it in your web browser instead (use this if the window does not open) |
| `python fleet_remote_v3.40.py --port 9000` | Use a specific port |
| `python fleet_remote_v3.40.py --share` | Also lets other computers on the network open it in a browser; they must enter the access code printed in the console |

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
