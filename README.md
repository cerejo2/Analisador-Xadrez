# Analisador de Xadrez

Programa para Windows que **lê um tabuleiro de xadrez a partir do ecrã** (um site, um vídeo, uma aplicação qualquer), reconstrói a posição e a analisa com o Stockfish. Não usa nenhuma API do site: só olha para os píxeis.

## O que faz

- **Visão por computador sem bibliotecas de visão.** Encontra o tabuleiro dentro de uma área do ecrã pelas duas cores das casas, corta-o em 64 casas e reconhece cada peça por comparação com modelos aprendidos. Tudo em NumPy e Pillow.
- **Calibração com uma imagem.** Basta mostrar um tabuleiro na posição inicial: o programa descobre as cores, a orientação e aprende as 12 peças a partir dessas 32.
- **Segue o jogo.** Lê o ecrã várias vezes por segundo (2, 4 ou 8 Hz) e, quando a imagem estabiliza, procura a jogada legal que explica a mudança. As regras do xadrez corrigem uma casa mal lida e reconstroem duas jogadas feitas entre leituras.
- **Análise com o Stockfish.** Melhor jogada com seta no tabuleiro, até 5 linhas de análise, sequência prevista, barra e gráfico de avaliação ao longo do jogo.
- **Revisão das jogadas.** Classifica cada lance (livro, brilhante, melhor, imprecisão, erro…) pela perda de probabilidade de vitória e calcula a precisão de cada lado.
- **Terminal de diagnóstico.** Mostra em tempo real o que a visão e o motor estão a fazer: tempos de captura e reconhecimento, profundidade, nós por segundo.
- **Desenhado para não atrasar.** Captura só a área escolhida com GDI (via `ctypes`), tapa a própria janela para não se ler a si mesmo, reaproveita a posição do tabuleiro entre leituras e corre o motor numa thread à parte.

## Ficheiros

| Ficheiro | Para quê |
|---|---|
| `app.py` | Versão principal: estado do jogo, motor, classificação e ponte para a interface |
| `ui/index.html` | Interface (HTML, CSS e JavaScript numa janela pywebview) |
| `analisador.py` | Visão (encontrar tabuleiro, calibrar, reconhecer) e lógica; também é a versão simples em Tkinter |
| `selecionar.py` | Ecrã congelado para arrastar a área do tabuleiro |
| `Abrir_Analisador.bat` / `Abrir_Versao_Simples.bat` | Atalhos para abrir cada versão |

## Como usar

Precisa de Windows, Python 3 (testado com o 3.13) e do [Stockfish](https://stockfishchess.org/).

```bash
winget install Stockfish.Stockfish
pip install -r requirements.txt
python app.py
```

1. **Selecionar área:** arrasta à volta da zona onde aparece o tabuleiro (pode ser maior).
2. **Calibrar:** com um tabuleiro na posição inicial dentro da área.
3. **Ao vivo** (F2) ou **Analisar agora**.

A área, a calibração e as definições ficam em `config.json` e `modelos.npz`, que não vão para o git.

## Uso responsável

Feito para estudar partidas: rever jogos gravados, acompanhar transmissões ou analisar posições de livros e vídeos. **Usar um motor durante uma partida online contra outra pessoa é batota** e é proibido pelas regras de fair play do chess.com, do Lichess e de outros sites.
