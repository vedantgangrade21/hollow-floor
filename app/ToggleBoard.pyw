"""Desktop app entry point for the effects board. Launched by its shortcut."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import applaunch

applaunch.run(os.path.join('toggle board', 'horror_toggle_board.py'),
              'Horror Toggle Board')
