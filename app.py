"""Analisador de Xadrez — interface nova (janela web com pywebview).

A visão e a lógica estão em analisador.py; aqui ficam o motor, o estado do jogo
e a ponte para a interface (ui/index.html).
"""
import base64
import ctypes
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes

import numpy as np
from PIL import ImageGrab
import chess
import chess.engine
import webview

import analisador as A

TITULO = "Analisador de Xadrez"
UI = os.path.join(A.PASTA, "ui", "index.html")
CPUS = os.cpu_count() or 4
THREADS = max(1, CPUS - 4)   # medido: com a app acima do normal, o motor em prioridade normal não abranda a captura
HASH = 512                   # MB
CLASSES = {  # classificação das jogadas (como na revisão do chess.com)
    "brilhante": "Brilhante", "excelente": "Excelente", "livro": "Livro", "melhor": "Melhor",
    "otimo": "Ótimo", "bom": "Bom", "imprecisao": "Imprecisão", "erro": "Erro",
    "chance": "Chance Perdida", "capivarada": "Capivarada",
}
VALOR = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def e_sacrificio(antes, lance):
    """A peça jogada fica onde o adversário a pode comer com lucro?"""
    depois = antes.copy()
    depois.push(lance)
    p = depois.piece_at(lance.to_square)
    if p is None or p.piece_type in (chess.PAWN, chess.KING):
        return False
    comida = antes.piece_at(lance.to_square)
    ganho = VALOR[comida.piece_type] if comida else 0
    atacantes = depois.attackers(depois.turn, lance.to_square)
    if not atacantes:
        return False
    menor = min(VALOR[depois.piece_at(s).piece_type] or 99 for s in atacantes)
    defendida = bool(depois.attackers(not depois.turn, lance.to_square))
    perda = VALOR[p.piece_type] - (menor if defendida else 0)
    return perda - ganho >= 2


def precisao(perda):
    """Fórmula ao estilo chess.com: perda de prob. de vitória -> precisão 0-100."""
    return max(0.0, min(100.0, 103.1668 * np.exp(-0.04354 * perda * 100) - 3.1669))


def hora():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


def fmt_n(n):
    n = n or 0
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= lim:
            return f"{n / lim:.1f}{suf}"
    return str(int(n))


def rect_janela():
    """Retângulo da nossa janela em píxeis reais (como o ImageGrab)."""
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, TITULO)
    if not hwnd:
        return None
    u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    antigo = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # por monitor (v2), só nesta thread
    try:
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
    finally:
        if antigo:
            u.SetThreadDpiAwarenessContext(ctypes.c_void_p(antigo))
    return r.left, r.top, r.right, r.bottom


def descobrir_lance(tab, alvo):
    """Qual jogada legal leva 'tab' à posição 'alvo' (board_fen)?"""
    for m in tab.legal_moves:
        tab.push(m)
        ok = tab.board_fen() == alvo
        tab.pop()
        if ok:
            return m
    return None


def score_branco(score):
    s = score.white()
    return f"#{s.mate()}" if s.is_mate() else f"{s.score() / 100:+.2f}"


class Nucleo:
    def __init__(self):
        self.cfg = self._ler_config()
        self.modelos = dict(np.load(A.MODELOS)) if os.path.exists(A.MODELOS) else None
        self.janela = None
        self.pronto = False
        self._saida, self._saida_lock = [], threading.Lock()
        self.fila = queue.Queue()
        self.ao_vivo = False
        self.motor, self.motor_nome = None, "Stockfish"
        self.pedido, self.ev_motor = None, threading.Event()
        # jogo
        self.grelha = self.pendente = None
        self.estavel = 0
        self.tab = None
        self.baixo_b = True
        self.historico = []
        self.aval = {}            # fen -> (prob. vitória de quem joga, melhor uci)
        self.visto = None
        self._assin = None
        self._invalida = None
        self._nao_explicada = None
        self._falhadas = set()
        self._t_img = 0.0
        self._grelha_img = None
        self._caixa = None
        self._an = None
        self._prec = (None, None)
        for alvo in (self._controlador, self._motor_loop, self._emissor):
            threading.Thread(target=alvo, daemon=True).start()

    # ------------------------------------------------------------ config
    def _ler_config(self):
        try:
            with open(A.CONFIG, encoding="utf-8") as f:
                c = json.load(f)
        except Exception:
            c = {}
        c.setdefault("linhas", 3)
        c.setdefault("hz", 4)
        c.setdefault("topo", False)
        c.setdefault("orient", "auto")
        c.setdefault("vez", "auto")
        return c

    def gravar(self):
        with open(A.CONFIG, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2)

    # ----------------------------------------------------------- eventos
    def emitir(self, tipo, **d):
        d["tipo"] = tipo
        with self._saida_lock:
            if tipo in ("imagem", "stats", "estado", "analise", "historico", "resumo", "visao"):
                self._saida = [e for e in self._saida if e["tipo"] != tipo]  # só interessa o mais recente
            self._saida.append(d)
            if tipo == "term" and len(self._saida) > 150:  # interface atrasada: larga as linhas mais velhas
                velhas = [e for e in self._saida if e["tipo"] == "term"][:-80]
                ids = {id(e) for e in velhas}
                self._saida = [e for e in self._saida if id(e) not in ids]

    def term(self, cat, txt):
        self.emitir("term", hora=hora(), cat=cat, txt=txt)

    def _emissor(self):
        while True:
            time.sleep(0.06)
            if not (self.pronto and self.janela):
                continue
            with self._saida_lock:
                lote, self._saida = self._saida, []
            if lote:
                try:
                    self.janela.evaluate_js(f"window.receber && window.receber({json.dumps(lote)})")
                except Exception:
                    A.registar_erro(traceback.format_exc())

    def passo(self):
        if "area" not in self.cfg:
            p = 1
        elif self.modelos is None:
            p = 2
        else:
            p = 3
        self.emitir("passo", passo=p)
        return p

    def estado(self, modo, texto):
        self.emitir("estado", modo=modo, texto=texto)

    def estado_inicial(self):
        self.pronto = True
        self.passo()
        self.estado("parado", "Parado")
        self.term("sys", f"analisador pronto · {CPUS} núcleos no CPU · motor com {THREADS} threads e {HASH} MB")
        if "area" in self.cfg:
            x0, y0, x1, y1 = self.cfg["area"]
            self.term("sys", f"área guardada: {x1 - x0}×{y1 - y0}px em ({x0}, {y0})")
        self.term("sys", "calibração carregada · 32 peças conhecidas" if self.modelos is not None
                  else "ainda sem calibração")
        return {"cfg": {k: self.cfg[k] for k in ("linhas", "hz", "topo", "orient", "vez")}}

    # ------------------------------------------------------------- visão
    def ler_ecra(self):
        t0 = time.perf_counter()
        x0, y0, x1, y1 = self.cfg["area"]
        img = A.capturar((x0, y0, x1, y1))
        t1 = time.perf_counter()
        r = rect_janela()
        if r:  # tapa a nossa própria janela
            ix0, iy0 = max(r[0], x0) - x0, max(r[1], y0) - y0
            ix1, iy1 = min(r[2], x1) - x0, min(r[3], y1) - y0
            if ix1 > ix0 and iy1 > iy0:
                img.paste((0, 0, 0), (ix0, iy0, ix1, iy1))
        clara, escura = self.modelos["clara"], self.modelos["escura"]
        caixa = self._caixa if (self._caixa and A.caixa_ainda_valida(img, self._caixa, clara, escura)) \
            else A.encontrar_tabuleiro(img, clara, escura)
        self._caixa = caixa
        t2 = time.perf_counter()
        if caixa is None:
            return None, None, {"cap": t1 - t0, "det": t2 - t1, "rec": 0, "caixa": None}
        recorte = img.crop(caixa)
        grelha = A.reconhecer(A.reduzir(recorte), self.modelos)
        t3 = time.perf_counter()
        return grelha, recorte, {"cap": t1 - t0, "det": t2 - t1, "rec": t3 - t2,
                                 "caixa": (caixa[0] + x0, caixa[1] + y0, caixa[2] - caixa[0], caixa[3] - caixa[1])}

    def _visao_loop(self):
        n, soma, t_beat = 0, np.zeros(3), time.time()
        while self.ao_vivo:
            t0 = time.time()
            try:
                grelha, img, info = self.ler_ecra()
                self.fila.put(("leitura", grelha, img, info, False))
                n += 1
                soma += (info["cap"], info["det"], info["rec"])
            except Exception:
                A.registar_erro(traceback.format_exc())
            if time.time() - t_beat > 4 and n:
                c, d, r = soma / n * 1000
                fps = n / (time.time() - t_beat)
                self.term("visão", f"{fps:.1f} leituras/s (alvo {self.cfg.get('hz', 4)} Hz) · captura {c:.0f} ms · procurar tabuleiro {d:.0f} ms"
                                   f" · ler 64 casas {r:.0f} ms")
                self.emitir("visao", fps=round(fps, 1))
                n, soma, t_beat = 0, np.zeros(3), time.time()
            periodo = 1.0 / max(1, int(self.cfg.get("hz", 4)))
            time.sleep(max(0.02, periodo - (time.time() - t0)))

    # ------------------------------------------------------- controlador
    def _controlador(self):
        while True:
            msg = self.fila.get()
            try:
                if msg[0] == "leitura":
                    self._leitura(*msg[1:])
                elif msg[0] == "analise":
                    self._analise(*msg[1:])
                elif msg[0] == "cmd":
                    msg[1]()
            except Exception as e:
                A.registar_erro(traceback.format_exc())
                self.term("erro", f"{type(e).__name__}: {e}")

    def _enviar_imagem(self, img, forcar=False):
        assin = np.asarray(img.resize((24, 24)), dtype=np.int16)
        if not forcar and self._assin is not None and self._assin.shape == assin.shape \
                and np.abs(self._assin - assin).mean() < 0.5 and time.time() - self._t_img < 1.5:
            return
        self._assin = assin
        self._t_img = time.time()
        buf = io.BytesIO()
        img.resize((640, 640)).save(buf, "JPEG", quality=88)
        self.emitir("imagem", src="data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())

    def _leitura(self, grelha, img, info, imediato):
        if not imediato and not self.ao_vivo:
            return
        if grelha is None:
            if self.visto is not False:
                self.term("visão", "tabuleiro não encontrado na área (tapado, fora da área ou a página mudou)")
                self.emitir("imagem", src=None)
                self.estado("procura", "À procura do tabuleiro…")
            self.visto = False
            return
        if self.visto is not True:
            x, y, w, h = info["caixa"]
            pecas = sum(ch != "." for ch in grelha)
            self.term("visão", f"tabuleiro encontrado · {w}×{h}px em ({x}, {y}) · {pecas} peças")
            self.estado("vivo" if self.ao_vivo else "parado", "Ao vivo" if self.ao_vivo else "Parado")
        self.visto = True
        self._enviar_imagem(img, imediato or grelha != self._grelha_img)
        self._grelha_img = grelha
        if grelha == self.pendente:
            self.estavel += 1
        else:
            self.pendente, self.estavel = grelha, 1
        if imediato:
            self._sincronizar(grelha, "análise pedida")
        elif self.estavel == 2:
            self._avaliar(grelha)
        elif self.estavel == 4 and self._nao_explicada == grelha:
            self._sincronizar(grelha, "posição mudou sem jogada reconhecida")

    def _mapa(self, grelha, baixo_b):
        return {A.casa_da_grelha(i, baixo_b): ch for i, ch in enumerate(grelha) if ch != "."}

    @staticmethod
    def _dif(tab, mapa):
        pm = tab.piece_map()
        n = 0
        for sq in pm.keys() | mapa.keys():
            p = pm.get(sq)
            if (p.symbol() if p else None) != mapa.get(sq):
                n += 1
        return n

    def _avaliar(self, grelha):
        """Leitura estável: que jogada(s) legal(is) explicam melhor o que se vê?"""
        self._nao_explicada = None
        inicial = grelha in (A.INICIAL, A.INICIAL[::-1])
        if self.tab is None or (inicial and self.tab.board_fen() != chess.STARTING_BOARD_FEN):
            return self._sincronizar(grelha, "novo jogo" if inicial else "primeira leitura")
        mapa = self._mapa(grelha, self.baixo_b)
        d0 = self._dif(self.tab, mapa)
        if d0 == 0:
            return
        t = self.tab
        melhor_d, lances = 99, None
        for m in t.legal_moves:
            t.push(m)
            d = self._dif(t, mapa)
            t.pop()
            if d < melhor_d:
                melhor_d, lances = d, [m]
        if melhor_d > 1 and 3 <= d0 <= 10 and grelha not in self._falhadas:  # talvez duas jogadas seguidas
            for m in list(t.legal_moves):
                t.push(m)
                for m2 in list(t.legal_moves):
                    t.push(m2)
                    d = self._dif(t, mapa)
                    t.pop()
                    if d < melhor_d:
                        melhor_d, lances = d, [m, m2]
                t.pop()
        if lances and melhor_d <= 1 and melhor_d < d0:
            if len(lances) == 2:
                self.term("visão", "duas jogadas entre leituras — reconstruídas pelas regras do xadrez")
            if melhor_d == 1:
                self.term("visão", "1 casa mal lida — corrigida pelas regras do xadrez")
            for m in lances:
                self._jogar(m)
            self.grelha = grelha
            self._nova_posicao()
            return
        if d0 <= 2:
            return  # ruído: animação, destaque ou casa mal lida
        if len(self._falhadas) > 200:
            self._falhadas.clear()
        self._falhadas.add(grelha)
        self._nao_explicada = grelha  # se continuar igual mais 2 leituras, ressincroniza

    def _jogar(self, lance):
        antes = self.tab
        novo = antes.copy()
        novo.push(lance)
        san = A.san_pt(antes, lance)
        self.historico.append({"san": san, "uci": lance.uci(), "cor": "w" if antes.turn else "b",
                               "antes": antes.fen(), "depois": novo.fen(), "classe": None})
        quem = "Brancas" if antes.turn else "Pretas"
        self.term("jogo", f"jogada das {quem}: {san} ({A.descrever(antes, lance)})")
        self.tab = novo

    def _sincronizar(self, grelha, motivo):
        """Constrói a posição diretamente a partir da leitura (início, ou quando se perdeu o fio)."""
        o = self.cfg.get("orient", "auto")
        baixo_b = A.brancas_em_baixo_auto(grelha) if o == "auto" else (o == "brancas")
        eu = chess.WHITE if baixo_b else chess.BLACK
        if grelha in (A.INICIAL, A.INICIAL[::-1]):
            if self.historico:
                self.term("jogo", "posição inicial — novo jogo")
            self.historico = []
            novo = chess.Board()
        else:
            vez = None
            if self.tab is not None and self.grelha is not None and baixo_b == self.baixo_b:
                _, vez = A.deduzir_vez(self.grelha, grelha, self.tab.turn)
            if vez is None:
                vez = eu  # por defeito: a tua vez
            v = self.cfg.get("vez", "auto")
            if v != "auto":
                vez = chess.WHITE if v == "brancas" else chess.BLACK
            novo = A.montar_tabuleiro(grelha, baixo_b, vez)
            if novo.status() & chess.STATUS_OPPOSITE_CHECK:
                novo.turn = not novo.turn
            if not novo.is_valid():
                if grelha != self._invalida:
                    self.term("visão", "posição impossível ignorada (peça a meio de um arrasto ou janela por cima?)")
                self._invalida = grelha
                return
            self.term("jogo", f"{motivo} — a seguir a partir desta posição")
        self.tab, self.baixo_b, self.grelha = novo, baixo_b, grelha
        self._nao_explicada = None
        self._nova_posicao()

    def _nova_posicao(self):
        novo = self.tab
        self._falhadas.clear()
        eu = chess.WHITE if self.baixo_b else chess.BLACK
        self.emitir("posicao", fen=novo.fen(), eu="w" if eu else "b", vez="w" if novo.turn else "b",
                    minha=novo.turn == eu)
        if novo.is_game_over():
            self.aval[novo.fen()] = (0.0 if novo.is_checkmate() else 0.5, None, None, 99)
            self._classificar()
            self._enviar_historico()
            texto = ("Xeque-mate — ganharam as " + ("Pretas" if novo.turn else "Brancas")) if novo.is_checkmate() \
                else "Empate"
            self.term("jogo", f"fim de jogo · {novo.result()} · {texto}")
            self.emitir("fim", texto=texto, prec_eu=self._prec[0], prec_adv=self._prec[1])
            self.pedido = None
            self._parar_analise()
            return
        self._enviar_historico()
        self.pedido = novo.copy()
        self.ev_motor.set()
        self._parar_analise()

    def _parar_analise(self):
        an = self._an
        if an is not None:
            try:
                an.stop()
            except Exception:
                pass

    # ------------------------------------------------------------- motor
    def _garantir_motor(self):
        if self.motor is not None:
            return
        caminho = A.procurar_stockfish(self.cfg.get("stockfish"))
        if caminho is None:
            raise RuntimeError("Stockfish não encontrado (instala com: winget install Stockfish.Stockfish)")
        self.motor = chess.engine.SimpleEngine.popen_uci(
            caminho, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS)
        try:
            self.motor.configure({"Threads": THREADS, "Hash": HASH})
        except Exception:
            pass
        self.cfg["stockfish"] = caminho
        self.motor_nome = self.motor.id.get("name", "Stockfish")
        self.term("motor", f"{self.motor_nome} iniciado · {THREADS} threads · hash {HASH} MB")
        self.emitir("motor", nome=self.motor_nome, threads=THREADS, hash=HASH)

    def _motor_loop(self):
        try:
            self._garantir_motor()
        except Exception as e:
            self.term("erro", str(e))
        while True:
            self.ev_motor.wait()
            self.ev_motor.clear()
            tab = self.pedido
            if tab is None:
                continue
            try:
                self._garantir_motor()
                linhas = int(self.cfg.get("linhas", 3))
                fen = tab.fen()
                self.term("motor", f"go infinite multipv {linhas} · {fen}")
                self.emitir("pensando", fen=fen)
                prof, prof_env, t_stats, t_env, t0 = 0, 0, 0.0, 0.0, time.time()
                final = []
                with self.motor.analysis(tab, multipv=linhas) as an:
                    self._an = an
                    if self.ev_motor.is_set():
                        an.stop()
                    for info in an:
                        if self.ev_motor.is_set():
                            break
                        agora = time.time()
                        if not self.ao_vivo and agora - t0 > 30:
                            self.term("motor", "parado ao fim de 30 s (o modo ao vivo está desligado)")
                            break
                        d = info.get("depth")
                        if info.get("multipv", 1) == 1 and d and d != prof and info.get("pv") \
                                and info.get("score") is not None:
                            prof = d
                            try:
                                pv = tab.variation_san(info["pv"][:6]).translate(A.PT)
                            except Exception:
                                pv = ""
                            self.term("motor", f"prof {d:>2}/{info.get('seldepth', d):<2}  "
                                               f"{score_branco(info['score']):>6}  {fmt_n(info.get('nodes')):>6} nós  "
                                               f"{fmt_n(info.get('nps')):>6}/s  › {pv}")
                        if agora - t_stats > 0.15:
                            t_stats = agora
                            self.emitir("stats", prof=info.get("depth", prof), sel=info.get("seldepth", 0),
                                        nos=info.get("nodes", 0), nps=info.get("nps", 0),
                                        hash=info.get("hashfull", 0), dec=agora - t0)
                        if prof > prof_env or (prof and agora - t_env > 0.6):
                            snap = [dict(i) for i in an.multipv if i and i.get("pv")]
                            if snap:
                                prof_env, t_env = prof, agora
                                self.fila.put(("analise", fen, snap, prof))
                    final = [dict(i) for i in an.multipv if i and i.get("pv")]
                self._an = None
                if final and not self.ev_motor.is_set():
                    self.fila.put(("analise", fen, final, prof))
            except Exception as e:
                self._an = None
                A.registar_erro(traceback.format_exc())
                try:
                    self.motor.quit()
                except Exception:
                    pass
                self.motor = None
                self.term("erro", f"motor: {e}")
                self.emitir("toast", tipo_t="erro", texto=f"Erro do motor: {e}")

    def _analise(self, fen, infos, prof):
        tab = self.tab
        if tab is None or fen != tab.fen():
            return
        validos = [i for i in infos if i.get("pv") and i.get("score") is not None]
        if not validos:
            return
        eu = chess.WHITE if self.baixo_b else chess.BLACK
        top = validos[0]
        lance = top["pv"][0]
        antigo = self.aval.get(fen)
        segunda = A.prob_vitoria(validos[1]["score"], tab.turn) if len(validos) > 1 else None
        self.aval[fen] = (A.prob_vitoria(top["score"], tab.turn), lance.uci(), segunda, prof)
        mudou = bool(antigo and antigo[1] and antigo[1] != lance.uci() and prof >= 12)
        if mudou:
            velho = A.san_pt(tab, chess.Move.from_uci(antigo[1]))
            self.term("motor", f"mudou de ideia na profundidade {prof}: {velho} → {A.san_pt(tab, lance)}")
        num, frase = A.avaliacao_texto(top["score"], eu)

        def centro(sq):
            f, rk = chess.square_file(sq), chess.square_rank(sq)
            col, lin = (f, 7 - rk) if self.baixo_b else (7 - f, rk)
            return [col + 0.5, lin + 0.5]

        alts = [{"san": A.san_pt(tab, i["pv"][0]), "num": A.avaliacao_texto(i["score"], eu)[0],
                 "desc": A.descrever(tab, i["pv"][0]), "prob": A.prob_vitoria(i["score"], eu)} for i in validos[1:]]
        try:
            seq = tab.variation_san(top["pv"][:10]).translate(A.PT)
        except Exception:
            seq = ""
        self.emitir("analise", fen=fen, minha=tab.turn == eu, eu="w" if eu else "b", prof=prof, mudou=mudou,
                    san=A.san_pt(tab, lance), desc=A.descrever(tab, lance), num=num, frase=frase,
                    prob=A.prob_vitoria(top["score"], eu), alts=alts, seq=seq,
                    setas=[{"de": centro(i["pv"][0].from_square), "para": centro(i["pv"][0].to_square)}
                           for i in validos[:2]])
        p_ant = antigo[3] if antigo else 0
        if (p_ant < 10 <= prof) or (prof >= 10 and prof != p_ant and prof % 4 == 0):
            self._classificar()
            self._enviar_historico()

    def _classificar(self):
        for k, h in enumerate(self.historico):
            if h["classe"] or h["antes"] not in self.aval or h["depois"] not in self.aval:
                continue
            p_melhor, melhor, p_segunda, prof_a = self.aval[h["antes"]]
            p_depois, _, _, prof_d = self.aval[h["depois"]]
            p_depois = 1 - p_depois
            if prof_a < 10 or prof_d < 10:  # ainda pouco pensado: espera
                continue
            perda = max(0.0, p_melhor - p_depois)
            antes = chess.Board(h["antes"])
            meia = (antes.fullmove_number - 1) * 2 + (0 if antes.turn else 1)
            if meia < 10 and perda <= 0.03 and abs(p_depois - 0.5) < 0.2 and not antes.gives_check(
                    chess.Move.from_uci(h["uci"])):
                c = "livro"
            elif perda <= 0.02 and p_depois >= 0.45 and e_sacrificio(antes, chess.Move.from_uci(h["uci"])):
                c = "brilhante"
            elif h["uci"] == melhor and p_segunda is not None and p_melhor - p_segunda >= 0.15 and p_depois >= 0.4:
                c = "excelente"
            elif h["uci"] == melhor:
                c = "melhor"
            elif perda <= 0.02:
                c = "otimo"
            elif perda <= 0.05:
                c = "bom"
            elif perda <= 0.10:
                c = "imprecisao"
            elif p_melhor >= 0.7 and p_depois >= 0.3:
                c = "chance"
            elif perda <= 0.20:
                c = "erro"
            else:
                c = "capivarada"
            h["classe"], h["perda"] = c, perda
            h["melhor_era"] = A.san_pt(chess.Board(h["antes"]), chess.Move.from_uci(melhor))
            if k == len(self.historico) - 1:
                quem = "tua" if (h["cor"] == "w") == self.baixo_b else "do adversário"
                self.term("jogo", f"{h['san']} ({quem}) → {CLASSES[c]} · perdeu {perda * 100:.0f}% de "
                                  f"probabilidade de vitória · melhor era {h['melhor_era']}")
                self.emitir("classificacao", san=h["san"], classe=c, rotulo=CLASSES[c], perda=perda,
                            melhor=h["melhor_era"], minha=(h["cor"] == "w") == self.baixo_b)

    def _enviar_historico(self):
        lista = [{"san": h["san"], "cor": h["cor"], "classe": h["classe"]} for h in self.historico]
        pontos = []
        if self.historico:
            fens = [self.historico[0]["antes"]] + [h["depois"] for h in self.historico]
        elif self.tab is not None:
            fens = [self.tab.fen()]
        else:
            fens = []
        for f in fens:
            if f in self.aval:
                p = self.aval[f][0]
                pontos.append(p if " w " in f else 1 - p)  # probabilidade das brancas
            else:
                pontos.append(None)
        self.emitir("historico", lista=lista, grafico=pontos, eu="w" if self.baixo_b else "b")
        # resumo: contagem por classificação e precisão de cada lado
        minha_cor = "w" if self.baixo_b else "b"
        resumo = {"eu": dict.fromkeys(CLASSES, 0), "adv": dict.fromkeys(CLASSES, 0)}
        prec = {"eu": [], "adv": []}
        for h in self.historico:
            if not h["classe"]:
                continue
            lado = "eu" if h["cor"] == minha_cor else "adv"
            resumo[lado][h["classe"]] += 1
            prec[lado].append(precisao(h.get("perda", 0)))
        self._prec = (round(float(np.mean(prec["eu"])), 1) if prec["eu"] else None,
                      round(float(np.mean(prec["adv"])), 1) if prec["adv"] else None)
        self.emitir("resumo", eu=resumo["eu"], adv=resumo["adv"],
                    prec_eu=round(float(np.mean(prec["eu"])), 1) if prec["eu"] else None,
                    prec_adv=round(float(np.mean(prec["adv"])), 1) if prec["adv"] else None)

    # ------------------------------------------------------------- ações
    def alternar_ao_vivo(self):
        if self.ao_vivo:
            self.ao_vivo = False
            self.pedido = None
            self._parar_analise()
            self.estado("parado", "Parado")
            self.term("sys", "modo ao vivo desligado")
            return False
        if self.passo() < 3:
            return False
        self.ao_vivo = True
        self.visto = None
        self.estado("procura", "À procura do tabuleiro…")
        self.term("sys", f"modo ao vivo ligado · a ler o ecrã {self.cfg.get('hz', 4)}× por segundo")
        threading.Thread(target=self._visao_loop, daemon=True).start()
        return True

    def analisar_agora(self):
        if self.passo() < 3:
            return
        self.term("sys", "análise pedida à mão")
        self.visto = None
        grelha, img, info = self.ler_ecra()
        self._leitura(grelha, img, info, True)

    def calibrar(self):
        if "area" not in self.cfg:
            return self.passo()
        self.term("sys", "calibração: à procura das cores do tabuleiro…")
        x0, y0, x1, y1 = self.cfg["area"]
        img = A.capturar((x0, y0, x1, y1))
        r = rect_janela()
        if r:
            ix0, iy0, ix1, iy1 = max(r[0], x0) - x0, max(r[1], y0) - y0, min(r[2], x1) - x0, min(r[3], y1) - y0
            if ix1 > ix0 and iy1 > iy0:
                img.paste((0, 0, 0), (ix0, iy0, ix1, iy1))
        res = A.detetar_cores(img)
        if res is None:
            self.term("erro", "calibração: nenhum tabuleiro dentro da área")
            self.emitir("toast", tipo_t="erro", texto="Não encontrei nenhum tabuleiro dentro da área.")
            return
        try:
            modelos = A.calibrar(A.reduzir(img.crop(res[2])))
        except ValueError as e:
            self.term("erro", f"calibração: {e}")
            self.emitir("toast", tipo_t="erro", texto=str(e).replace("\n", " "))
            return
        self.modelos = modelos
        np.savez(A.MODELOS, **modelos)
        c, e = modelos["clara"].astype(int), modelos["escura"].astype(int)
        self.term("sys", f"calibração feita · casa clara rgb{tuple(c)} · escura rgb{tuple(e)} · "
                         f"32 peças aprendidas · limiar de casa vazia {float(modelos['lim_vazio']):.3f}")
        self.emitir("toast", tipo_t="ok", texto="Calibração feita! Já podes ligar o modo ao vivo.")
        self.tab = None
        self.passo()
        self.analisar_agora()

    def definir(self, chave, valor):
        if chave not in ("linhas", "hz", "topo", "orient", "vez"):
            return
        if chave in ("linhas", "hz"):
            valor = int(valor)
        self.cfg[chave] = valor
        self.gravar()
        if chave == "topo" and self.janela:
            try:
                self.janela.on_top = bool(valor)
            except Exception:
                pass
        if chave in ("orient", "vez") and self.grelha:
            self.term("sys", f"definição '{chave}' = {valor} · a reanalisar")
            self._sincronizar(self.grelha, "definição mudada")
        if chave == "linhas" and self.tab is not None and not self.tab.is_game_over():
            self.term("sys", f"a mostrar {valor} linha(s) · a recomeçar a análise")
            self.pedido = self.tab.copy()
            self.ev_motor.set()
            self._parar_analise()

    def fechar(self):
        self.ao_vivo = False
        try:
            self.gravar()
            if self.motor:
                self.motor.quit()
        except Exception:
            pass


class Api:
    """Funções chamadas pela interface (JavaScript)."""

    def __init__(self, nucleo):
        self._n = nucleo

    def iniciar(self):
        return self._n.estado_inicial()

    def alternar_ao_vivo(self):
        return self._n.alternar_ao_vivo()

    def analisar_agora(self):
        self._n.fila.put(("cmd", self._n.analisar_agora))

    def calibrar(self):
        if self._n.ao_vivo:
            self._n.alternar_ao_vivo()
        self._n.fila.put(("cmd", self._n.calibrar))

    def definir(self, chave, valor):
        self._n.fila.put(("cmd", lambda: self._n.definir(chave, valor)))

    def selecionar_area(self):
        n = self._n
        if n.ao_vivo:
            n.alternar_ao_vivo()
        n.janela.hide()
        time.sleep(0.35)
        try:
            r = subprocess.run([sys.executable, os.path.join(A.PASTA, "selecionar.py")],
                               capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            area = json.loads((r.stdout or "null").strip().splitlines()[-1] if r.stdout.strip() else "null")
        except Exception:
            A.registar_erro(traceback.format_exc())
            area = None
        n.janela.show()
        if not area:
            n.term("sys", "seleção de área cancelada")
            return False
        n.cfg["area"] = area
        n._caixa = None
        n.gravar()
        n.term("sys", f"nova área: {area[2] - area[0]}×{area[3] - area[1]}px em ({area[0]}, {area[1]})")
        n.tab = None
        if n.passo() == 3:
            n.fila.put(("cmd", n.analisar_agora))
        return True


def geometria(cfg):
    """Tamanho/posição da janela que cabem no ecrã (em unidades lógicas, antes do pywebview)."""
    u = ctypes.windll.user32
    r = wintypes.RECT()
    u.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # área de trabalho do ecrã principal
    aw, ah = r.right - r.left, r.bottom - r.top
    w, h = min(1440, int(aw * 0.94)), min(860, int(ah * 0.94))
    g = {"width": w, "height": h, "x": r.left + (aw - w) // 2, "y": r.top + (ah - h) // 2}
    gu = cfg.get("janela")
    if gu and len(gu) == 4:
        vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        x, y, w2, h2 = gu
        if vx - 50 <= x < vx + vw - 200 and vy - 10 <= y < vy + vh - 100 and w2 >= 900 and h2 >= 600:
            g = {"width": w2, "height": h2, "x": x, "y": y}
    return g


def main():
    try:  # a app fica acima do normal (captura e interface nunca esperam); o Stockfish corre em normal
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00008000)  # ABOVE_NORMAL_PRIORITY_CLASS
    except Exception:
        pass
    nucleo = Nucleo()
    g = geometria(nucleo.cfg)
    janela = webview.create_window(TITULO, UI, js_api=Api(nucleo), min_size=(1060, 660),
                                   background_color="#0a0a0a", on_top=bool(nucleo.cfg.get("topo")), **g)
    nucleo.janela = janela

    def ao_fechar():
        try:
            nucleo.cfg["janela"] = [int(janela.x), int(janela.y), int(janela.width), int(janela.height)]
        except Exception:
            pass

    janela.events.closing += ao_fechar
    janela.events.closed += nucleo.fechar
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
