# Audio Visualizer

Visualizador de áudio para o terminal. As barras têm fade de cor e, quando bate um grave forte, a janela do terminal treme.

![screenshot](docs/screenshot.png)

## Uso

```bash
# Windows
pip install -r requirements.txt
python visualizer.py

# Linux (PulseAudio ou PipeWire)
sudo apt install python3-numpy pulseaudio-utils
python3 visualizer.py

python visualizer.py --demo   # sem precisar tocar nada
```

Teclas: `M` menu, `P` paleta, `S` tremor, `F` efeitos, `Q` sair.

No Linux a janela só treme em X11/XWayland; em Wayland puro treme só o conteúdo.
