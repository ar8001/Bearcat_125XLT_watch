#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ubc125xlt_closecall.py
=======================

Terminal-Anzeige (curses) für den Uniden Bearcat UBC-125XLT (baugleich BC125AT /
UBC126AT), getestet für Raspberry Pi 5 / Raspberry Pi OS "trixie" (64-Bit).

Funktion
--------
- Prüft per USB, ob der Scanner angeschlossen ist (Suche über USB-Vendor-ID
  0x1965 = Uniden, sowie Bestätigung über den seriellen "MDL"-Befehl).
- Wenn verbunden: schaltet den Scanner sofort in den echten "Close Call Only"-
  Betrieb (CLC-Befehl, CC_MODE=3, alle 5 Bänder aktiv). Fallback auf CC_MODE=2
  (DND), falls die Firmware den Wert 3 nicht kennt.
- Statuszeile oben: Datum, Uhrzeit, Verbindungsstatus, aktueller Betriebsmodus,
  ggf. "LOG ACTIVE" (rot).
- Zweite Zeile: die 5 Close-Call-Bänder, aktive Bänder ROT, inaktive GRAU.
  Mit den Zifferntasten 1-5 lassen sich einzelne Bänder live umschalten.
- Sobald der Scanner ein Signal empfängt UND dieses Signal ununterbrochen
  länger als 2 Sekunden anliegt (Entprellung gegen kurze/falsche Anzeigen),
  erscheint darunter eine Zeile mit letzter Empfangszeit, Frequenz,
  Modulation, Speichernummer, Dauer und Anzahl. Dies gilt einheitlich in
  allen Betriebsmodi.
- Pro Frequenz gibt es immer genau eine Zeile auf dem Bildschirm: Wird
  dieselbe Frequenz erneut empfangen, wird die bestehende Zeile aktualisiert
  (Dauer läuft live mit, Zähler "Anzahl" wird erhöht) statt eine neue Zeile
  anzuhängen. Die zuletzt aktive Frequenz steht ganz oben in der Liste.
- Taste L schaltet die Log-Datei (CSV) ein/aus. Die CSV enthält ebenfalls
  eine Zeile je Frequenz mit erster/letzter Erkennung, Dauer, Gesamtdauer
  und Anzahl - sie wird bei jedem neuen Treffer bzw. Ende eines Empfangs
  aktualisiert (nicht bei jeder einzelnen Abfrage).
- Taste C schaltet in den Close-Call-Only-Modus (CC_MODE=3, Standard).
- Taste S schaltet in den Suche-Modus (CC_MODE=1, Close Call als Priorität
  neben Scan/Suche - eine echte Suchlauf-Sequenz muss ggf. weiterhin einmalig
  über die Srch/Svc-Taste am Gerät gestartet werden, da das Uniden-Protokoll
  keinen Fernbefehl zum Simulieren von Tastendrücken kennt).
- Taste A schaltet in den Scan-Modus (CC_MODE=0, Close Call aus) - ein
  laufender Scan muss ggf. ebenfalls einmalig über die Scan-Taste am Gerät
  gestartet werden (gleiche Einschränkung wie oben).
- Taste Q beendet das Programm.

Voraussetzungen
----------------
    sudo apt install python3-serial
    # oder, falls nicht per apt verfügbar:
    pip3 install pyserial --break-system-packages

    # Der Benutzer braucht Zugriff auf die serielle Schnittstelle:
    sudo usermod -aG dialout $USER
    # danach einmal ab- und wieder anmelden (bzw. neu einloggen per SSH)

Hinweis zur Baudrate
---------------------
Der UBC-125XLT unterstützt laut Uniden-Protokoll folgende Baudraten:
4800 / 9600 / 19200 / 38400 / 57600 / 115200 Bit/s. Werksseitig ist üblicherweise
9600 Baud eingestellt. Falls du die Baudrate am Scanner (Menü) geändert hast,
passe die Konstante BAUD unten entsprechend an.

Hinweis zum GLG-Befehl
-----------------------
Der Befehl "GLG" (Empfangsstatus laufend abfragen) ist im offiziellen Uniden-
Protokoll für dieses Modell nicht dokumentiert, funktioniert aber nachweislich
(von mehreren Projekten per Sniffing verifiziert) und liefert Frequenz und
Modulation der aktuell laufenden Empfangsstelle. Sollte dein Gerät (z. B. eine
sehr alte/neue Firmware) abweichen, kannst du das im Quellcode unten leicht
anpassen (siehe Funktion parse_glg()).

Quellen (Protokoll-Referenz):
- http://info.uniden.com/twiki/pub/UnidenMan4/BC125AT/BC125AT_Protocol.pdf
- https://info.uniden.com/twiki/pub/UnidenMan4/BC125AT/BC125AT_PC_Protocol_V1.01.pdf
- https://github.com/pa3ang/ubc125xlt (GLG-Befehl, reverse engineered)
- https://github.com/fdev/bc125csv (Vendor-ID 0x1965, Modell-Erkennung, Baudrate)
"""

import curses
import csv
import locale
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit(
        "Das Python-Modul 'pyserial' wird benötigt.\n"
        "Installation:  sudo apt install python3-serial\n"
        "oder:          pip3 install pyserial --break-system-packages"
    )

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

BAUD = 9600                 # Werks-Standard des UBC-125XLT / BC125AT
SERIAL_TIMEOUT = 0.5         # Sekunden, Timeout je gelesenem Byte
RECONNECT_INTERVAL = 3.0     # Sekunden zwischen Verbindungsversuchen
POLL_INTERVAL = 0.25         # Sekunden zwischen GLG-Abfragen (Empfangsstatus)

UNIDEN_VENDOR_ID = 0x1965
SUPPORTED_MODELS = ("BC125AT", "UBC125XLT", "UBC126AT")

# Close-Call/Betriebs-Modus (CLC-Befehl, Feld CC_MODE):
#   0 = AUS       (Close Call aus -> reiner Scan/Suche-Betrieb)
#   1 = CC PRI    (Close Call als Priorität neben Scan/Suche - "Suche"-Taste)
#   2 = CC DND    (Close Call nur zwischen Durchsagen, im Hintergrund)
#   3 = CC ONLY   (echter, exklusiver "Close Call Only"-Modus - Standard beim Verbinden)
CC_MODE = 3
ALERT_BEEP = 0    # 0 = aus, 1 = an
ALERT_LIGHT = 1   # 0 = aus, 1 = an
CC_LOCKOUT = 0    # 0 = kein Lockout bei Close-Call-Treffern, 1 = Lockout

# Wie lange ein Signal auf derselben Frequenz ununterbrochen anliegen muss,
# bevor es als echter Empfang gewertet und angezeigt/geloggt wird (Sekunden).
CONFIRM_SECONDS = 2.0

MODE_LABELS = {
    0: "SCAN (Close Call aus)",
    1: "SUCHE (Close Call Prio)",
    2: "CC DND",
    3: "CLOSE CALL ONLY",
}

# Reihenfolge der 5 CC_BAND-Ziffern gemäß Uniden-Protokoll
BANDS = [
    ("VHF-LOW1", "VHF LOW1"),
    ("AIR", "AIR BAND"),
    ("VHF-HIGH1", "VHF HIGH1"),
    ("VHF-HIGH2", "VHF HIGH2"),
    ("UHF", "UHF"),
]

MAX_FREQ_ROWS = 1000        # Obergrenze an gleichzeitig verfolgten Frequenzen
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


# --------------------------------------------------------------------------- #
# Serielle Kommunikation mit dem Scanner
# --------------------------------------------------------------------------- #

class ScannerError(Exception):
    pass


def find_candidate_ports():
    """Liefert eine Liste möglicher serieller Ports für den Scanner.

    Zuerst werden Ports mit Uniden-USB-Vendor-ID (0x1965) bevorzugt, danach
    als Fallback alle USB-seriellen Ports (ttyUSB*/ttyACM*)."""
    ports = list(list_ports.comports())
    uniden = [p for p in ports if p.vid == UNIDEN_VENDOR_ID]
    if uniden:
        return [p.device for p in uniden]
    fallback = [
        p.device for p in ports
        if os.path.basename(p.device).startswith(("ttyUSB", "ttyACM"))
    ]
    return fallback


def read_response(ser):
    """Liest eine Antwort bis zum Carriage-Return (\\r), wie vom Uniden-
    Protokoll vorgegeben ('Return Code: Carriage Return only')."""
    buf = bytearray()
    while True:
        b = ser.read(1)
        if not b:
            break  # Timeout - keine weiteren Daten
        if b in (b"\r", b"\n"):
            if buf:
                break
            continue  # führende CR/LF überspringen
        buf += b
    return buf.decode("ascii", errors="replace").strip()


def send_command(ser, cmd):
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    ser.flush()
    return read_response(ser)


def apply_close_call_settings(ser, band_state, cc_mode):
    """Wechselt in den Programmiermodus, setzt die Close-Call-/Betriebs-
    konfiguration und verlässt den Programmiermodus wieder (Scanner geht
    danach automatisch in den entsprechenden Modus über).

    Gibt den tatsächlich gesetzten CC_MODE zurück (kann von cc_mode
    abweichen, falls die Firmware den Wert 3 "CC ONLY" nicht unterstützt
    und auf 2 "CC DND" zurückgefallen werden musste)."""
    resp = send_command(ser, "PRG")
    if resp != "PRG,OK":
        raise ScannerError("Konnte Programmiermodus nicht aktivieren (%r)" % resp)

    cc_band = "".join("1" if v else "0" for v in band_state)
    effective_mode = cc_mode
    cmd = "CLC,%d,%d,%d,%s,%d" % (effective_mode, ALERT_BEEP, ALERT_LIGHT, cc_band, CC_LOCKOUT)
    resp = send_command(ser, cmd)
    if resp != "CLC,OK" and cc_mode == 3:
        # Manche älteren Firmware-/Protokollrevisionen kennen "CC ONLY" (3)
        # nicht - Fallback auf CC DND (2), was funktional am nächsten kommt.
        effective_mode = 2
        cmd = "CLC,%d,%d,%d,%s,%d" % (effective_mode, ALERT_BEEP, ALERT_LIGHT, cc_band, CC_LOCKOUT)
        resp = send_command(ser, cmd)
    if resp != "CLC,OK":
        # Programmiermodus in jedem Fall wieder verlassen, bevor der Fehler
        # weitergegeben wird
        send_command(ser, "EPG")
        raise ScannerError("Konnte Betriebsmodus nicht setzen (%r)" % resp)

    resp = send_command(ser, "EPG")
    if resp != "EPG,OK":
        raise ScannerError("Konnte Programmiermodus nicht verlassen (%r)" % resp)

    return effective_mode


def connect(band_state, cc_mode):
    """Sucht den Scanner, verbindet sich und aktiviert den gewünschten Modus.

    Gibt (serial.Serial, modellname, effektiver_cc_mode) zurück oder
    (None, None, None) falls nichts gefunden wurde. Wirft ScannerError bei
    einem Verbindungsproblem trotz gefundenem Port (z. B. falsche Baudrate)."""
    for port in find_candidate_ports():
        try:
            ser = serial.Serial(port, BAUD, timeout=SERIAL_TIMEOUT)
        except (serial.SerialException, OSError):
            continue
        try:
            model_resp = send_command(ser, "MDL")
            if not model_resp.startswith("MDL,"):
                ser.close()
                continue
            model = model_resp[4:].strip()
            effective_mode = apply_close_call_settings(ser, band_state, cc_mode)
            return ser, model, effective_mode
        except ScannerError:
            ser.close()
            continue
        except (serial.SerialException, OSError):
            ser.close()
            continue
    return None, None, None


def parse_glg(response):
    """Parst die (undokumentierte) GLG-Antwort und liefert
    (freq_mhz, mod, kanal_nr) zurück, oder (None, None, None) wenn aktuell
    kein Signal anliegt."""
    if not response.startswith("GLG"):
        return None, None, None
    parts = response.split(",")
    if len(parts) < 3:
        return None, None, None
    freq_raw = parts[1].strip()
    mod = parts[2].strip()
    if not freq_raw or not freq_raw.isdigit():
        return None, None, None
    value = int(freq_raw)
    if value == 0:
        return None, None, None
    # Bestätigt durch Praxistest und das offizielle CIN-Befehlsbeispiel
    # ([FRQ]=290000 -> 29.0000 MHz): Der Rohwert liegt in 100-Hz-Schritten
    # vor, NICHT in kHz. (Die kHz-Annahme führte zu einem Faktor-10-Fehler,
    # z. B. 127.2750 MHz wurde fälschlich als 1272.7500 angezeigt.)
    freq_mhz = value / 10000.0
    # Plausibilitätsprüfung: gültiger Scanner-Empfangsbereich ca. 20-1400 MHz.
    # Falls das Ergebnis unrealistisch ist, alternative Skalierung (kHz)
    # als Fallback versuchen (falls eine Firmware-Variante doch kHz liefert).
    if not (20.0 <= freq_mhz <= 1400.0):
        alt = value / 1000.0
        if 20.0 <= alt <= 1400.0:
            freq_mhz = alt
        else:
            return None, None, None
    # Kanal-/Speichernummer (nur bei Scan/Search meist gefüllt, bei reinem
    # Close Call üblicherweise leer) - laut Reverse Engineering Feld 11.
    chan_num = parts[11].strip() if len(parts) > 11 else ""
    return freq_mhz, mod, chan_num


# --------------------------------------------------------------------------- #
# Curses-Oberfläche
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.ser = None
        self.model = None
        self.connected = False
        self.last_reconnect_attempt = 0.0
        self.last_poll = 0.0
        self.band_state = [True] * len(BANDS)
        self.cc_mode = CC_MODE
        # Ein Eintrag je Frequenz (Schlüssel = gerundete Frequenz in MHz).
        # Reihenfolge = zuletzt aktualisiert steht am Ende (siehe move_to_end
        # in _get_stat); für die Anzeige wird die Liste umgedreht, damit die
        # zuletzt aktive Frequenz oben steht.
        self.freq_stats = OrderedDict()
        self.active_freq = None      # Frequenz, die gerade bestätigt aktiv ist
        self.active_start = None     # Zeitpunkt (time.time()), an dem der aktuelle Empfang begann
        self.pending_freq = None
        self.pending_mod = None
        self.pending_chan = ""
        self.pending_since = None
        self.log_active = False
        self.log_file = None
        self.log_path = None
        self.status_msg = "Suche Scanner..."

        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)
        self._init_colors()

    def _init_colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_RED, bg)      # aktiv / Log aktiv / Fehler
        curses.init_pair(2, curses.COLOR_GREEN, bg)    # verbunden
        curses.init_pair(3, curses.COLOR_WHITE, bg)    # normaler Text
        curses.init_pair(4, curses.COLOR_CYAN, bg)     # Überschriften
        curses.init_pair(5, curses.COLOR_YELLOW, bg)   # Hinweise
        self.RED = curses.color_pair(1) | curses.A_BOLD
        self.GREEN = curses.color_pair(2) | curses.A_BOLD
        self.WHITE = curses.color_pair(3)
        self.CYAN = curses.color_pair(4) | curses.A_BOLD
        self.YELLOW = curses.color_pair(5)
        self.GRAY = curses.color_pair(3) | curses.A_DIM

    # ----------------------------------------------------------------- #
    # Verbindung / Empfang
    # ----------------------------------------------------------------- #

    def try_connect(self):
        now = time.time()
        if now - self.last_reconnect_attempt < RECONNECT_INTERVAL:
            return
        self.last_reconnect_attempt = now
        self.status_msg = "Suche Scanner..."
        try:
            ser, model, effective_mode = connect(self.band_state, self.cc_mode)
        except ScannerError as exc:
            ser, model, effective_mode = None, None, None
            self.status_msg = "Fehler: %s" % exc
        if ser:
            self.ser = ser
            self.model = model
            self.cc_mode = effective_mode
            self.connected = True
            self.status_msg = ""
        else:
            self.connected = False

    def disconnect(self, reason=""):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.connected = False
        # Laufenden Empfang sauber abschließen (Dauer/Gesamtdauer sichern),
        # bevor der Zustand zurückgesetzt wird. Die Frequenz-Statistik
        # (freq_stats) bleibt über einen Verbindungsabbruch hinweg erhalten.
        self._finalize_active(time.time())
        self.pending_freq = None
        self.pending_mod = None
        self.pending_chan = ""
        self.pending_since = None
        if reason:
            self.status_msg = reason

    def poll_signal(self):
        now = time.time()
        if now - self.last_poll < POLL_INTERVAL:
            return
        self.last_poll = now
        try:
            resp = send_command(self.ser, "GLG")
        except (serial.SerialException, OSError):
            self.disconnect("Verbindung verloren")
            return
        freq, mod, chan_num = parse_glg(resp)

        if freq is None:
            # Kein Empfang mehr - laufenden Treffer abschließen (Dauer
            # sichern) und Kandidatenstatus zurücksetzen.
            self._finalize_active(now)
            self.pending_freq = None
            self.pending_since = None
            return

        if freq != self.pending_freq:
            # Neue Kandidaten-Frequenz - falls vorher eine andere Frequenz
            # aktiv war, deren Empfang zuerst sauber abschließen.
            self._finalize_active(now)
            self.pending_freq = freq
            self.pending_mod = mod
            self.pending_chan = chan_num
            self.pending_since = now
            return

        # Gleiche Kandidaten-Frequenz wie beim letzten Poll.
        if chan_num:
            self.pending_chan = chan_num

        if self.active_freq == freq:
            # Bereits bestätigt und weiterhin aktiv - Zeile live aktualisieren
            # (Dauer läuft mit), aber kein neuer Zähler-Eintrag.
            self._touch_active(now)
            return

        # Erst nach CONFIRM_SECONDS ununterbrochenem Empfang als echten
        # Treffer werten (Entprellung gegen kurze/falsche Anzeigen).
        if now - self.pending_since >= CONFIRM_SECONDS:
            self.active_freq = freq
            self.active_start = self.pending_since
            self._confirm_hit(freq, mod, self.pending_chan, now)

    # ------------------------------------------------------------- #
    # Frequenz-Statistik (eine Zeile pro Frequenz, mit Dauer/Anzahl)
    # ------------------------------------------------------------- #

    def _get_stat(self, freq):
        """Liefert (und erstellt bei Bedarf) den Statistik-Eintrag für
        `freq` und markiert ihn als zuletzt benutzt (für Anzeige-
        Reihenfolge und Verdrängung ältester Einträge)."""
        key = round(freq, 4)
        stat = self.freq_stats.get(key)
        if stat is None:
            stat = {
                "freq": freq,
                "mod": "",
                "chan": "",
                "first_seen": datetime.now(),
                "last_seen": datetime.now(),
                "count": 0,
                "duration": 0.0,
                "total_duration": 0.0,
                "active": False,
            }
            self.freq_stats[key] = stat
            if len(self.freq_stats) > MAX_FREQ_ROWS:
                self.freq_stats.popitem(last=False)
        else:
            self.freq_stats.move_to_end(key)
        return stat

    def _confirm_hit(self, freq, mod, chan_num, now_ts):
        """Wird genau einmal pro neu bestätigtem Empfang aufgerufen
        (nach Ablauf von CONFIRM_SECONDS) - erhöht den Zähler."""
        stat = self._get_stat(freq)
        stat["mod"] = mod
        if chan_num:
            stat["chan"] = chan_num
        stat["last_seen"] = datetime.now()
        stat["count"] += 1
        stat["active"] = True
        stat["duration"] = 0.0
        self._write_log_snapshot()

    def _touch_active(self, now_ts):
        """Aktualisiert die laufende Dauer eines weiterhin aktiven
        Empfangs (ohne den Zähler zu erhöhen oder den Log zu schreiben)."""
        if self.active_freq is None:
            return
        stat = self._get_stat(self.active_freq)
        stat["duration"] = now_ts - self.active_start
        stat["last_seen"] = datetime.now()

    def _finalize_active(self, now_ts):
        """Schließt einen laufenden Empfang ab, sichert die Dauer in der
        Gesamtdauer und schreibt den Log-Schnappschuss."""
        if self.active_freq is None:
            return
        stat = self._get_stat(self.active_freq)
        duration = now_ts - self.active_start
        stat["duration"] = duration
        stat["total_duration"] += duration
        stat["active"] = False
        stat["last_seen"] = datetime.now()
        self.active_freq = None
        self.active_start = None
        self._write_log_snapshot()

    @staticmethod
    def _format_duration(seconds):
        if seconds < 0:
            seconds = 0.0
        if seconds < 60:
            return "%.1fs" % seconds
        minutes, secs = divmod(int(seconds), 60)
        return "%d:%02d" % (minutes, secs)

    def toggle_band(self, index):
        if not (0 <= index < len(self.band_state)):
            return
        old_state = list(self.band_state)
        self.band_state[index] = not self.band_state[index]
        if self.connected:
            try:
                self.cc_mode = apply_close_call_settings(self.ser, self.band_state, self.cc_mode)
            except (ScannerError, serial.SerialException, OSError):
                self.band_state = old_state
                self.disconnect("Verbindung verloren")

    def set_mode(self, new_mode):
        """Wechselt den Betriebsmodus (0=Scan/CC aus, 1=Suche/CC Prio,
        3=Close Call Only) über den CLC-Befehl."""
        if not self.connected or self.cc_mode == new_mode:
            return
        old_mode = self.cc_mode
        try:
            self.cc_mode = apply_close_call_settings(self.ser, self.band_state, new_mode)
            # Modus-Wechsel wirkt sich auf neue Empfangs-Bestätigung aus -
            # laufenden Treffer sauber abschließen (Dauer sichern).
            self._finalize_active(time.time())
            self.pending_freq = None
            self.pending_since = None
        except (ScannerError, serial.SerialException, OSError):
            self.cc_mode = old_mode
            self.disconnect("Verbindung verloren")

    def toggle_log(self):
        if self.log_active:
            self.log_active = False
            if self.log_file:
                self.log_file.close()
            self.log_file = None
        else:
            os.makedirs(LOG_DIR, exist_ok=True)
            filename = "closecall_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_path = os.path.join(LOG_DIR, filename)
            # "w+": Datei wird bei jedem Schnappschuss komplett neu geschrieben
            # (siehe _write_log_snapshot) - so enthält die CSV immer genau
            # eine aktuelle Zeile je Frequenz, statt endlos anzuwachsen.
            self.log_file = open(self.log_path, "w+", newline="", encoding="utf-8")
            self.log_active = True
            self._write_log_snapshot()

    def _write_log_snapshot(self):
        """Schreibt die komplette Frequenz-Statistik in die Log-Datei neu
        (eine Zeile je Frequenz mit erster/letzter Erkennung, Dauer,
        Gesamtdauer und Anzahl). Wird bei jedem neuen Treffer bzw. beim
        Ende eines Empfangs aufgerufen - nicht bei jeder einzelnen Abfrage."""
        if not (self.log_active and self.log_file):
            return
        self.log_file.seek(0)
        self.log_file.truncate()
        writer = csv.writer(self.log_file)
        writer.writerow([
            "Datum", "Erste_Erkennung", "Letzte_Erkennung", "Frequenz_MHz",
            "Modulation", "Speichernummer", "Anzahl", "Dauer_s", "Gesamtdauer_s",
        ])
        for stat in self.freq_stats.values():
            writer.writerow([
                stat["first_seen"].strftime("%d.%m.%Y"),
                stat["first_seen"].strftime("%H:%M:%S"),
                stat["last_seen"].strftime("%H:%M:%S"),
                "%.4f" % stat["freq"],
                stat["mod"],
                stat["chan"] or "",
                stat["count"],
                "%.1f" % stat["duration"],
                "%.1f" % stat["total_duration"],
            ])
        self.log_file.flush()

    # ----------------------------------------------------------------- #
    # Zeichnen
    # ----------------------------------------------------------------- #

    def draw(self):
        stdscr = self.stdscr
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        # --- Zeile 0: Datum, Uhrzeit, Verbindungsstatus, Log-Status --- #
        now = datetime.now()
        col = 0
        col = self._put(0, col, now.strftime("%d.%m.%Y  %H:%M:%S"), self.WHITE, w)
        col = self._put(0, col, "   |   Verbindung: ", self.WHITE, w)
        if self.connected:
            col = self._put(0, col, "OK (%s)" % (self.model or "?"), self.GREEN, w)
            col = self._put(0, col, "   |   Modus: ", self.WHITE, w)
            col = self._put(0, col, MODE_LABELS.get(self.cc_mode, str(self.cc_mode)), self.CYAN, w)
        else:
            label = self.status_msg or "getrennt"
            col = self._put(0, col, label, self.RED, w)
        if self.log_active:
            col = self._put(0, col, "   |   ", self.WHITE, w)
            col = self._put(0, col, "LOG ACTIVE", self.RED, w)

        # --- Zeile 1: Close-Call-Bänder --- #
        col = 0
        col = self._put(1, col, "Close-Call-Bereiche: ", self.CYAN, w)
        for i, (short, _long) in enumerate(BANDS):
            active = self.band_state[i]
            attr = self.RED if active else self.GRAY
            label = "[%d]%s" % (i + 1, short)
            col = self._put(1, col, label, attr, w)
            col = self._put(1, col, "  ", self.WHITE, w)

        # --- Zeile 2: Trennlinie / Überschrift --- #
        self._put(2, 0, "-" * max(0, w - 1), self.WHITE, w)
        header_line = "%-8s   %12s   %-5s   %-9s   %-8s   %s" % (
            "Zuletzt", "Frequenz(MHz)", "Mod.", "Speicher", "Dauer", "Anzahl",
        )
        self._put(3, 0, header_line, self.CYAN, w)

        # --- Frequenz-Tabelle: genau eine Zeile pro Frequenz --- #
        # Reihenfolge: zuletzt aktive/aktualisierte Frequenz zuerst.
        header_rows = 4
        footer_rows = 1
        visible_rows = max(0, h - header_rows - footer_rows)
        now_ts = time.time()
        stats = list(reversed(self.freq_stats.values())) if visible_rows else []
        for i, stat in enumerate(stats[:visible_rows]):
            is_active = stat["active"] and round(self.active_freq or -1, 4) == round(stat["freq"], 4)
            duration = (now_ts - self.active_start) if is_active else stat["duration"]
            line = "%-8s   %12.4f   %-5s   %-9s   %-8s   x%d" % (
                stat["last_seen"].strftime("%H:%M:%S"),
                stat["freq"],
                stat["mod"],
                stat["chan"] or "-",
                self._format_duration(duration),
                stat["count"],
            )
            attr = self.RED if is_active else self.WHITE
            self._put(header_rows + i, 0, line, attr, w)

        # --- Fußzeile: Tastenhilfe --- #
        help_line = (
            "[L] Log an/aus   [1-5] Band um   "
            "[C] Close Call   [S] Suche   [A] Scan   [Q] Beenden"
        )
        if self.log_active and self.log_path:
            help_line += "   Datei: %s" % os.path.basename(self.log_path)
        self._put(h - 1, 0, help_line, self.YELLOW, w)

        stdscr.refresh()

    def _put(self, y, x, text, attr, max_w):
        """Schreibt Text an Position (y, x), auf Bildschirmbreite begrenzt.
        Gibt die neue X-Position zurück."""
        if y < 0 or x >= max_w - 1:
            return x
        avail = max_w - 1 - x
        if avail <= 0:
            return x
        text = text[:avail]
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass
        return x + len(text)

    # ----------------------------------------------------------------- #
    # Hauptschleife
    # ----------------------------------------------------------------- #

    def run(self):
        while True:
            if not self.connected:
                self.try_connect()
            else:
                self.poll_signal()

            self.draw()

            try:
                key = self.stdscr.getch()
            except curses.error:
                key = -1

            if key == -1:
                continue
            ch = chr(key) if 0 <= key < 256 else ""
            if ch in ("q", "Q"):
                break
            elif ch in ("l", "L"):
                self.toggle_log()
            elif ch in ("1", "2", "3", "4", "5"):
                self.toggle_band(int(ch) - 1)
            elif ch in ("c", "C"):
                self.set_mode(3)
            elif ch in ("s", "S"):
                self.set_mode(1)
            elif ch in ("a", "A"):
                self.set_mode(0)

        if self.log_file:
            self.log_file.close()
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


def main(stdscr):
    app = App(stdscr)
    app.run()


if __name__ == "__main__":
    # UTF-8-Locale setzen, damit curses Sonderzeichen (falls vorhanden)
    # korrekt darstellt - schadet auf reinen ASCII-Terminals nicht.
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(main)
