"""Analisador de Xadrez ao vivo.

1. Selecionar área  -> arrasta à volta da zona onde está o tabuleiro (pode ser maior, ex: a janela do browser).
2. Calibrar         -> uma vez, com um tabuleiro na POSIÇÃO INICIAL dentro da área.
3. Ao vivo          -> o programa encontra o tabuleiro, segue as jogadas e mostra a melhor jogada.
"""
import ctypes
import glob
import json
import os
import queue
import shutil
import threading
import time
import traceback

import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageGrab, ImageTk

import chess
import chess.engine

def ativar_dpi():  # coordenadas em píxeis reais (ecrãs com zoom 125%/150%)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

PASTA = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(PASTA, "config.json")
MODELOS = os.path.join(PASTA, "modelos.npz")
LOG = os.path.join(PASTA, "erros.log")

INICIAL = "rnbqkbnr" "pppppppp" + "." * 32 + "PPPPPPPP" "RNBQKBNR"  # brancas em baixo
TOL = 28        # cor -> "fundo da casa"
TOL_TAB = 20    # cor -> "casa do tabuleiro" (para encontrar o tabuleiro)
NOMES = {chess.PAWN: "Peão", chess.KNIGHT: "Cavalo", chess.BISHOP: "Bispo",
         chess.ROOK: "Torre", chess.QUEEN: "Dama", chess.KING: "Rei"}
PT = str.maketrans({"K": "R", "Q": "D", "R": "T", "N": "C"})  # notação portuguesa


def registar_erro(texto):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + texto + "\n")
    except Exception:
        pass


# ===================================================================== visão
def _mascara_tabuleiro(a, clara, escura, tol=TOL_TAB):
    return (np.linalg.norm(a - clara, axis=-1) < tol) | (np.linalg.norm(a - escura, axis=-1) < tol)


def _maior_componente(m):
    """Caixa (x0, y0, x1, y1) da maior mancha ligada de True."""
    h, w = m.shape
    flat = m.ravel().tolist()
    visto = bytearray(h * w)
    melhor, melhor_n = None, 0
    for ini in np.flatnonzero(m).tolist():
        if visto[ini]:
            continue
        visto[ini] = 1
        pilha = [ini]
        n, x0, y0, x1, y1 = 0, w, h, -1, -1
        while pilha:
            p = pilha.pop()
            n += 1
            y, x = divmod(p, w)
            if x < x0: x0 = x
            if x > x1: x1 = x
            if y < y0: y0 = y
            if y > y1: y1 = y
            for q, ok in ((p - 1, x > 0), (p + 1, x < w - 1), (p - w, y > 0), (p + w, y < h - 1)):
                if ok and flat[q] and not visto[q]:
                    visto[q] = 1
                    pilha.append(q)
        if n > melhor_n:
            melhor, melhor_n = (x0, y0, x1, y1), n
    return melhor, melhor_n


def encontrar_tabuleiro(img, clara, escura, tol=TOL_TAB):
    """Procura o maior tabuleiro com estas cores. Devolve (x0, y0, x1, y1) ou None."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    H, W, _ = a.shape
    f = max(1, int(np.ceil(max(H, W) / 160)))
    caixa, n = _maior_componente(_mascara_tabuleiro(a[::f, ::f], clara, escura, tol))
    if caixa is None or n < 64:
        return None
    x0, y0, x1, y1 = caixa
    x0, y0, x1, y1 = x0 * f, y0 * f, (x1 + 1) * f, (y1 + 1) * f
    # afinar as bordas em resolução total
    mg = f + 2
    X0, Y0, X1, Y1 = max(0, x0 - mg), max(0, y0 - mg), min(W, x1 + mg), min(H, y1 + mg)
    m = _mascara_tabuleiro(a[Y0:Y1, X0:X1], clara, escura, tol)
    ih, iw = m.shape
    cols = m[mg:ih - mg].mean(axis=0) if ih > 2 * mg else m.mean(axis=0)
    rows = m[:, mg:iw - mg].mean(axis=1) if iw > 2 * mg else m.mean(axis=1)
    cs, rs = np.flatnonzero(cols > 0.25), np.flatnonzero(rows > 0.25)
    if len(cs) == 0 or len(rs) == 0:
        return None
    x0, x1, y0, y1 = X0 + cs[0], X0 + cs[-1] + 1, Y0 + rs[0], Y0 + rs[-1] + 1
    w, h = x1 - x0, y1 - y0
    if min(w, h) < 64 or abs(w - h) > 0.08 * max(w, h):
        return None
    return int(x0), int(y0), int(x1), int(y1)


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def _preparar_gdi():
    V, I = ctypes.c_void_p, ctypes.c_int
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    u.GetDC.restype, u.GetDC.argtypes = V, [V]
    u.ReleaseDC.argtypes = [V, V]
    u.SetThreadDpiAwarenessContext.restype, u.SetThreadDpiAwarenessContext.argtypes = V, [V]
    g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = V, [V]
    g.CreateCompatibleBitmap.restype, g.CreateCompatibleBitmap.argtypes = V, [V, I, I]
    g.SelectObject.restype, g.SelectObject.argtypes = V, [V, V]
    g.BitBlt.restype, g.BitBlt.argtypes = I, [V, I, I, I, I, V, I, I, ctypes.c_uint32]
    g.GetDIBits.restype, g.GetDIBits.argtypes = I, [V, V, ctypes.c_uint, ctypes.c_uint, V, V, ctypes.c_uint]
    g.DeleteObject.argtypes, g.DeleteDC.argtypes = [V], [V]
    return u, g


try:
    _U32, _G32 = _preparar_gdi()
except Exception:
    _U32 = _G32 = None


def capturar(bbox):
    """Captura só a área pedida (píxeis reais). Muito mais rápido do que fotografar todos os monitores."""
    x0, y0, x1, y1 = (int(v) for v in bbox)
    w, h = x1 - x0, y1 - y0
    if _U32 is None or w <= 0 or h <= 0:
        return ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True)
    antigo = src = mem = bmp = None
    try:
        antigo = _U32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # por monitor (v2), só nesta thread
        src = _U32.GetDC(None)
        mem = _G32.CreateCompatibleDC(src)
        bmp = _G32.CreateCompatibleBitmap(src, w, h)
        velho = _G32.SelectObject(mem, bmp)
        ok = _G32.BitBlt(mem, 0, 0, w, h, src, x0, y0, 0x00CC0020)  # SRCCOPY
        _G32.SelectObject(mem, velho)
        bi = _BMIH(ctypes.sizeof(_BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        n = _G32.GetDIBits(src, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if not ok or n != h:
            raise OSError("BitBlt falhou")
        return Image.frombytes("RGB", (w, h), buf.raw, "raw", "BGRX")
    except Exception:
        return ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True)
    finally:
        if bmp:
            _G32.DeleteObject(bmp)
        if mem:
            _G32.DeleteDC(mem)
        if src:
            _U32.ReleaseDC(None, src)
        if antigo:
            _U32.SetThreadDpiAwarenessContext(ctypes.c_void_p(antigo))


def caixa_ainda_valida(img, caixa, clara, escura):
    """Confirma depressa se o tabuleiro continua no mesmo sítio (evita procurar tudo outra vez)."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    H, W, _ = a.shape
    x0, y0, x1, y1 = caixa
    if x1 > W or y1 > H or x1 - x0 < 64:
        return False
    frac = lambda px: float(_mascara_tabuleiro(px.reshape(-1, 3), clara, escura).mean())
    # cada lado: por dentro tem de ser tabuleiro, logo por fora não
    lados = [(a[y0 + 1:y0 + 3, x0:x1], a[y0 - 3:y0 - 1, x0:x1] if y0 >= 3 else None),
             (a[y1 - 3:y1 - 1, x0:x1], a[y1 + 1:y1 + 3, x0:x1] if y1 + 3 <= H else None),
             (a[y0:y1, x0 + 1:x0 + 3], a[y0:y1, x0 - 3:x0 - 1] if x0 >= 3 else None),
             (a[y0:y1, x1 - 3:x1 - 1], a[y0:y1, x1 + 1:x1 + 3] if x1 + 3 <= W else None)]
    for dentro, fora in lados:
        if frac(dentro) < 0.6 or (fora is not None and frac(fora) > 0.3):
            return False
    return True


def reduzir(img, lado=400):
    """Tabuleiros grandes são lidos a 400 px (50 px por casa): mais rápido e igualmente fiável."""
    return img.resize((lado, lado), Image.BILINEAR) if img.width > lado * 1.05 else img


def cortar_casas(img):
    a = np.asarray(img.convert("RGB"))
    h, w, _ = a.shape
    return [a[round(r * h / 8):round((r + 1) * h / 8), round(c * w / 8):round((c + 1) * w / 8)]
            for r in range(8) for c in range(8)]


def _dist_segmento(a, p, q):
    d = q - p
    t = np.clip(((a - p) @ d) / max(float(d @ d), 1.0), 0, 1)[..., None]
    return np.linalg.norm(a - (p + t * d), axis=-1)


def _cor_borda(a):
    borda = np.concatenate([a[:2].reshape(-1, 3), a[-2:].reshape(-1, 3),
                            a[:, :2].reshape(-1, 3), a[:, -2:].reshape(-1, 3)])
    return np.median(borda, axis=0)


def caracteristicas(casa, clara, escura):
    """Imagem 32x32 só com a peça (fundo = 0) e fração de píxeis de peça."""
    a = casa.astype(np.float32)
    h, w, _ = a.shape
    d = np.minimum(_dist_segmento(a, clara, escura), np.linalg.norm(a - _cor_borda(a), axis=-1))
    m = max(1, int(min(h, w) * 0.06))
    peca = (d > TOL)[m:h - m, m:w - m]
    cinza = a[m:h - m, m:w - m].mean(axis=2)
    feat = np.where(peca, np.clip(cinza + 20, 0, 255), 0).astype(np.uint8)
    feat = Image.fromarray(feat).resize((32, 32), Image.BILINEAR)
    return np.asarray(feat, dtype=np.float32), float(peca.mean())


def detetar_cores(img):
    """Descobre as duas cores do tabuleiro. Devolve (clara, escura, caixa) ou None."""
    a = np.asarray(img.convert("RGB"))
    f = max(1, max(a.shape[:2]) // 300)
    s = a[::f, ::f].reshape(-1, 3).astype(np.int32)
    chave = (s[:, 0] // 8) * 1024 + (s[:, 1] // 8) * 32 + s[:, 2] // 8
    vals, cont = np.unique(chave, return_counts=True)
    cores = [np.median(s[chave == k], axis=0).astype(np.float32) for k in vals[np.argsort(-cont)[:8]]]
    melhor, melhor_area = None, 0
    for i in range(len(cores)):
        for j in range(i + 1, len(cores)):
            A, B = cores[i], cores[j]
            if np.linalg.norm(A - B) < 40:
                continue
            caixa = encontrar_tabuleiro(img, A, B)
            if not caixa:
                continue
            casas = cortar_casas(img.crop(caixa))
            pares = []
            for k, casa in enumerate(casas):
                cb = _cor_borda(casa.astype(np.float32))
                e_a = np.linalg.norm(cb - A) < np.linalg.norm(cb - B)
                pares.append(e_a == ((k // 8 + k % 8) % 2 == 0))
            acerto = np.mean(pares)
            if max(acerto, 1 - acerto) < 0.75:
                continue
            area = (caixa[2] - caixa[0]) * (caixa[3] - caixa[1])
            if area > melhor_area:
                clara, escura = (A, B) if acerto >= 0.5 else (B, A)
                melhor, melhor_area = (clara, escura, caixa), area
    return melhor


def calibrar(img):
    """Aprende as peças a partir de um tabuleiro (já recortado) na posição inicial."""
    casas = cortar_casas(img)
    claras = [casas[i] for i in range(16, 48) if (i // 8 + i % 8) % 2 == 0]
    escuras = [casas[i] for i in range(16, 48) if (i // 8 + i % 8) % 2 == 1]
    clara = np.median(np.concatenate([c.reshape(-1, 3) for c in claras]), axis=0).astype(np.float32)
    escura = np.median(np.concatenate([c.reshape(-1, 3) for c in escuras]), axis=0).astype(np.float32)
    feats = [caracteristicas(c, clara, escura) for c in casas]

    def brilho(idx):
        v = [f[f > 0].mean() for f, _ in (feats[i] for i in idx) if (f > 0).any()]
        return np.mean(v) if v else 0

    rotulos = INICIAL if brilho(range(48, 64)) > brilho(range(0, 16)) else INICIAL[::-1]
    X, y, fp, fv = [], [], [], []
    for i, (f, frac) in enumerate(feats):
        if rotulos[i] == ".":
            fv.append(frac)
        else:
            X.append(f)
            y.append(rotulos[i])
            fp.append(frac)
    if min(fp) < max(fv) * 2:
        raise ValueError("Não consegui distinguir peças de casas vazias.\n"
                         "O tabuleiro está mesmo na posição inicial?")
    return {"X": np.array(X), "y": np.array(y), "clara": clara, "escura": escura,
            "lim_vazio": np.float32((max(fv) + min(fp)) / 2)}


def reconhecer(img, modelos):
    """String de 64 caracteres (linha 0 = topo do ecrã)."""
    res = []
    for casa in cortar_casas(img):
        f, frac = caracteristicas(casa, modelos["clara"], modelos["escura"])
        if frac < modelos["lim_vazio"]:
            res.append(".")
        else:
            res.append(str(modelos["y"][np.abs(modelos["X"] - f).mean(axis=(1, 2)).argmin()]))
    return "".join(res)


# ================================================================== lógica
def brancas_em_baixo_auto(grelha):
    lb = [i // 8 for i, ch in enumerate(grelha) if ch.isupper()]
    lp = [i // 8 for i, ch in enumerate(grelha) if ch.islower()]
    return True if not lb or not lp else bool(np.mean(lb) >= np.mean(lp))


def casa_da_grelha(i, baixo_b):
    r, c = divmod(i, 8)
    return chess.square(c, 7 - r) if baixo_b else chess.square(7 - c, r)


def montar_tabuleiro(grelha, baixo_b, vez):
    b = chess.Board(None)
    for i, ch in enumerate(grelha):
        if ch != ".":
            b.set_piece_at(casa_da_grelha(i, baixo_b), chess.Piece.from_symbol(ch))
    b.turn = vez
    b.set_castling_fen("KQkq")
    b.castling_rights = b.clean_castling_rights()
    return b


def deduzir_vez(antiga, nova, vez):
    """Vê o que mudou e diz de quem é a vez. Devolve (aceitar, vez ou None)."""
    if nova in (INICIAL, INICIAL[::-1]):
        return True, chess.WHITE
    if antiga is None:
        return True, vez
    saiu = [i for i in range(64) if antiga[i] != "." and antiga[i] != nova[i]]
    entrou = [i for i in range(64) if nova[i] != "." and antiga[i] != nova[i]]
    if not saiu or not entrou:  # peça a ser arrastada, ainda não é jogada
        return False, vez
    cores = {antiga[i].isupper() for i in saiu if nova[i] == "."}
    if len(cores) == 1:
        return True, chess.BLACK if cores.pop() else chess.WHITE
    return True, None if vez is None else (not vez)


def san_pt(tab, lance):
    return tab.san(lance).translate(PT)


def descrever(tab, lance):
    if tab.is_castling(lance):
        t = "Roque pequeno" if tab.is_kingside_castling(lance) else "Roque grande"
    else:
        p = tab.piece_at(lance.from_square)
        t = f"{NOMES[p.piece_type]} {chess.square_name(lance.from_square)} → {chess.square_name(lance.to_square)}"
        if tab.is_capture(lance):
            alvo = tab.piece_at(lance.to_square)
            t += f", captura {NOMES[alvo.piece_type if alvo else chess.PAWN].lower()}"
        if lance.promotion:
            t += f", promove a {NOMES[lance.promotion].lower()}"
    if tab.gives_check(lance):
        t += " (xeque!)"
    return t


def prob_vitoria(score, cor):
    s = score.pov(cor)
    if s.is_mate():
        return 1.0 if s.mate() > 0 else 0.0
    return 1 / (1 + 10 ** (-s.score() / 400))


def avaliacao_texto(score, cor):
    """(número, frase) do ponto de vista de 'cor' (o jogador)."""
    s = score.pov(cor)
    if s.is_mate():
        n = s.mate()
        return (f"M{abs(n)}", f"Tens mate em {n}!" if n > 0 else f"O adversário tem mate em {-n}")
    cp = s.score() / 100
    quem = "ti" if cp > 0 else "o adversário"
    x = abs(cp)
    frase = ("Posição equilibrada" if x < 0.5 else f"Ligeira vantagem para {quem}" if x < 1.5
             else f"Vantagem clara para {quem}" if x < 3 else f"Vantagem decisiva para {quem}")
    return f"{cp:+.1f}", frase


def procurar_stockfish(guardado=None):
    if guardado and os.path.isfile(guardado):
        return guardado
    cand = glob.glob(os.path.join(PASTA, "**", "stockfish*.exe"), recursive=True)
    if shutil.which("stockfish"):
        cand.append(shutil.which("stockfish"))
    local = os.environ.get("LOCALAPPDATA", "")
    cand.append(os.path.join(local, "Microsoft", "WinGet", "Links", "stockfish.exe"))
    cand += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*tockfish*", "**",
                                   "stockfish*.exe"), recursive=True)
    return next((c for c in cand if os.path.isfile(c)), None)


# ====================================================================== UI
BG, PAINEL, CARTAO, TXT, CINZA = "#1f1e1c", "#2b2a27", "#363431", "#eeeeee", "#9a9894"
VERDE, LARANJA, VERMELHO = "#81b64c", "#f0a020", "#e05a4f"


class App:
    LADO = 480

    def __init__(self, root):
        self.root = root
        root.title("Analisador de Xadrez")
        root.configure(bg=BG)
        root.report_callback_exception = self._erro_tk
        self.cfg = self._ler_config()
        self.modelos = dict(np.load(MODELOS)) if os.path.exists(MODELOS) else None
        self.fila = queue.Queue()
        self.motor = None
        self.pedido = None
        self.evento = threading.Event()
        self.ao_vivo = False
        self.rect_janela = (0, 0, 0, 0)
        # estado do jogo
        self.grelha = None      # posição aceite
        self.pendente, self.estavel = None, 0
        self.vez = None
        self.tab = None
        self.baixo_b = True
        self.analise = None     # (fen, infos)
        self.ultima_img = None
        self.foto = None

        self._estilos()
        self._construir()
        threading.Thread(target=self._motor_loop, daemon=True).start()
        root.after(80, self._processar_fila)
        root.protocol("WM_DELETE_WINDOW", self.fechar)
        root.bind("<F2>", lambda e: self.alternar_ao_vivo())
        root.attributes("-topmost", self.topo.get())
        self._passo_seguinte()
        self._desenhar()

    # ------------------------------------------------------------ construção
    def _estilos(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("TCombobox", fieldbackground=CARTAO, background=CARTAO, foreground=TXT,
                     arrowcolor=TXT, bordercolor=CARTAO, lightcolor=CARTAO, darkcolor=CARTAO)
        st.map("TCombobox", fieldbackground=[("readonly", CARTAO)], foreground=[("readonly", TXT)],
               selectbackground=[("readonly", CARTAO)], selectforeground=[("readonly", TXT)])
        self.root.option_add("*TCombobox*Listbox.background", CARTAO)
        self.root.option_add("*TCombobox*Listbox.foreground", TXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", VERDE)

    def _botao(self, pai, texto, cmd, cor=CARTAO, **kw):
        op = dict(bg=cor, fg=TXT, activebackground=VERDE, activeforeground="white", relief="flat", bd=0,
                  cursor="hand2", font=("Segoe UI", 10, "bold"), padx=10, pady=6)
        op.update(kw)
        return tk.Button(pai, text=texto, command=cmd, **op)

    def _rotulo(self, pai, texto="", tam=10, cor=TXT, peso="normal", **kw):
        op = dict(anchor="w", justify="left")
        op.update(kw)
        return tk.Label(pai, text=texto, bg=pai["bg"], fg=cor, font=("Segoe UI", tam, peso), **op)

    def _construir(self):
        r = self.root
        # --- barra de cima
        barra = tk.Frame(r, bg=PAINEL, padx=10, pady=8)
        barra.pack(fill="x")
        self._rotulo(barra, "♞  Analisador de Xadrez", 13, peso="bold").pack(side="left")
        self.btn_vivo = self._botao(barra, "▶  Ao vivo  (F2)", self.alternar_ao_vivo, VERDE)
        self.btn_vivo.pack(side="right", padx=3)
        self._botao(barra, "Analisar agora", self.analisar_agora).pack(side="right", padx=3)
        self._botao(barra, "Calibrar", self.calibrar).pack(side="right", padx=3)
        self._botao(barra, "Selecionar área", self.selecionar_area).pack(side="right", padx=3)

        corpo = tk.Frame(r, bg=BG, padx=10, pady=10)
        corpo.pack(fill="both", expand=True)
        # --- barra de avaliação + tabuleiro
        self.barra_av = tk.Canvas(corpo, width=26, height=self.LADO, bg=CARTAO, highlightthickness=0)
        self.barra_av.pack(side="left", padx=(0, 6))
        self.canvas = tk.Canvas(corpo, width=self.LADO, height=self.LADO, bg=PAINEL, highlightthickness=0)
        self.canvas.pack(side="left")
        # --- painel da direita
        lado = tk.Frame(corpo, bg=BG, padx=12, width=330)
        lado.pack(side="left", fill="both", expand=True)

        cartao = tk.Frame(lado, bg=CARTAO, padx=14, pady=12)
        cartao.pack(fill="x")
        self.l_vez = self._rotulo(cartao, "—", 11, CINZA, "bold")
        self.l_vez.pack(fill="x")
        self.l_lance = self._rotulo(cartao, "", 34, TXT, "bold")
        self.l_lance.pack(fill="x")
        self.l_desc = self._rotulo(cartao, "", 11, TXT, wraplength=290)
        self.l_desc.pack(fill="x")
        self.l_aval = self._rotulo(cartao, "", 11, CINZA, wraplength=290)
        self.l_aval.pack(fill="x", pady=(6, 0))

        self._rotulo(lado, "OUTRAS BOAS JOGADAS", 9, CINZA, "bold").pack(fill="x", pady=(14, 2))
        self.l_alt = self._rotulo(lado, "", 11, TXT, wraplength=310)
        self.l_alt.pack(fill="x")
        self._rotulo(lado, "SEQUÊNCIA PREVISTA", 9, CINZA, "bold").pack(fill="x", pady=(14, 2))
        self.l_seq = self._rotulo(lado, "", 10, TXT, wraplength=310)
        self.l_seq.pack(fill="x")

        # --- definições
        defs = tk.Frame(lado, bg=PAINEL, padx=10, pady=8)
        defs.pack(fill="x", side="bottom")
        linha1 = tk.Frame(defs, bg=PAINEL)
        linha1.pack(fill="x")
        self._rotulo(linha1, "Tabuleiro", 9, CINZA).pack(side="left")
        self.orient = ttk.Combobox(linha1, width=15, state="readonly",
                                   values=["Automático", "Brancas em baixo", "Pretas em baixo"])
        self.orient.current(0)
        self.orient.pack(side="left", padx=(4, 10))
        self._rotulo(linha1, "Vez", 9, CINZA).pack(side="left")
        self.vez_cb = ttk.Combobox(linha1, width=10, state="readonly", values=["Automático", "Brancas", "Pretas"])
        self.vez_cb.current(0)
        self.vez_cb.pack(side="left", padx=4)
        for cb in (self.orient, self.vez_cb):
            cb.bind("<<ComboboxSelected>>", lambda e: self._reanalisar())
        linha2 = tk.Frame(defs, bg=PAINEL)
        linha2.pack(fill="x", pady=(6, 0))
        self._rotulo(linha2, "Força (segundos a pensar)", 9, CINZA).pack(side="left")
        self.tempo = tk.DoubleVar(value=self.cfg.get("tempo", 1.0))
        def mudar(d):
            self.tempo.set(round(min(10.0, max(0.5, self.tempo.get() + d)), 1))
            l_t.config(text=f"{self.tempo.get():.1f} s")
            self._gravar_config()

        self._botao(linha2, "−", lambda: mudar(-0.5), padx=6, pady=0).pack(side="left", padx=(6, 0))
        l_t = self._rotulo(linha2, f"{self.tempo.get():.1f} s", 10, TXT, "bold", width=5, anchor="center")
        l_t.pack(side="left")
        self._botao(linha2, "+", lambda: mudar(0.5), padx=6, pady=0).pack(side="left", padx=(0, 10))
        self.topo = tk.BooleanVar(value=self.cfg.get("topo", True))
        tk.Checkbutton(linha2, text="Por cima", variable=self.topo, bg=PAINEL, fg=TXT, selectcolor=CARTAO,
                       activebackground=PAINEL, activeforeground=TXT, font=("Segoe UI", 9),
                       command=lambda: self.root.attributes("-topmost", self.topo.get())).pack(side="left")

        # --- estado
        est = tk.Frame(r, bg=PAINEL, padx=10, pady=5)
        est.pack(fill="x")
        self.ponto = tk.Label(est, text="●", bg=PAINEL, fg=CINZA, font=("Segoe UI", 11))
        self.ponto.pack(side="left")
        self.l_estado = self._rotulo(est, "", 9, TXT)
        self.l_estado.pack(side="left", padx=4)

    # --------------------------------------------------------------- utilit.
    def _ler_config(self):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _gravar_config(self):
        self.cfg["tempo"] = round(float(self.tempo.get()), 1)
        self.cfg["topo"] = bool(self.topo.get())
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2)

    def estado(self, msg, cor=CINZA):
        self.l_estado.config(text=msg)
        self.ponto.config(fg=cor)

    def _passo_seguinte(self):
        if "area" not in self.cfg:
            self.estado("Passo 1: clica 'Selecionar área' e arrasta à volta da zona do tabuleiro.", LARANJA)
        elif self.modelos is None:
            self.estado("Passo 2: mostra um tabuleiro na posição inicial dentro da área e clica 'Calibrar'.", LARANJA)
        else:
            self.estado("Pronto. Carrega em 'Ao vivo' (F2).", VERDE)

    def _erro_tk(self, exc, val, tb):
        texto = "".join(traceback.format_exception(exc, val, tb))
        registar_erro(texto)
        messagebox.showerror("Erro", f"{val}\n\n(detalhes em erros.log)")

    # ---------------------------------------------------------------- captura
    def _atualizar_rect_janela(self):
        r = self.root
        x, y = r.winfo_rootx(), r.winfo_rooty()
        self.rect_janela = (x - 10, y - 45, x + r.winfo_width() + 10, y + r.winfo_height() + 10)

    def _ler_ecra(self):
        """(corre numa thread) Captura, encontra o tabuleiro e lê as peças."""
        x0, y0, x1, y1 = self.cfg["area"]
        img = ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True)
        jx0, jy0, jx1, jy1 = self.rect_janela  # tapa a nossa janela para não se ler a si própria
        ix0, iy0, ix1, iy1 = max(jx0, x0) - x0, max(jy0, y0) - y0, min(jx1, x1) - x0, min(jy1, y1) - y0
        if ix1 > ix0 and iy1 > iy0:
            img.paste((0, 0, 0), (ix0, iy0, ix1, iy1))
        caixa = encontrar_tabuleiro(img, self.modelos["clara"], self.modelos["escura"])
        if caixa is None:
            return None, None
        recorte = img.crop(caixa)
        return reconhecer(recorte, self.modelos), recorte

    def _visao_loop(self):
        while self.ao_vivo:
            t0 = time.time()
            try:
                self.fila.put(("leitura",) + self._ler_ecra())
            except Exception:
                registar_erro(traceback.format_exc())
            time.sleep(max(0.05, 0.3 - (time.time() - t0)))

    # ----------------------------------------------------------------- ações
    def alternar_ao_vivo(self):
        if not self.ao_vivo:
            if "area" not in self.cfg or self.modelos is None:
                return self._passo_seguinte()
            self.ao_vivo = True
            self._atualizar_rect_janela()
            self.btn_vivo.config(text="■  Parar  (F2)", bg=VERMELHO)
            self.estado("Ao vivo: à procura do tabuleiro…", LARANJA)
            threading.Thread(target=self._visao_loop, daemon=True).start()
        else:
            self.ao_vivo = False
            self.btn_vivo.config(text="▶  Ao vivo  (F2)", bg=VERDE)
            self.estado("Parado.", CINZA)

    def analisar_agora(self):
        if "area" not in self.cfg or self.modelos is None:
            return self._passo_seguinte()
        self._atualizar_rect_janela()
        grelha, img = self._ler_ecra()
        self._nova_leitura(grelha, img, imediato=True)

    def _reanalisar(self):
        if self.pendente:
            g, self.grelha = self.pendente, None
            self._aceitar(g)

    def selecionar_area(self):
        if self.ao_vivo:
            self.alternar_ao_vivo()
        self.root.withdraw()
        self.root.after(300, self._abrir_selecao)

    def _abrir_selecao(self):
        u = ctypes.windll.user32
        vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        ecra = ImageGrab.grab(bbox=(vx, vy, vx + vw, vy + vh), all_screens=True)
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        top.attributes("-topmost", True)
        cv = tk.Canvas(top, width=vw, height=vh, highlightthickness=0, cursor="cross")
        cv.pack()
        cv.foto = ImageTk.PhotoImage(ecra)
        cv.create_image(0, 0, image=cv.foto, anchor="nw")
        px, py = u.GetSystemMetrics(0) // 2 - vx, 40 - vy  # centro do ecrã principal
        cv.create_rectangle(px - 430, py - 22, px + 430, py + 22, fill="#000000", outline=VERDE, width=2)
        cv.create_text(px, py, fill="white", font=("Segoe UI", 14, "bold"),
                       text="Arrasta à volta da zona onde está o tabuleiro (pode ser maior).   Esc = cancelar")
        st = {}

        def carregar(e):
            st.update(x=e.x, y=e.y, r=cv.create_rectangle(e.x, e.y, e.x, e.y, outline=VERDE, width=3))

        def arrastar(e):
            if "r" in st:
                cv.coords(st["r"], st["x"], st["y"], e.x, e.y)

        def largar(e):
            if "r" not in st:
                return
            x0, x1 = sorted((st["x"], e.x))
            y0, y1 = sorted((st["y"], e.y))
            top.destroy()
            self.root.deiconify()
            if x1 - x0 < 80 or y1 - y0 < 80:
                return messagebox.showwarning("Área pequena", "A área é demasiado pequena. Tenta outra vez.")
            self.cfg["area"] = [vx + x0, vy + y0, vx + x1, vy + y1]
            self._gravar_config()
            if self.modelos is not None:
                self.analisar_agora()
            self._passo_seguinte()

        def cancelar(e=None):
            top.destroy()
            self.root.deiconify()

        cv.bind("<ButtonPress-1>", carregar)
        cv.bind("<B1-Motion>", arrastar)
        cv.bind("<ButtonRelease-1>", largar)
        top.bind("<Escape>", cancelar)
        top.focus_force()

    def calibrar(self):
        if "area" not in self.cfg:
            return self._passo_seguinte()
        if self.ao_vivo:
            self.alternar_ao_vivo()
        self._atualizar_rect_janela()
        x0, y0, x1, y1 = self.cfg["area"]
        img = ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True)
        res = detetar_cores(img)
        if res is None:
            return messagebox.showerror("Calibrar", "Não encontrei nenhum tabuleiro dentro da área.\n"
                                                    "Confirma que a área apanha o tabuleiro inteiro.")
        try:
            modelos = calibrar(img.crop(res[2]))
        except ValueError as e:
            return messagebox.showerror("Calibrar", str(e))
        self.modelos = modelos
        np.savez(MODELOS, **modelos)
        self.grelha = None
        self.analisar_agora()
        self.estado("Calibração feita! Confirma no tabuleiro à esquerda e carrega em 'Ao vivo'.", VERDE)

    # -------------------------------------------------------- leitura → jogo
    def _nova_leitura(self, grelha, img, imediato=False):
        if grelha is None:
            self.ultima_img = None
            self.estado("Não vejo o tabuleiro na área (está tapado, fora da área ou a página mudou?).", VERMELHO)
            self._desenhar()
            return
        self.ultima_img = img
        if grelha == self.pendente:
            self.estavel += 1
        else:
            self.pendente, self.estavel = grelha, 1
        if imediato or self.estavel == 2:
            if grelha != self.grelha or imediato:
                self._aceitar(grelha, forcar=imediato)
        self._desenhar()

    def _aceitar(self, grelha, forcar=False):
        o = self.orient.current()
        baixo_b = brancas_em_baixo_auto(grelha) if o == 0 else (o == 1)
        antiga = None if (forcar or baixo_b != self.baixo_b) else self.grelha
        aceitar, vez = deduzir_vez(antiga, grelha, self.vez)
        if not aceitar:
            return
        if vez is None or (forcar and grelha not in (INICIAL, INICIAL[::-1])):
            vez = chess.WHITE if baixo_b else chess.BLACK  # por defeito: a tua vez
        v = self.vez_cb.current()
        if v:
            vez = chess.WHITE if v == 1 else chess.BLACK
        tab = montar_tabuleiro(grelha, baixo_b, vez)
        if tab.status() & chess.STATUS_OPPOSITE_CHECK:
            tab.turn = not tab.turn
        if not tab.is_valid():
            self.estado("Posição não reconhecida (peça a mexer, ou é preciso calibrar de novo).", LARANJA)
            return
        self.grelha, self.vez, self.tab, self.baixo_b = grelha, tab.turn, tab, baixo_b
        self.analise = None
        if tab.is_game_over():
            self._mostrar_fim()
            return
        self.estado("A pensar…", LARANJA)
        self.pedido = tab.copy()
        self.evento.set()
        self._mostrar_texto()

    # ------------------------------------------------------------------ motor
    def _motor_loop(self):
        while True:
            self.evento.wait()
            self.evento.clear()
            tab = self.pedido
            if tab is None:
                continue
            try:
                if self.motor is None:
                    caminho = procurar_stockfish(self.cfg.get("stockfish"))
                    if caminho is None:
                        self.fila.put(("sem_motor",))
                        continue
                    self.motor = chess.engine.SimpleEngine.popen_uci(caminho)
                    self.cfg["stockfish"] = caminho
                limite = chess.engine.Limit(time=float(self.tempo.get()))
                interrompido = False
                with self.motor.analysis(tab, limite, multipv=3) as an:
                    for _ in an:
                        if self.evento.is_set():
                            interrompido = True
                            an.stop()
                            break
                    infos = list(an.multipv)
                if not interrompido:
                    self.fila.put(("analise", tab.fen(), infos))
            except Exception as e:
                registar_erro(traceback.format_exc())
                self.motor = None
                self.fila.put(("erro", f"Erro do motor: {e}"))

    def _processar_fila(self):
        try:
            if self.ao_vivo:
                self._atualizar_rect_janela()
            while True:
                msg = self.fila.get_nowait()
                if msg[0] == "leitura" and self.ao_vivo:
                    self._nova_leitura(msg[1], msg[2])
                elif msg[0] == "analise" and self.tab is not None and msg[1] == self.tab.fen():
                    self.analise = msg[2]
                    self._mostrar_texto()
                    self._desenhar()
                    self.estado("Ao vivo — a seguir o jogo." if self.ao_vivo else "Análise feita.", VERDE)
                elif msg[0] == "erro":
                    self.estado(msg[1], VERMELHO)
                elif msg[0] == "sem_motor":
                    self._pedir_stockfish()
        except queue.Empty:
            pass
        except Exception:
            registar_erro(traceback.format_exc())
        self.root.after(80, self._processar_fila)

    def _pedir_stockfish(self):
        messagebox.showinfo("Stockfish", "Não encontrei o Stockfish. Indica onde está o stockfish.exe.")
        p = filedialog.askopenfilename(filetypes=[("Programa", "*.exe")])
        if p:
            self.cfg["stockfish"] = p
            self._gravar_config()
            self.evento.set()

    # --------------------------------------------------------------- mostrar
    def _mostrar_fim(self):
        tab = self.tab
        self.l_vez.config(text="FIM DE JOGO", fg=CINZA)
        if tab.is_checkmate():
            venc = "Pretas" if tab.turn == chess.WHITE else "Brancas"
            self.l_lance.config(text="Xeque-mate")
            self.l_desc.config(text=f"Ganharam as {venc}.")
        else:
            self.l_lance.config(text="Empate")
            self.l_desc.config(text="")
        for l in (self.l_aval, self.l_alt, self.l_seq):
            l.config(text="")
        self.estado("Fim de jogo.", CINZA)

    def _mostrar_texto(self):
        tab = self.tab
        eu = chess.WHITE if self.baixo_b else chess.BLACK
        minha = tab.turn == eu
        cor = "Brancas" if eu == chess.WHITE else "Pretas"
        if minha:
            self.l_vez.config(text=f"É A TUA VEZ  ·  jogas com as {cor}", fg=VERDE)
        else:
            self.l_vez.config(text="VEZ DO ADVERSÁRIO  ·  ele deve jogar:", fg=LARANJA)
        if not self.analise:
            self.l_lance.config(text="…")
            for l in (self.l_desc, self.l_aval, self.l_alt, self.l_seq):
                l.config(text="")
            return
        validos = [i for i in self.analise if i.get("pv")]
        if not validos:
            return
        top = validos[0]
        lance = top["pv"][0]
        num, frase = avaliacao_texto(top["score"], eu)
        self.l_lance.config(text=san_pt(tab, lance))
        self.l_desc.config(text=descrever(tab, lance))
        self.l_aval.config(text=f"Avaliação: {num}  ·  {frase}")
        alt = [f"{san_pt(tab, i['pv'][0]):7}  ({avaliacao_texto(i['score'], eu)[0]})  {descrever(tab, i['pv'][0])}"
               for i in validos[1:]]
        self.l_alt.config(text="\n".join(alt) if alt else "—")
        self.l_seq.config(text=tab.variation_san(top["pv"][:8]).translate(PT))

    def _desenhar(self):
        L, q = self.LADO, self.LADO / 8
        cv = self.canvas
        cv.delete("all")
        if self.ultima_img is None:
            cv.create_text(L / 2, L / 2, fill=CINZA, font=("Segoe UI", 12), justify="center",
                           text="O tabuleiro aparece aqui\nquando for encontrado na área.")
        else:
            self.foto = ImageTk.PhotoImage(self.ultima_img.resize((L, L), Image.BILINEAR))
            cv.create_image(0, 0, image=self.foto, anchor="nw")
        # seta da melhor jogada
        if self.analise and self.tab is not None and self.ultima_img is not None and self.grelha == self.pendente:
            validos = [i for i in self.analise if i.get("pv")]
            if validos:
                lance = validos[0]["pv"][0]
                eu = chess.WHITE if self.baixo_b else chess.BLACK
                cor = VERDE if self.tab.turn == eu else LARANJA

                def centro(sq):
                    f, rk = chess.square_file(sq), chess.square_rank(sq)
                    col, lin = (f, 7 - rk) if self.baixo_b else (7 - f, rk)
                    return col * q + q / 2, lin * q + q / 2

                (x0, y0), (x1, y1) = centro(lance.from_square), centro(lance.to_square)
                cv.create_oval(x0 - q * 0.42, y0 - q * 0.42, x0 + q * 0.42, y0 + q * 0.42, outline=cor, width=4)
                cv.create_line(x0, y0, x1, y1, width=q * 0.17, arrow="last",
                               arrowshape=(q * 0.42, q * 0.45, q * 0.2), fill=cor, capstyle="round")
        self._desenhar_barra()

    def _desenhar_barra(self):
        b, L = self.barra_av, self.LADO
        b.delete("all")
        eu = chess.WHITE if self.baixo_b else chess.BLACK
        cor_eu, cor_ele = ("#f0f0f0", "#403d39") if eu == chess.WHITE else ("#403d39", "#f0f0f0")
        p = 0.5
        num = ""
        if self.analise:
            validos = [i for i in self.analise if i.get("pv")]
            if validos:
                p = prob_vitoria(validos[0]["score"], eu)
                num = avaliacao_texto(validos[0]["score"], eu)[0]
        h = L * (1 - p)
        b.create_rectangle(0, 0, 26, h, fill=cor_ele, outline="")
        b.create_rectangle(0, h, 26, L, fill=cor_eu, outline="")
        if num:
            y = L - 10 if p >= 0.5 else 10
            b.create_text(13, y, text=num.lstrip("+"), font=("Segoe UI", 7, "bold"),
                          fill="#403d39" if (p >= 0.5) == (eu == chess.WHITE) else "#f0f0f0")

    def fechar(self):
        self.ao_vivo = False
        try:
            self._gravar_config()
            if self.motor:
                self.motor.quit()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    ativar_dpi()
    raiz = tk.Tk()
    App(raiz)
    raiz.mainloop()
