# Audio Visualizer (terminal)

Visualizador de áudio colorido para o terminal do Windows. Quando bate um grave
forte, a **janela do terminal treme de verdade**.

## Uso

```powershell
pip install -r requirements.txt
python visualizer.py            # captura o que está tocando na saída padrão
python visualizer.py --demo     # sinal sintético (kick + chimbal), sem precisar de música
python visualizer.py --list-devices
python visualizer.py --device Mic   # escolhe a entrada por trecho do nome
```

Funciona melhor no **Windows Terminal** (cores 24 bits). Toque música em qualquer
programa: a entrada padrão é o *loopback* da saída de áudio, então não precisa de
microfone nem de "Stereo Mix". Também dá para escolher um microfone.

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
- Se o tremor disparar demais ou de menos, ajuste *Sensib. dos graves*.
