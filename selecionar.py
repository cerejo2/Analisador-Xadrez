"""Ecrã congelado para arrastar a área. Escreve [x0, y0, x1, y1] (ou null) no stdout."""
import ctypes
import json
import sys
import tkinter as tk

from PIL import ImageGrab, ImageTk, ImageEnhance

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

u = ctypes.windll.user32
vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
ecra = ImageGrab.grab(bbox=(vx, vy, vx + vw, vy + vh), all_screens=True)
escuro = ImageEnhance.Brightness(ecra).enhance(0.45)

root = tk.Tk()
root.overrideredirect(True)
root.geometry(f"{vw}x{vh}+{vx}+{vy}")
root.attributes("-topmost", True)
cv = tk.Canvas(root, width=vw, height=vh, highlightthickness=0, cursor="cross", bg="black")
cv.pack()
f_escuro = ImageTk.PhotoImage(escuro)
cv.create_image(0, 0, image=f_escuro, anchor="nw")
px, py = u.GetSystemMetrics(0) // 2 - vx, 56 - vy
cv.create_rectangle(px - 360, py - 26, px + 360, py + 26, fill="#111111", outline="#81b64c", width=2)
cv.create_text(px, py, fill="white", font=("Segoe UI", 13, "bold"),
               text="Arrasta à volta da zona do tabuleiro (pode ser maior)  ·  Esc cancela")
st = {"claro": None}


def carregar(e):
    st.update(x=e.x, y=e.y)
    st["r"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#81b64c", width=3)


def arrastar(e):
    if "r" not in st:
        return
    x0, x1 = sorted((st["x"], e.x))
    y0, y1 = sorted((st["y"], e.y))
    cv.coords(st["r"], x0, y0, x1, y1)
    if x1 - x0 > 4 and y1 - y0 > 4:  # mostra a zona escolhida "acesa"
        st["foto"] = ImageTk.PhotoImage(ecra.crop((x0, y0, x1, y1)))
        if st["claro"]:
            cv.delete(st["claro"])
        st["claro"] = cv.create_image(x0, y0, image=st["foto"], anchor="nw")
        cv.tag_raise(st["r"])


def largar(e):
    if "r" not in st:
        return
    x0, x1 = sorted((st["x"], e.x))
    y0, y1 = sorted((st["y"], e.y))
    print(json.dumps([vx + x0, vy + y0, vx + x1, vy + y1] if (x1 - x0 > 80 and y1 - y0 > 80) else None))
    root.destroy()


def cancelar(e=None):
    print("null")
    root.destroy()


cv.bind("<ButtonPress-1>", carregar)
cv.bind("<B1-Motion>", arrastar)
cv.bind("<ButtonRelease-1>", largar)
root.bind("<Escape>", cancelar)
root.focus_force()
root.mainloop()
sys.stdout.flush()
