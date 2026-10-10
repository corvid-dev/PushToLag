"""Backend layer for PushToLag.

No module here imports tkinter. Anything that must run callbacks on a UI
thread takes a `dispatch(fn, *args)` callable at construction instead of a Tk
root (Tk callers pass `lambda fn, *a: root.after(0, fn, *a)`).
"""
