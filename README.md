# 🎸 Patchbox Guitar Synth - Controlador Web de Instrumentos

Aplicación web interactiva y motor de audio en tiempo real diseñado para correr en una **Raspberry Pi** con **Patchbox OS** (Pisound / USB Audio). Permite transformar las notas tocadas en tu guitarra eléctrica en cualquier instrumento musical en tiempo real mediante **JACK**, **Aubio** y **FluidSynth**.

---

## 🔍 ¿Por qué `guitar_pitch.py` sonaba siempre como Piano?

En tu archivo original `guitar_pitch.py`, en las líneas **47** y **109-112**:

```python
SOUNDFONT_PROGRAM = 0
MIDI_CHANNEL = 0
...
# Seleccionar piano acústico General MIDI
midi.send_message([
    0xC0 | MIDI_CHANNEL,
    SOUNDFONT_PROGRAM
])
```

- El mensaje MIDI `0xC0` es un **Program Change** (cambio de instrumento).
- En el estándar **General MIDI (GM)**, el programa **`0`** corresponde a **Acoustic Grand Piano (Piano de Cola Acústico)**.
- FluidSynth (que corre en Patchbox OS cargado con una SoundFont GM como `FluidR3_GM.sf2`) recibía la orden fija de reproducir el programa 0.
- Existen **128 instrumentos estándar (programas 0 al 127)**:
  - `0 - 7`: Pianos y teclados (Cola, Rhodes, Clavicordio...)
  - `16 - 23`: Órganos (Hammond B3, Rock, Iglesia, Acordeón...)
  - `24 - 31`: Guitarras (Española nylon, acústica acero, overdrive, distorsión...)
  - `32 - 39`: Bajos (Contrabajo, bajo eléctrico con dedos, púa, slap, synth bass...)
  - `40 - 47`: Cuerdas solistas (Violín, viola, violonchelo...)
  - `48 - 55`: Ensambles orquestales y coros celestiales
  - `56 - 63`: Metales (Trompeta, trombón, corno francés, metales synth...)
  - `64 - 71`: Cañas y saxos (Saxo alto, tenor, barítono, clarinete, oboe...)
  - `72 - 79`: Vientos y flautas (Flauta dulce, travesera, zampoña...)
  - `80 - 87`: Sintetizadores Lead (Onda cuadrada 8-bit, sierra analógica, quintas...)
  - `88 - 95`: Sintetizadores Pad (New Age, Warm analógico, coro espacial, halo...)
  - `104 - 111`: Instrumentos étnicos (Sitar, Banjo, Koto, Gaita...)

---

## 🚀 Qué incluye esta aplicación web

1. **Selector de Instrumentos en Tiempo Real**:
   - Los 128 instrumentos General MIDI categorizados en 16 grupos con nombres en español e inglés e iconos.
   - Buscador rápido (ej. escribe *sax*, *violin*, *organ*, *lead*, *bajo*).
   - Botón de **"Escuchar / Audicionar"** para probar el sonido directamente desde el móvil/navegador sin necesidad de tocar la guitarra.
   - Barra de **Presets Favoritos (1-Click)** para cambiar rápidamente de instrumento en el escenario.
2. **Dashboard en Vivo para Escenario**:
   - **Nota en vivo y Afinador**: Muestra la nota actual en grande (ej. `E2`, `A2`, `C#4`) y la frecuencia exacta en Hz.
   - **Vúmetro de Señal**: Medidor visual de nivel RMS de la cuerda de la guitarra.
   - **Transposición de Octava Inmediata**: Botones para transponer `-2, -1, 0, +1, +2` octavas (indispensable para cuando tocas un bajo y quieres que suene 1 octava más grave, o una flauta/violín 1 octava más aguda).
   - **Volumen Master**: Control deslizable de ganancia MIDI (CC 7).
   - **Botón PANIC (🛑)**: Apaga de inmediato cualquier nota colgada.
   - **Botón Silenciar / Activo (⚡)**: Pausa o reanuda la conversión al instante.
3. **Ajustes Avanzados**:
   - Ajuste fino del Noise Gate / Sensibilidad (RMS mínimo).
   - Modo de velocidad de nota: fija (100) o sensible a la fuerza del ataque de púa.
   - Ajuste de estabilidad de detección (evita notas fantasma).

---

## 📂 Estructura de Archivos

```
guitar-synth/
├── app.py                  # Servidor Web FastAPI con WebSockets en tiempo real
├── engine.py               # Motor de audio JACK + Aubio + MIDI (Pitch-to-MIDI)
├── instruments.py          # Definición de los 128 instrumentos General MIDI
├── requirements.txt        # Dependencias de Python
├── install_patchbox.sh     # Script automatizado de instalación en Patchbox OS
├── start.sh                # Script de inicio rápido
├── guitar-synth.service    # Servicio systemd para autoarranque opcional
├── README.md               # Esta guía
└── static/
    ├── index.html          # Interfaz web de usuario
    ├── style.css           # Estilos tipo pedalera digital de estudio (Dark mode)
    └── app.js              # Lógica de cliente, WebSockets y favoritos
```

---

## 🛠️ Instalación en la Raspberry Pi (Patchbox OS)

### 1. Copiar la carpeta a tu Raspberry Pi
Desde tu computadora, puedes copiar la carpeta mediante `scp`:
```bash
scp -r /mnt/Datos/Proyectos/CtrlpGT/mdl/guitar-synth patch@patchbox.local:/home/patch/
```
*(O simplemente cópiala a una memoria USB y pégala en la Raspberry Pi en `/home/patch/guitar-synth`)*.

### 2. Conectarte a la Raspberry Pi y ejecutar la instalación
```bash
ssh patch@patchbox.local
# (La contraseña por defecto en Patchbox OS suele ser: blokaslabs)

cd /home/patch/guitar-synth
bash install_patchbox.sh
```

El script instalará automáticamente las dependencias del sistema:
- `libjack-jackd2-dev` y servidor JACK
- `fluidsynth` y SoundFont `fluid-soundfont-gm`
- Librerías Python: `fastapi`, `uvicorn`, `JACK-Client`, `aubio`, `python-rtmidi`.

---

## ⚡ Ejecución

1. Asegúrate de que **FluidSynth** esté activo en Patchbox OS:
   ```bash
   patchbox module activate fluidsynth
   ```
2. Ejecuta la aplicación:
   ```bash
   ./start.sh
   ```
3. Conéctate desde cualquier dispositivo (celular, tablet o laptop conectado a la misma red Wi-Fi o al punto de acceso Hotspot de Patchbox OS) abriendo en el navegador:
   ```
   http://patchbox.local:8000
   ```
   O usando la IP local de tu Raspberry Pi:
   ```
   http://192.168.1.XX:8000
   ```

---

## 🔄 (Opcional) Auto-inicio al encender la Raspberry Pi

Si deseas que la aplicación web inicie sola cada vez que prendas la Raspberry Pi:

```bash
sudo cp guitar-synth.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable guitar-synth.service
sudo systemctl start guitar-synth.service
```

Para verificar su estado:
```bash
sudo systemctl status guitar-synth.service
```
