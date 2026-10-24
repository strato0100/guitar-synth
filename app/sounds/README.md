# Bancos de sonido

Esta carpeta no se sube a git (salvo este archivo). La app busca aquí archivos
`.sf2` (FluidSynth) y `.sfz` (sfizz), también en subcarpetas.

| Carpeta | Tipo | Tamaño | Licencia | Fuente |
|---|---|---|---|---|
| GeneralUser-GS | SoundFont GM (128 instrumentos) | 32 MB | GeneralUser GS License (uso libre, también comercial) | https://github.com/mrbumpy409/GeneralUser-GS |
| MuseScore_General | SoundFont GM (derivada de FluidR3, muestras en mono: menos CPU) | 206 MB | MIT | https://ftp.osuosl.org/pub/musescore/soundfont/MuseScore_General/ |
| BengtNilsson.HeadroomPiano | SFZ: piano de cola Yamaha C3 | 150 MB | CC-BY-4.0 (atribuir a Bengt Nilsson) | https://github.com/sfzinstruments/BengtNilsson.HeadroomPiano |
| Ixox.Flute | SFZ: flauta | 11 MB | CC-BY-4.0 (atribuir a Ixox / Xavier Hosxe) | https://github.com/sfzinstruments/Ixox.Flute |
| karoryfer-bigcat.cello | SFZ: chelo (arco y pizzicato) | 140 MB | CC0 | https://github.com/sfzinstruments/karoryfer-bigcat.cello |

Cada carpeta conserva su archivo de licencia. Más instrumentos libres:
https://sfzinstruments.github.io

Para llevarlos a la Pi:

    rsync -av app/sounds/ patch@<ip-de-la-pi>:~/guitar-synth/app/sounds/
