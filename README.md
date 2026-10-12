# Hollow Floor

A first-person horror game set in an endless Backrooms. Yellowed wallpaper,
humming lights, damp carpet, and no way out. Something lives under the floor.
It's called Diamond. When it digs its way up, run.

Everything is generated in code when the game starts. The rooms go on forever,
and the textures, lighting and every sound (music, footsteps, the screams) are
made procedurally. There are no image or audio files in the repo.

## Features

- **An endless maze:** rooms stream in around you as you walk. Failing
  fluorescent tubes flicker and tick, and the power browns out now and then.
- **Diamond:** it tears up through the floor in a cutscene and then hunts you
  around the walls. If it reaches you, the run is over.
- **Fire is the only defence:** lighters are rare. Throw one and Diamond burns,
  thrashes and collapses, buying you time before it digs back up.
- **Inventory:** five slots, or ten with a backpack. You can put items you
  don't need back down on the floor.
- **Survival timer:** each run tracks how long you lasted, and your best time
  is saved.
- Title menu, settings (sensitivity, FOV, draw distance, volume, screen effects,
  window shake, difficulty, fullscreen), a pause menu and a loading screen.

## Running it

You need **Windows** and **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/)).

1. Download this repo (green **Code** button → *Download ZIP*, then unzip), or clone it.
2. Open a terminal in the folder and install the three packages it needs:

   ```
   py -m pip install -r requirements.txt
   ```

3. Start the game by double-clicking **`run_backrooms.bat`**, or run:

   ```
   py backrooms.py
   ```

The `.bat` file looks for whichever Python on your PC has the packages
installed, so it works even with several versions of Python.

**Optional: desktop and Start Menu shortcuts with icons.** From the game folder, run:

```
powershell -ExecutionPolicy Bypass -File app\make_shortcuts.ps1
```

Run it again if you ever move the folder.

### If Windows blocks it

If you see *"DLL load failed … An Application Control policy has blocked this
file"*, Windows **Smart App Control** is refusing to load the Panda3D engine's
files. It isn't a bug in the game, and the code can't work around it. Smart App
Control is in *Windows Security → App & browser control*. Be careful: once
Windows turns it off, it can't be switched back on without reinstalling
Windows.

## Controls

| Key | Action |
| --- | --- |
| W A S D | move |
| Mouse | look around |
| Shift | sprint (watch the stamina bar) |
| Space | jump |
| E | pick up / open and close the backpack |
| 1–5, mouse wheel | choose an inventory slot (6–0 with the backpack open) |
| Left click / F | throw a lighter, or put the selected item down |
| Esc | pause |
| F1 / Tab | developer panel |

## What's in here

| Path | What it is |
| --- | --- |
| `backrooms.py` | the whole game in one file: world generation, shaders, monster, audio, menus |
| `run_backrooms.bat` | double-click launcher |
| `requirements.txt` | the packages it needs |
| `app/` | no-console launchers, icons, and the shortcut script |
| `toggle board/` | the effects test board (below) |

Your settings and best time are saved to `settings.json` next to the game. It's
created the first time you change a setting.

## The effects test board

`toggle board/horror_toggle_board.py` is a separate developer tool, not part of
the game. It's a panel of buttons for trying out "the game is taking over your
computer" effects before building them in. The effects act on the PC it runs
on: it puts text files on your Desktop (with a button to clean them up),
briefly changes the wallpaper, mouse trails and keyboard lights and then puts
them back, opens Notepad or Paint, and can move the real mouse to draw in Paint
(that one needs `pip install pyautogui`). One effect looks up the PC's public
IP address and rough location from **ip-api.com**, so that request does go over
the internet. Only run it on your own machine, and only if you're fine with
those effects.

Start it with `toggle board\run_board.bat`.

## Built with

[Ursina](https://www.ursinaengine.org/) on [Panda3D](https://www.panda3d.org/),
with custom GLSL shaders, and numpy and Pillow for the generated textures and
sound.
