CD Into Directory, Python MKUltra.py in terminal to start

diff --git a//Users/blake/Documents/Mac_Code/MKUltra/README.md b//Users/blake/Documents/Mac_Code/MKUltra/README.md
--- a//Users/blake/Documents/Mac_Code/MKUltra/README.md
+++ b//Users/blake/Documents/Mac_Code/MKUltra/README.md
@@ -1 +1,92 @@
-CD Into Directory, Python MKUltra.py in terminal to start
+# MK Ultra
+
+A lightweight, MK-inspired 2D fighting game prototype built in **Python + Pygame**.
+
+## Features
+
+- **Single-player** (vs CPU) and **local 2-player** (“Double”)
+- **Character select** + **stage select**
+- Keyboard and **gamepad** support (auto-detected)
+- Per-frame **hit/hurt/push box editor** (dev tool) saved to JSON
+- Music + SFX (fails gracefully if audio isn’t available)
+
+## Requirements
+
+- Python **3.10+**
+- `pygame` (Pygame Community Edition or classic pygame both work as long as `import pygame` succeeds)
+
+## Quick start
+
+```bash
+cd MKUltra
+python -m venv .venv
+source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
+pip install pygame
+python MKUltra.py
+```
+
+## Controls
+
+### Menu
+
+- **Up/Down**: `W/S` or `↑/↓`
+- **Select**: `Enter`
+- **Back/Quit**: `Esc`
+
+### Fight (keyboard)
+
+**Player 1**
+
+- Move: `A/D`
+- Jump: `W`
+- Crouch: `S`
+- Block: `F`
+- Attacks: `E`, `R`, `T`, `Y`
+
+**Player 2**
+
+- Move: `←/→`
+- Jump: `↑`
+- Crouch: `↓`
+- Block: `Right Ctrl`
+- Attacks: `Numpad 1/2/3/4`
+
+**Global**
+
+- Pause: `Esc` (or `P`)
+- Skip intro: `Space`
+
+### Gamepad (auto-mapped)
+
+- D-pad / left stick: move + jump/crouch
+- A/B/X/Y (Cross/Circle/Square/Triangle): attacks
+- RB / R1: block
+
+Controllers are assigned automatically:
+
+- 1 controller → Player 1
+- 2 controllers + “Double” → Player 2 gets controller #2
+
+## Project layout
+
+- `MKUltra.py` — game + menus (single-file prototype)
+- `menu/` — title/selection screens
+- `sprites/` — character sprite packs (currently: Nate, Scorpion, Connor, Blake)
+- `stages/` — stage backgrounds
+- `sounds/` — music + SFX (announcer, hits, damage, etc.)
+- `hitboxes_nate.json` — per-frame hit/hurt/push boxes (generated/edited in-game)
+
+## Hitbox editor (dev tool)
+
+During a fight press:
+
+- `F2` to toggle the editor
+- `1/2/3` to switch **push / hurt / hit** box mode
+- `Tab` to switch editing **P1 / P2**
+- Drag to draw, drag boxes to move, `Del` to delete
+- `Ctrl+S` to save (`hitboxes_nate.json`)
+
+## Troubleshooting
+
+- **Blank/black backgrounds or missing art:** some assets are referenced via absolute paths in `MKUltra.py`. If you cloned this repo to a different location, update the path constants near the top (menus) and `STAGES_DIR` (stages), or place assets in the expected sibling folders (`menu/`, `stages/`, etc.).
+- **No audio:** the game will continue silently if the mixer can’t initialize or files are missing.
