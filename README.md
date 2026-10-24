# Audio Visualizer (terminal)

Visualizador de áudio colorido para o terminal, no **Windows** e no **Linux**.
Quando bate um grave forte, a **janela do terminal treme de verdade**.

## Uso

```bash
python visualizer.py            # captura o que está tocando na saída padrão
python visualizer.py --demo     # sinal sintético (kick + chimbal), sem precisar de música
python visualizer.py --list-devices
python visualizer.py --device Mic   # escolhe a entrada por trecho do nome
```

Em ambos os sistemas a entrada padrão é o que você está **ouvindo** (não precisa de
microfone nem de "Stereo Mix"); também dá para escolher um microfone. Use um
terminal com cores 24 bits (Windows Terminal, GNOME Terminal, Konsole, kitty,
alacritty, xterm...).

### Windows

```powershell
pip install -r requirements.txt
python visualizer.py
```

Captura via loopback WASAPI (PyAudioWPatch).

### Linux

```bash
sudo apt install python3-numpy pulseaudio-utils   # PipeWire: pipewire-pulse + pulseaudio-utils
python3 visualizer.py
# ou, em venv:  pip install numpy
```

A captura usa `pactl` (lista as fontes, incluindo os *monitores* das saídas) e
`parec`, então funciona com **PulseAudio e PipeWire** sem nenhuma lib Python extra
além do numpy. Se o terminal for X11 (ou XWayland), a janela treme de verdade;
veja "Tremor" abaixo.

## Teclas

| Tecla | Ação |
|---|---|
| `M` / `Enter` | abre/fecha o menu de configurações |
| `↑` `↓` | sensibilidade (ou item anterior/próximo no menu) |
| `←` `→` | nº de barras (ou ajusta o item no menu) |
| `P` | próxima paleta de cores |
| `S` | modo do tremor |
| `F` | efeitos cósmicos (tudo / só estrelas / só faíscas / desligado) |
| `D` | próxima entrada de áudio |
| `R` (no menu) | restaura os padrões |
| `Q` / `Esc` | sair |

## Configurações (menu)

Entrada de áudio, sensibilidade, ganho automático, nº de barras, movimento suave
(ataque rápido / queda lenta), paleta (fade de cor: neon, fire, ocean, sunset,
matrix, rainbow, ice), estilo (barras ou espelhado), reflexo, picos flutuantes,
tremor (automático / janela / conteúdo / desligado), força do tremor,
sensibilidade dos graves e FPS. Tudo é salvo em `settings.json`.

## Efeitos cósmicos

Uma camada em braille (2×4 pontos por célula, bem mais fina que os blocos das
barras) desenhada atrás das barras:

- **Estrelas em warp**: flutuam devagar; quanto mais alto o som, mais rápido
  passam, e a cada pancada de grave entram em hiperespaço com riscos de velocidade.
- **Faíscas**: a cada kick saem do topo das barras mais altas, com gravidade e
  na cor da paleta.
- **Ondas de choque**: anéis que se expandem do chão nos graves mais fortes.

## Tremor

- **Janela**: move a janela do terminal alguns pixels a cada pancada de grave.
  Não funciona com a janela maximizada/minimizada nem em terminais embutidos
  (ex.: VS Code); no modo *Automático* ele cai para o tremor de **conteúdo**
  (as barras balançam dentro do terminal). Se você arrastar a janela, ela passa
  a tremer em volta da nova posição.
- **Linux**: a janela é movida via Xlib, então só funciona em **X11/XWayland**.
  O terminal precisa expor a janela: a variável `WINDOWID` (xterm, kitty, alacritty,
  konsole...) ou, sem ela, a janela ativa (`xdotool` ou `xprop`). Em **Wayland
  puro** nenhum programa pode mover a própria janela, então o modo Automático usa o
  tremor de conteúdo. Ao abrir, a janela dá um pulinho de ~20 px: é a calibração,
  que descobre se o gerenciador de janelas aceita mover e o desvio causado pela
  decoração; em janela maximizada ou gerenciador tiling ela é ignorada.
- Se o tremor disparar demais ou de menos, ajuste *Sensib. dos graves*.
