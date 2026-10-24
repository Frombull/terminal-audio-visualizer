# Audio Visualizer

Terminal audio visualizer. The bars have a color fade and, when a strong bass hits, the terminal window shakes.

![screenshot](docs/screenshot.png)

## Usage

```powershell
pip install -r requirements.txt
python visualizer.py          # captures what is playing on your PC
python visualizer.py --demo   # no need to play anything
```

Keys: `M` menu, `P` palette, `S` shake, `F` effects, `Q` quit.

Too sensitive? Press `Down` (or open the menu with `M`). Sensitivity goes down to x0.02.
