"""
Ruteo de la salida en JACK: sólo synth, sólo guitarra o ambos.

La guitarra puede llegar a la salida por dos caminos:
  * MODEP activo: la guitarra pasa por el pedalboard (mod-monitor) y sus
    salidas van a system:playback. La app conecta/desconecta mod-monitor.
  * MODEP inactivo: la app conecta directamente la entrada de captura a
    las dos salidas (guitarra limpia, mono en ambos lados).

La guitarra nunca pasa por Python: son conexiones directas de JACK, sin
latencia añadida y sin riesgo de cortes.
"""

import logging

import jack

log = logging.getLogger("router")

PLAYBACK = ["system:playback_1", "system:playback_2"]
MODEP_OUTPUTS = ["mod-monitor:out_1", "mod-monitor:out_2"]


class OutputRouter:
    def __init__(self, client, capture_port):
        self.client = client
        self.capture_port = capture_port
        self.synth_ports = []
        self.modep = self.detect_modep()
        self._original = self._guitar_state()

    def detect_modep(self):
        names = {p.name for p in self.client.get_ports(is_audio=True, is_output=True)}
        return all(name in names for name in MODEP_OUTPUTS)

    def _guitar_pairs(self):
        if self.modep:
            return list(zip(MODEP_OUTPUTS, PLAYBACK))
        return [(self.capture_port, dst) for dst in PLAYBACK]

    def _synth_pairs(self):
        return list(zip(self.synth_ports, PLAYBACK))

    def _guitar_state(self):
        return {pair: self._connected(*pair) for pair in self._guitar_pairs()}

    def _connected(self, src, dst):
        try:
            return any(p.name == dst for p in self.client.get_all_connections(src))
        except jack.JackError:
            return False

    def _set(self, src, dst, wanted):
        try:
            connected = self._connected(src, dst)
            if wanted and not connected:
                self.client.connect(src, dst)
            elif not wanted and connected:
                self.client.disconnect(src, dst)
        except jack.JackError as exc:
            log.warning("No se pudo %s %s -> %s: %s",
                        "conectar" if wanted else "desconectar", src, dst, exc)

    def apply(self, output):
        """output: 'synth' | 'guitar' | 'both'."""
        modep = self.detect_modep()
        if modep != self.modep:
            log.info("MODEP %s", "detectado" if modep else "ya no está activo")
            # Dejar el camino anterior como estaba antes de cambiar
            for pair, was in self._original.items():
                self._set(*pair, was)
            self.modep = modep
            self._original = self._guitar_state()

        for src, dst in self._guitar_pairs():
            self._set(src, dst, output in ("guitar", "both"))
        for src, dst in self._synth_pairs():
            self._set(src, dst, output in ("synth", "both"))

    def restore(self):
        """Devuelve la guitarra a como estaba al arrancar y desconecta el synth."""
        for src, dst in self._synth_pairs():
            self._set(src, dst, False)
        for pair, was in self._original.items():
            self._set(*pair, was)
