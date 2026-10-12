"""Desktop app entry point for the game. Launched by the 'Hollow Floor' shortcut."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import applaunch

applaunch.run('backrooms.py', 'Hollow Floor')
