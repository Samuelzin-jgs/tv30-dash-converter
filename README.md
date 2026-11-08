# TV 3.0 DASH Converter

Conversor com interface grafica (Windows) para gravacoes DASH de TV 3.0 brasileira:
video **LCEVC sobre VVC/H.266 em 4K HDR10** + audio **MPEG-H 3D Audio**.
Arraste a pasta da gravacao para a janela e pronto: o programa junta os segmentos,
descarta duplicados, corrige sobreposicoes/lacunas da linha do tempo e converte para
**AV1 4K 10 bits HDR10 + FLAC/AAC** em um unico MKV.

![Captura](docs/screenshot.png)

## O que ele resolve

Gravacoes feitas de broadcast ISDB-Tb (TV 3.0) chegam como pastas de segmentos
DASH (`local_000.mpd`, `v1_1-*.m4s`, `v1_2-*.m4s`, `a2_3-*.m4s`, init em `*-init.mp4`),
muitas vezes divididas em varias pastas ("parte 1", "parte 2"...) com segmentos
duplicados na emenda. Este programa:

- **Inventario de segmentos** nas subpastas, escolhendo a copia maior em duplicatas;
- **Analisa a linha do tempo** (tfdt/trun de cada `moof`) e regenera o `SegmentTimeline`
  exato, eliminando as **sobreposicoes e lacunas** que causam "pulos" e audio repetido;
- **Monta um MPD estatico** com as duas camadas de video (base VVC 1080p +Enhancement
  LCEVC 4K, `dependencyId`) que o FFmpeg entende, e um `audio.mp4` concatenado;
- **Descarta o audio anterior ao inicio do video** (os segmentos de audio que
  sobram quando a gravacao comeca no meio de um segmento de video), e realinha
  o inicio do audio ao do video com `-itsoffset`, preservando a defasagem
  original entre as trilhas;
- **Alinha o inicio do audio ao do video** (`-itsoffset`) para nao haver atraso;
- Le os metadados **HDR10** (mastering display + MaxCLL/MaxFALL) do primeiro frame
  e reaplica no encoder;
- Codifica com **SVT-AV1** (`-crf`, `-preset` configuraveis) o video 4K decodificado
  com o filtro `lcevc`, e o audio MPEG-H com o decoder oficial da Fraunhofer
  (`libmpeghdec`), para FLAC 5.1.2 / 5.1 / estereo ou AAC estereo.

## Requisitos

- Windows 10/11 x64, CPU com AVX2.
- **FFmpeg com `libmpeghdec`, `lcevc` e `libsvtav1`**. O programa procura na ordem:
  1. o caminho escolhido no botao "Trocar FFmpeg...";
  2. `ffmpeg\bin\ffmpeg.exe` junto ao programa;
  3. `C:\ffmpeg-mpegh\bin\ffmpeg.exe` (padrao do script de build deste repo);
  4. `ffmpeg` no PATH.
  O FFmpeg comum (gyan.dev etc.) **nao serve**: nao decodifica MPEG-H audio.
- Python 3.10+ somente se quiser rodar de codigo (`pip` nao e necessario: so stdlib).

## Como gerar o FFmpeg (uma vez)

Instale o [MSYS2](https://www.msys2.org/) e, no terminal **MSYS2 UCRT64**:

```bash
git clone https://github.com/Samuelzin-jgs/tv30-dash-converter.git
cd tv30-dash-converter
bash tools/build-ffmpeg-mpegh.sh
```

O build usa `mpeghdec` (Fraunhofer) e `LCEVCdec` (V-NOVA) por ligacao estatica.
Como o `mpeghdec` tem licenca **nonfree**, o `ffmpeg.exe` resultante e para
**uso pessoal** e **nao pode ser redistribuido** (nem neste repositorio).

## Como usar

1. `python app.py` (ou duplo clique em `Converter.bat`, ou o .exe gerado).
2. Arraste a pasta raiz da gravacao (ex.: `captura-f1-gp-sp-qualy`) para a janela.
   Se houver mais de uma gravacao na pasta, cada uma aparece na lista; selecione as
   que quiser (Ctrl+clique).
3. Escolha CRF/preset/audio e a pasta de saida (padrao: junto a gravacao).
4. Clique em **Converter**. A preparacao e rapida (usa hardlinks, so copia o audio);
   a codificacao 4K e lenta (independente de GPU, ~0,3x no preset 9 com 12 threads).

O stage (`_tv30_stage`) e removido ao final; so o MKV final fica no disco.

## Limitacoes

- Presets 10+ do SVT e "8K" sao do dominio do FFmpeg/SVT, sem ajuste aqui.
- Se o ffprobe reportar mais de uma faixa de audio, e usada a primeira.
- Nao ha suporte a legendas nem a multiplos periodos DASH.

## Estrutura

```
app.py                    Interface (Tkinter + drag-and-drop nativo do Windows)
dashconv.py               DASH inventory/timeline/staging/encoding (sem dependencias)
tools/build-ffmpeg-mpegh.sh  Build do FFmpeg com MPEG-H + LCEVC no MSYS2
```

## Empacotar como .exe (opcional)

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name TV30Converter app.py
```

## Licenca

Codigo: MIT (veja LICENSE). O ffmpeg.exe compilado por `tools/build-ffmpeg-mpegh.sh`
e regido pelas licencas dos componentes (Fraunhofer MPEG-H e nonfree) e deve ser
recompilado por cada usuario.
