import time
import threading
import tkinter as tk
from pynput.mouse import Button, Controller

mouse = Controller()

clicking = False

def clicker(interval):
    global clicking
    while clicking:
        mouse.click(Button.left)
        time.sleep(interval)

def start_clicking():
    global clicking
    if not clicking:
        clicking = True
        interval = float(interval_entry.get())
        thread = threading.Thread(target=clicker, args=(interval,))
        thread.daemon = True
        thread.start()

def stop_clicking():
    global clicking
    clicking = False

root = tk.Tk()
root.title("autoclicker")
root.geometry("500x350")

interval_label = tk.Label(root, text="Interval (s):")
interval_label.pack(pady=10)
interval_entry = tk.Entry(root)
interval_entry.pack(pady=5)
interval_entry.insert(0, "0.1")

start_button = tk.Button(root, text="start", command=start_clicking, bg="green", fg="white")
start_button.pack(pady=10)

stop_button = tk.Button(root, text="stop", command=stop_clicking, bg="red", fg="white")
stop_button.pack(pady=10)

root.mainloop()
