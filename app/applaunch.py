"""Shared launcher used by the desktop apps.

Runs a script from the project folder with no console window. If it dies, the
traceback goes into a window you can read and copy, instead of a console that
flashes past or nothing at all.
"""
import os
import runpy
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def show_error(title, text, auto_close_ms=None):
    """Put a traceback on screen. Falls back to stderr if tkinter is missing."""
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        win = tk.Tk()
        win.title(f'{title} - failed to start')
        win.configure(bg='#1b1b1f')
        win.geometry('900x460')
        head = tk.Label(win, text=f'{title} could not start', bg='#1b1b1f',
                        fg='#ff8f7a', anchor='w', padx=14, pady=10,
                        font=tkfont.Font(size=13, weight='bold'))
        head.pack(fill='x')
        box = tk.Text(win, bg='#111114', fg='#ddd8cc', insertbackground='#ddd8cc',
                      wrap='word', relief='flat', padx=12, pady=10)
        box.insert('1.0', text)
        box.configure(state='disabled')
        box.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        if auto_close_ms:
            win.after(auto_close_ms, win.destroy)
        win.mainloop()
    except Exception:
        sys.stderr.write(text)


def run(script, title):
    """Run `script` (a path relative to the game folder, the one holding app/)
    as if double-clicked."""
    target = os.path.join(ROOT, script)
    os.chdir(ROOT)                      # ursina resolves its assets from the cwd
    sys.path.insert(0, ROOT)
    if not os.path.exists(target):
        show_error(title, f'Cannot find:\n  {target}')
        return
    try:
        runpy.run_path(target, run_name='__main__')
    except SystemExit:
        pass
    except BaseException:
        show_error(title, traceback.format_exc())
