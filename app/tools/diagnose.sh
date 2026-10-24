#!/usr/bin/env bash
# Diagnóstico de audio para la Raspberry Pi: sólo LEE información, no cambia nada.
#
# Sirve para encontrar por qué hay cortes o ruidos ("mal contacto") al sonar el
# synth: poca CPU, frecuencia de la CPU bloqueada a la baja, subtensión, calor,
# o una configuración de JACK demasiado ajustada.
#
# Uso (con la app funcionando y tocando, para ver la carga real):
#     bash app/tools/diagnose.sh
# y pega la salida completa.

section() { printf '\n=== %s ===\n' "$1"; }

section "Sistema"
tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
uname -mr
grep -m1 PRETTY_NAME /etc/os-release

section "Alimentación y temperatura (vcgencmd)"
if command -v vcgencmd >/dev/null 2>&1; then
    vcgencmd measure_temp
    throttled="$(vcgencmd get_throttled | cut -d= -f2)"
    echo "get_throttled = ${throttled}   (0x0 es lo correcto)"
    value=$((throttled))
    (( value & 0x1 ))     && echo "  ⚠ SUBTENSIÓN ahora mismo: la fuente de alimentación no da la talla"
    (( value & 0x2 ))     && echo "  ⚠ Frecuencia de la CPU limitada ahora"
    (( value & 0x4 ))     && echo "  ⚠ CPU estrangulada (throttling) ahora"
    (( value & 0x8 ))     && echo "  ⚠ Límite de temperatura alcanzado ahora"
    (( value & 0x10000 )) && echo "  ⚠ Ha habido subtensión desde el arranque"
    (( value & 0x20000 )) && echo "  ⚠ Ha habido limitación de frecuencia desde el arranque"
    (( value & 0x40000 )) && echo "  ⚠ Ha habido estrangulamiento desde el arranque"
    (( value & 0x80000 )) && echo "  ⚠ Ha habido límite de temperatura desde el arranque"
else
    echo "vcgencmd no disponible"
fi

section "Frecuencia de la CPU"
for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
    gov="$(cat "$cpu/cpufreq/scaling_governor" 2>/dev/null)"
    cur="$(cat "$cpu/cpufreq/scaling_cur_freq" 2>/dev/null)"
    max="$(cat "$cpu/cpufreq/cpuinfo_max_freq" 2>/dev/null)"
    [ -n "$gov" ] && echo "$(basename "$cpu"): gobernador=${gov} actual=$((cur / 1000)) MHz máximo=$((max / 1000)) MHz"
done
echo "(para audio lo recomendable es el gobernador 'performance')"

section "Carga"
uptime
free -h | head -2
echo
echo "Procesos que más CPU gastan:"
ps -eo pcpu,pmem,comm,args --sort=-pcpu --no-headers | head -12 | cut -c1-140

section "JACK"
ps -eo args | grep -E '[j]ackd|[j]ackdbus' | cut -c1-200
command -v jack_samplerate >/dev/null && echo "frecuencia: $(jack_samplerate 2>&1)"
command -v jack_bufsize >/dev/null && echo "buffer: $(jack_bufsize 2>&1)"
systemctl cat jack 2>/dev/null | grep -E '^(ExecStart|Environment)' | cut -c1-200
echo
echo "(a 96 kHz con buffer 128 cada ciclo dura 1.3 ms; a 48 kHz con 256, 5.3 ms)"

section "Servicios de audio activos"
systemctl list-units --type=service --state=running --no-legend 2>/dev/null \
    | grep -i -E 'jack|modep|mod-|pulse|pisound|fluid' | awk '{print $1}'

section "Programas"
command -v fluidsynth >/dev/null && fluidsynth --version | head -1
command -v sfizz_jack >/dev/null && echo "sfizz_jack: $(command -v sfizz_jack)" || echo "sfizz_jack: no instalado"

section "Mensajes recientes del kernel sobre audio, USB o alimentación"
dmesg 2>/dev/null | grep -i -E 'under-?voltage|throttl|xrun|usb.*(reset|disconnect)|snd|pisound' | tail -8
echo
echo "Fin del diagnóstico."
