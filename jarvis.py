#!/usr/bin/env python3
"""
J.A.R.V.I.S. - assistente vocale per macOS e Windows con controllo del sistema.

Il modello NON esegue comandi arbitrari: puo' solo invocare gli strumenti
dichiarati in TOOLS_LOCALI, e lettura e scrittura file sono confinate a una sandbox.

Requisiti macOS:
    brew install portaudio
    pip install anthropic SpeechRecognition pyaudio edge-tts miniaudio psutil
    export ANTHROPIC_API_KEY="sk-ant-..."

Requisiti Windows (PowerShell):
    pip install anthropic SpeechRecognition pyaudio edge-tts miniaudio psutil
    setx ANTHROPIC_API_KEY "sk-ant-..."     (poi riaprire il terminale)

Avvio:
    python3 jarvis.py        (macOS)
    py jarvis.py             (Windows, con finestra del terminale)
    pyw jarvis.py            (Windows, senza finestre: i messaggi vanno in ~/Jarvis/jarvis.log)
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import queue
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# ==========================================================================
# REGISTRO (avvio senza finestra)
# Con pythonw non esiste una console: senza questo, ogni messaggio ed errore
# andrebbe perso. Deve stare prima degli import che possono fallire.
# ==========================================================================

CARTELLA_JARVIS = Path.home() / "Jarvis"
FILE_REGISTRO = CARTELLA_JARVIS / "jarvis.log"
SENZA_CONSOLE = sys.stdout is None or sys.stderr is None

if SENZA_CONSOLE:
    CARTELLA_JARVIS.mkdir(parents=True, exist_ok=True)
    if FILE_REGISTRO.exists() and FILE_REGISTRO.stat().st_size > 1_000_000:
        FILE_REGISTRO.unlink()   # niente crescita infinita: si riparte da zero oltre 1 MB
    sys.stdout = sys.stderr = open(FILE_REGISTRO, "a", encoding="utf-8", buffering=1)
    print(f"\n===== Avvio {datetime.now():%d/%m/%Y %H:%M:%S} =====")

try:
    import anthropic
except ImportError:
    sys.exit("Manca il pacchetto 'anthropic'. Esegui: pip install anthropic")

try:
    import speech_recognition as sr
except ImportError:
    sys.exit("Manca 'SpeechRecognition'. Esegui: pip install SpeechRecognition pyaudio")

import array
import math

# pyaudio serve gia' al microfono; qui lo si usa anche per suonare voce e segnali.
try:
    import pyaudio
except ImportError:
    pyaudio = None

# Voce neurale: facoltativa. Se manca, si usa la voce di sistema.
try:
    import asyncio
    import edge_tts
    import miniaudio
    NEURALE_INSTALLATA = pyaudio is not None
except ImportError:
    NEURALE_INSTALLATA = False

# Dati di CPU e memoria per l'HUD: facoltativi.
try:
    import psutil
except ImportError:
    psutil = None


# ==========================================================================
# CONFIGURAZIONE
# ==========================================================================

SISTEMA = platform.system()   # "Darwin" (macOS) oppure "Windows"
IS_MAC = SISTEMA == "Darwin"
IS_WIN = SISTEMA == "Windows"

MODEL = os.environ.get("JARVIS_MODEL", "claude-haiku-4-5-20251001")
VOICE = os.environ.get("JARVIS_VOICE", "Alice")                 # solo macOS
SPEECH_RATE = int(os.environ.get("JARVIS_RATE", "190"))         # macOS: parole al minuto
SPEECH_RATE_WIN = int(os.environ.get("JARVIS_RATE_WIN", "1"))   # Windows: da -10 a 10
STT_LANG = "it-IT"
# Voce neurale Microsoft (servizio online di Edge). "0" per usare solo la voce di sistema.
# Giuseppe multilingue: italiano naturale e parole inglesi pronunciate all'inglese.
# Alternative: it-IT-DiegoNeural (solo italiano), it-IT-IsabellaNeural, it-IT-ElsaNeural.
VOCE_NEURALE = os.environ.get("JARVIS_VOCE_NEURALE", "it-IT-GiuseppeMultilingualNeural")

MAX_TOKENS = 1024
MAX_SCAMBI = 6                # scambi completi tenuti in memoria
MAX_RICERCHE_WEB = 3          # tetto per richiesta: ogni ricerca costa ~0,01 $ piu' i token dei risultati
MAX_GIRI_TOOL = 6             # anti-loop sul ciclo di tool use
LISTEN_TIMEOUT = 6
FINESTRA_ASCOLTO = 8          # secondi, dopo una risposta, in cui non serve dire "Jarvis"
SUONI_ATTIVI = os.environ.get("JARVIS_SUONI", "1") != "0"   # segnali di attivazione e riposo

HUD_ATTIVO = os.environ.get("JARVIS_HUD", "1") != "0"
HUD_PORTA = int(os.environ.get("JARVIS_HUD_PORTA", "8765"))
FILE_HUD = Path(__file__).resolve().parent / "hud.html"
PHRASE_TIME_LIMIT = 15
# 0,6 s tagliava le frasi lunghe (dettatura di una mail) a chi prende fiato: 1 s.
PAUSA_FINE_FRASE = float(os.environ.get("JARVIS_PAUSA", "1.0"))
MAX_BYTE_LETTURA = 20_000     # troncamento in lettura file

WORKSPACE = Path(
    os.environ.get("JARVIS_WORKSPACE", str(Path.home() / "Jarvis" / "workspace"))
).expanduser()

NOME_MACCHINA = "MacBook" if IS_MAC else "PC Windows"

SYSTEM_PROMPT = (
    f"Sei Jarvis, l'intelligenza artificiale integrata nel {NOME_MACCHINA} di Christian, "
    "sul modello del J.A.R.V.I.S. di Iron Man. Rispondi in italiano con la calma e l'eleganza "
    "di un maggiordomo inglese: misurato, preciso, mai servile, con un'ironia asciutta e "
    "sottile che usi di rado e solo quando viene spontanea, mai per riempire. "
    "Dai del Lei a Christian. Chiamalo 'Signore' solo ogni tanto, per esempio in un saluto "
    "o in un momento solenne: nella maggior parte delle risposte non serve. "
    "Vai dritto al punto: prima l'informazione o l'esito dell'azione, poi, solo se utile, "
    "un breve commento. Usa pure i termini tecnici inglesi quando sono quelli naturali.\n\n"
    "Le tue risposte vengono lette ad alta voce da un sintetizzatore vocale. Quindi: "
    "massimo tre frasi, prosa continua, niente elenchi puntati, niente markdown, "
    "niente emoji, niente URL letti per esteso (di' 'secondo il sito X'), niente codice. "
    "Scrivi il tuo nome come Jarvis, mai con i punti.\n\n"
    f"Hai a disposizione degli strumenti per agire sul {NOME_MACCHINA}. Usali quando servono, "
    "senza chiedere conferma per azioni innocue come aprire un'app o leggere un file. "
    "Chiedi conferma a voce prima di sovrascrivere un file gia' esistente.\n\n"
    "Puoi cercare sul web quando la domanda riguarda fatti attuali o che non conosci. "
    "Non cercare per cose che sai gia': ogni ricerca ha un costo. "
    "Se non conosci la risposta e non puoi cercarla, dillo in una frase invece di inventare."
)


class Spegnimento(Exception):
    """Sollevata quando l'utente chiede di terminare la sessione."""


# ==========================================================================
# POWERSHELL (solo Windows)
# Gli script sono costanti: i dati variabili passano da variabili d'ambiente,
# mai interpolati nel testo dello script.
# ==========================================================================

PS_VOCE_ITALIANA = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'it-IT' } "
    "| Select-Object -First 1; "
    "if ($v) { $v.VoiceInfo.Name }"
)

PS_PARLA = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "if ($env:JARVIS_VOCE_WIN) { $s.SelectVoice($env:JARVIS_VOCE_WIN) }; "
    "$s.Rate = [int]$env:JARVIS_RATE_WIN; "
    "$s.Speak($env:JARVIS_TESTO)"
)

PS_BATTERIA = (
    "$b = Get-CimInstance Win32_Battery; "
    "if ($b) { \"$($b.EstimatedChargeRemaining)% (stato $($b.BatteryStatus))\" } "
    "else { 'nessuna batteria rilevata (PC fisso?)' }"
)


# Senza questo flag ogni comando lanciato da pythonw farebbe lampeggiare una finestra nera.
SENZA_FINESTRA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _powershell(script: str, env_extra: dict | None = None, timeout: int = 30):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, errors="replace", timeout=timeout, env=env,
        creationflags=SENZA_FINESTRA,
    )


# ==========================================================================
# HUD: la "faccia" di J.A.R.V.I.S. in una pagina web locale
# Il server e' SOLO IN LETTURA (nessuna richiesta puo' comandare J.A.R.V.I.S.),
# ascolta solo su 127.0.0.1 e rifiuta ogni Host diverso da quello atteso,
# cosi' un sito aperto nel browser non puo' leggerlo (DNS rebinding).
# ==========================================================================

# ==========================================================================
# CONSUMI: token e spesa stimata
# Con una chiave API normale il saldo reale della Console non e' leggibile:
# l'utente indica il credito (--credito), J.A.R.V.I.S. somma il costo stimato
# di ogni richiesta e lo sottrae. Il dato vero resta quello della Console.
# ==========================================================================

# Dollari per milione di token (input, output). Fonte: listino Anthropic.
PREZZI_MODELLI = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}
PREZZO_RICERCA_WEB = 0.01   # dollari per ricerca
FILE_CONSUMI = CARTELLA_JARVIS / "consumi.json"


def prezzi_del_modello(modello: str) -> tuple[float, float] | None:
    return PREZZI_MODELLI.get(re.sub(r"-\d{8}$", "", modello))


class Contatore:
    CAMPI = ("token_input", "token_output", "ricerche_web", "richieste", "spesa")

    def __init__(self, percorso: Path) -> None:
        self._lock = threading.Lock()
        self.percorso = percorso
        self.sessione = dict.fromkeys(self.CAMPI, 0)
        self.totale = dict.fromkeys(self.CAMPI, 0)   # dall'ultima impostazione del credito
        self.credito: float | None = None
        self._carica()

    def _carica(self) -> None:
        try:
            dati = json.loads(self.percorso.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(dati, dict):
            self.credito = dati.get("credito")
            for campo in self.CAMPI:
                self.totale[campo] = dati.get(campo, 0)

    def _salva(self) -> None:
        self.percorso.parent.mkdir(parents=True, exist_ok=True)
        provvisorio = self.percorso.with_suffix(".tmp")
        provvisorio.write_text(json.dumps({"credito": self.credito, **self.totale}), encoding="utf-8")
        provvisorio.replace(self.percorso)

    def imposta_credito(self, dollari: float) -> None:
        """Riparte da zero: da qui in poi il residuo e' dollari meno la spesa stimata."""
        with self._lock:
            self.credito = dollari
            self.totale = dict.fromkeys(self.CAMPI, 0)
            self._salva()

    def registra(self, usage) -> None:
        entrata = sum(
            getattr(usage, campo, 0) or 0
            for campo in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        uscita = getattr(usage, "output_tokens", 0) or 0
        ricerche = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        if not (entrata or uscita or ricerche):
            return
        prezzi = prezzi_del_modello(MODEL)
        costo = 0.0
        if prezzi:
            costo = entrata * prezzi[0] / 1e6 + uscita * prezzi[1] / 1e6
        costo += ricerche * PREZZO_RICERCA_WEB
        with self._lock:
            for somma in (self.sessione, self.totale):
                somma["token_input"] += entrata
                somma["token_output"] += uscita
                somma["ricerche_web"] += ricerche
                somma["richieste"] += 1
                somma["spesa"] += costo
            try:
                self._salva()
            except OSError as e:
                print(f"[Consumi non salvati: {e}]")

    def riepilogo(self) -> dict:
        with self._lock:
            residuo = None if self.credito is None else self.credito - self.totale["spesa"]
            return {
                "sessione": dict(self.sessione),
                "totale": dict(self.totale),
                "credito": self.credito,
                "residuo": residuo,
                "prezzi_noti": prezzi_del_modello(MODEL) is not None,
            }


CONSUMI = Contatore(FILE_CONSUMI)


class Hud:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stato = "avvio"
        self.dettaglio = ""
        self.storico: deque = deque(maxlen=12)
        self.versione = 0
        self.ultimo_contatto = 0.0
        self.sistema: dict = {}
        self.voce: dict = {"id": 0, "inizio": 0, "passo": 50, "livelli": []}

    def imposta_sistema(self, dati: dict) -> None:
        with self._lock:
            self.sistema = dati

    def imposta_voce(self, livelli: list[float], inizio_ms: float, passo_ms: int) -> None:
        """Volume della voce nel tempo: la pagina anima il reattore seguendolo."""
        with self._lock:
            self.voce = {
                "id": self.voce["id"] + 1,
                "inizio": inizio_ms,
                "passo": passo_ms,
                "livelli": livelli,
            }

    def imposta(self, stato: str, dettaglio: str = "") -> None:
        with self._lock:
            self.stato, self.dettaglio = stato, dettaglio

    def aggiungi(self, chi: str, testo: str) -> None:
        with self._lock:
            self.storico.append({"chi": chi, "testo": testo, "ora": f"{datetime.now():%H:%M}"})
            self.versione += 1

    def istantanea(self) -> dict:
        with self._lock:
            self.ultimo_contatto = time.monotonic()
            return {
                "stato": self.stato,
                "dettaglio": self.dettaglio,
                "storico": list(self.storico),
                "versione": self.versione,
                "consumi": CONSUMI.riepilogo(),
                "sistema": self.sistema,
                "voce": self.voce,
                "claude_code": LAVORO.stato(),
            }

    def pagina_aperta(self) -> bool:
        with self._lock:
            return time.monotonic() - self.ultimo_contatto < 2.0

    def segna_apertura(self) -> None:
        """Lascia alla pagina appena aperta qualche secondo per collegarsi, senza riaprirla."""
        with self._lock:
            self.ultimo_contatto = time.monotonic() + 5.0


HUD = Hud()

# Cronometro di un turno (secondi): per vedere dove si perde tempo prima di ottimizzare.
TEMPI: dict = {}


def riepilogo_tempi() -> str:
    pezzi = []
    if "trascrizione" in TEMPI:
        pezzi.append(f"trascrizione {TEMPI['trascrizione']:.1f} s")
    if "claude" in TEMPI:
        chiamate = TEMPI.get("chiamate", 1)
        pezzi.append(f"Claude {TEMPI['claude']:.1f} s" + (f" ({chiamate} chiamate)" if chiamate > 1 else ""))
    if "voce" in TEMPI:
        dettaglio = TEMPI.get("voce_dettaglio") or {}
        if dettaglio.get("da_cache"):
            nota = " (dalla memoria)"
        elif "primo" in dettaglio:
            nota = (f" (primo audio da Microsoft dopo {dettaglio['primo']:.1f} s, "
                    f"frase completa {dettaglio.get('totale', 0):.1f} s)")
        else:
            nota = ""
        pezzi.append(f"voce pronta in {TEMPI['voce']:.1f} s{nota}")
    return "[Tempi: " + " · ".join(pezzi) + "]" if pezzi else ""
URL_HUD: str | None = None


def _crea_server_hud(porta: int) -> ThreadingHTTPServer:
    pagina = FILE_HUD.read_bytes()
    politica = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:"
    )

    class Gestore(BaseHTTPRequestHandler):
        def log_message(self, *argomenti) -> None:   # altrimenti una riga di registro ogni 300 ms
            pass

        def _rispondi(self, codice: int, corpo: bytes, tipo: str, extra: dict | None = None) -> None:
            self.send_response(codice)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for chiave, valore in (extra or {}).items():
                self.send_header(chiave, valore)
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self) -> None:
            porta_reale = self.server.server_address[1]
            if self.headers.get("Host") not in (f"127.0.0.1:{porta_reale}", f"localhost:{porta_reale}"):
                self._rispondi(403, b"Vietato", "text/plain; charset=utf-8")
            elif self.path == "/":
                self._rispondi(200, pagina, "text/html; charset=utf-8",
                               {"Content-Security-Policy": politica})
            elif self.path == "/stato":
                corpo = json.dumps(HUD.istantanea(), ensure_ascii=False).encode("utf-8")
                self._rispondi(200, corpo, "application/json; charset=utf-8")
            else:
                self._rispondi(404, b"Non trovato", "text/plain; charset=utf-8")

    return ThreadingHTTPServer(("127.0.0.1", porta), Gestore)


def avvia_hud() -> str | None:
    """Avvia il server dell'HUD in sottofondo. Se non riesce, J.A.R.V.I.S. funziona lo stesso."""
    if not HUD_ATTIVO:
        return None
    try:
        server = _crea_server_hud(HUD_PORTA)
    except OSError as e:
        print(f"[HUD non disponibile ({e}): continuo senza]")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"[HUD: {url}]")
    return url


def apri_hud() -> None:
    """Apre la pagina, ma solo se non e' gia' aperta: niente schede a decine."""
    if not URL_HUD or HUD.pagina_aperta():
        return
    HUD.segna_apertura()
    if IS_WIN:
        # Edge in modalita' app: una finestra pulita, senza barra degli indirizzi ne' schede.
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            edge = Path(base or "") / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if base and edge.is_file():
                subprocess.Popen([str(edge), f"--app={URL_HUD}"], creationflags=SENZA_FINESTRA)
                return
    webbrowser.open(URL_HUD)


def raccogli_sistema() -> dict:
    """Istantanea di CPU, memoria, disco e batteria per i pannelli dell'HUD."""
    dati: dict = {"cpu": None, "ram": None, "ram_usata": None, "ram_totale": None,
                  "disco": None, "disco_libero": None, "batteria": None, "in_carica": None}
    gb = 1024 ** 3
    try:
        uso = shutil.disk_usage(Path.home().anchor or "/")
        dati["disco"] = round(100 * (uso.total - uso.free) / uso.total, 1)
        dati["disco_libero"] = round(uso.free / gb)
    except OSError:
        pass
    if psutil is not None:
        dati["cpu"] = psutil.cpu_percent(interval=None)
        memoria = psutil.virtual_memory()
        dati["ram"] = memoria.percent
        dati["ram_usata"] = round((memoria.total - memoria.available) / gb, 1)
        dati["ram_totale"] = round(memoria.total / gb, 1)
        try:
            batteria = psutil.sensors_battery()
        except (AttributeError, NotImplementedError, OSError):
            batteria = None
        if batteria is not None:
            dati["batteria"] = round(batteria.percent)
            dati["in_carica"] = bool(batteria.power_plugged)
    return dati


def avvia_monitor_sistema(intervallo: float = 2.0) -> None:
    def ciclo() -> None:
        while True:
            try:
                HUD.imposta_sistema(raccogli_sistema())
            except Exception as e:   # il monitor non deve mai far cadere J.A.R.V.I.S.
                print(f"[Monitor di sistema: {type(e).__name__}: {e}]")
            time.sleep(intervallo)

    if psutil is None:
        print("[Per CPU e memoria nell'HUD: pip install psutil]")
    threading.Thread(target=ciclo, daemon=True).start()


# ==========================================================================
# SEGNALI SONORI
# Sintetizzati al volo e suonati da Python: una pagina web non puo' suonare
# finche' l'utente non ci clicca sopra, quindi non sarebbe affidabile.
# ==========================================================================

FREQUENZA_AUDIO = 24000


def sintetizza_segnale(note: list[tuple[float, float]], volume: float = 0.18) -> bytes:
    """note: (frequenza in Hz, durata in secondi). Attacco e rilascio morbidi, niente click."""
    campioni = array.array("h")
    for frequenza, durata in note:
        n = int(FREQUENZA_AUDIO * durata)
        sfuma = max(1, int(FREQUENZA_AUDIO * 0.012))
        for i in range(n):
            inviluppo = min(1.0, i / sfuma, (n - i) / sfuma)
            campione = math.sin(2 * math.pi * frequenza * i / FREQUENZA_AUDIO)
            campione += 0.25 * math.sin(4 * math.pi * frequenza * i / FREQUENZA_AUDIO)   # armonica: piu' "metallico"
            campioni.append(int(32767 * volume * inviluppo * campione / 1.25))
    return campioni.tobytes()


SEGNALI = {
    "attivo": [(880.0, 0.07), (1318.5, 0.11)],    # sale: la ascolto
    "riposo": [(1318.5, 0.07), (659.3, 0.14)],    # scende: vado in pausa
}


def suona(nome: str) -> None:
    if not SUONI_ATTIVI or pyaudio is None or nome not in SEGNALI:
        return
    uscita = None
    try:
        uscita = pyaudio.PyAudio()
        flusso = uscita.open(format=pyaudio.paInt16, channels=1, rate=FREQUENZA_AUDIO, output=True)
        flusso.write(sintetizza_segnale(SEGNALI[nome]))
        flusso.stop_stream()
        flusso.close()
    except OSError as e:
        print(f"[Segnale sonoro non riprodotto: {e}]")
    finally:
        if uscita is not None:
            uscita.terminate()


# ==========================================================================
# SINTESI VOCALE
# macOS: comando nativo 'say'. Windows: sintetizzatore di sistema via PowerShell.
# ==========================================================================

def _voce_mac_installata(nome: str) -> bool:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    return any(riga.startswith(nome + " ") for riga in out.stdout.splitlines())


def _voce_windows_italiana() -> str:
    """Nome della prima voce italiana installata su Windows, stringa vuota se assente."""
    try:
        out = _powershell(PS_VOCE_ITALIANA, timeout=20)
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


VOCE_OK = _voce_mac_installata(VOICE) if IS_MAC else False
VOCE_WIN = _voce_windows_italiana() if IS_WIN else ""


# Le voci italiane leggono "J.A.R.V.I.S." lettera per lettera e "Jarvis" come "Iarvis":
# "Giarvis" e' la grafia italiana che suona come il nome inglese.
_NOME_SCRITTO = re.compile(r"\bJ\.?A\.?R\.?V\.?I\.?S\b\.?", re.IGNORECASE)


# Parole inglesi riscritte come le pronuncerebbe un italiano: la voce italiana
# altrimenti le legge lettera per lettera all'italiana ("fi-le"). Grafie da
# ritoccare a orecchio.
PRONUNCIA = {
    "file": "fàil",
    "download": "dàunlod",
    "upload": "àplod",
    "browser": "bràuser",
    "e-mail": "imèil",
    "email": "imèil",
    "software": "sòftuer",
    "hardware": "àrduer",
    "desktop": "dèsktop",
    "online": "onlàin",
    "offline": "offlàin",
    "password": "pàssuord",
    "web": "uèb",
    "wi-fi": "uaifài",
    "wifi": "uaifài",
}
_PAROLE_INGLESI = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(p) for p in sorted(PRONUNCIA, key=len, reverse=True)) + r")(?![\w-])",
    re.IGNORECASE,
)


def per_la_voce(testo: str, multilingue: bool = False) -> str:
    """
    Voci solo italiane: nome e parole inglesi riscritti all'italiana.
    Voci multilingue: leggono le parole inglesi da sole, riscriverle le peggiorerebbe.
    Il nome invece va riscritto anche per loro: in una frase italiana leggono
    "Jarvis" come "Iarvis" (verificato a orecchio con Giuseppe).
    """
    testo = _NOME_SCRITTO.sub("Giarvis", testo)
    if multilingue:
        return testo
    return _PAROLE_INGLESI.sub(lambda m: PRONUNCIA[m.group(1).lower()], testo)


_neurale_attiva = NEURALE_INSTALLATA and VOCE_NEURALE not in ("", "0")


async def _sintetizza(testo: str, misure: dict | None = None) -> bytes:
    """misure, se dato, riceve 'primo' (secondi al primo audio) e 'totale'."""
    inizio = time.monotonic()
    audio = bytearray()
    async for pezzo in edge_tts.Communicate(testo, VOCE_NEURALE).stream():
        if pezzo["type"] == "audio":
            if misure is not None and "primo" not in misure:
                misure["primo"] = time.monotonic() - inizio
            audio += pezzo["data"]
    if misure is not None:
        misure["totale"] = time.monotonic() - inizio
    return bytes(audio)


# Memoria delle frasi brevi gia' pronunciate (su disco, per voce): "Mi dica", "Annullato",
# "Avviato su prova"... ripartono subito invece di chiedere di nuovo l'audio a Microsoft.
CARTELLA_CACHE_VOCE = CARTELLA_JARVIS / "voce_cache"
MAX_CARATTERI_CACHE = 80
MAX_FILE_CACHE = 300
FRASI_FISSE = [
    "Mi dica.", "Di nuovo operativo.", "Annullato.",
    "Modalita' riposo. Mi chiami quando serve.",
    "Disattivazione dei sistemi. A presto, Signore.",
    "Buongiorno. Tutti i sistemi sono operativi.",
    "Buon pomeriggio. Tutti i sistemi sono operativi.",
    "Buonasera. Tutti i sistemi sono operativi.",
]


def _file_cache_voce(pezzo: str) -> Path:
    import hashlib
    chiave = hashlib.sha256(f"{VOCE_NEURALE}|{pezzo}".encode("utf-8")).hexdigest()[:32]
    return CARTELLA_CACHE_VOCE / f"{chiave}.mp3"


def audio_del_pezzo(pezzo: str, misure: dict | None = None) -> bytes:
    """Audio mp3 di un pezzo: dalla memoria se c'e', altrimenti dal servizio (e poi in memoria)."""
    file = _file_cache_voce(pezzo)
    if len(pezzo) <= MAX_CARATTERI_CACHE and file.is_file():
        if misure is not None:
            misure["da_cache"] = True
        return file.read_bytes()
    mp3 = asyncio.run(asyncio.wait_for(_sintetizza(pezzo, misure), timeout=15))
    if mp3 and len(pezzo) <= MAX_CARATTERI_CACHE:
        try:
            CARTELLA_CACHE_VOCE.mkdir(parents=True, exist_ok=True)
            file.write_bytes(mp3)
        except OSError:
            pass
    return mp3


def prepara_frasi_fisse() -> None:
    """In sottofondo all'avvio: le frasi fisse sono pronte prima che servano."""
    try:
        if CARTELLA_CACHE_VOCE.is_dir():
            file = sorted(CARTELLA_CACHE_VOCE.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            for vecchio in file[:-MAX_FILE_CACHE]:
                vecchio.unlink(missing_ok=True)
        multilingue = "Multilingual" in VOCE_NEURALE
        for frase in FRASI_FISSE:
            for pezzo in dividi_frasi(per_la_voce(frase, multilingue=multilingue)):
                audio_del_pezzo(pezzo)
    except Exception as e:   # e' solo un'accelerazione: se fallisce, pazienza
        print(f"[Preparazione delle frasi fisse non riuscita: {type(e).__name__}: {e}]")


def inviluppo(campioni, frequenza: int, passo_ms: int = 50) -> list[float]:
    """Volume (0-1) della voce ogni passo_ms, relativo al picco della frase."""
    passo = max(1, frequenza * passo_ms // 1000)
    livelli = []
    for inizio in range(0, len(campioni), passo):
        blocco = campioni[inizio:inizio + passo]
        livelli.append(math.sqrt(sum(c * c for c in blocco) / len(blocco)))
    picco = max(livelli, default=0.0)
    if picco <= 0:
        return [0.0] * len(livelli)
    return [round(min(1.0, l / (0.7 * picco)), 3) for l in livelli]


def dividi_frasi(testo: str) -> list[str]:
    """
    Pezzi da sintetizzare uno alla volta, cosi' la voce parte prima: il primo corto
    (parte subito), i successivi di almeno ~120 caratteri (meno stacchi tra le frasi).
    """
    frasi = [f for f in re.split(r"(?<=[.!?;:])\s+", (testo or "").strip()) if f]
    pezzi, corrente = [], ""
    for frase in frasi:
        corrente = f"{corrente} {frase}".strip()
        if len(corrente) >= (30 if not pezzi else 120):
            pezzi.append(corrente)
            corrente = ""
    if corrente:
        if pezzi and len(corrente) < 30:
            pezzi[-1] += " " + corrente
        else:
            pezzi.append(corrente)
    return pezzi or ([testo.strip()] if testo and testo.strip() else [])


def _parla_neurale(testo: str) -> None:
    """
    Mentre un pezzo suona, il successivo si sta gia' sintetizzando in un altro thread.
    Se il primo pezzo fallisce si solleva l'errore (parla() passa alla voce di sistema);
    se fallisce dopo, il resto lo legge la voce di sistema.
    """
    pezzi = dividi_frasi(testo)
    coda: queue.Queue = queue.Queue(maxsize=2)
    inizio = time.monotonic()

    def sintetizza_in_ordine() -> None:
        for i, pezzo in enumerate(pezzi):
            try:
                misure: dict = {}
                mp3 = audio_del_pezzo(pezzo, misure)
                if i == 0:
                    TEMPI["voce_dettaglio"] = misure
                if not mp3:
                    raise RuntimeError("nessun audio ricevuto")
            except Exception as e:
                coda.put((i, None, e))
                return
            coda.put((i, mp3, None))
        coda.put((len(pezzi), None, None))

    threading.Thread(target=sintetizza_in_ordine, daemon=True).start()
    uscita = pyaudio.PyAudio()
    flusso = None
    try:
        while True:
            try:
                i, mp3, errore = coda.get(timeout=30)
            except queue.Empty:
                errore, i, mp3 = TimeoutError("sintesi troppo lenta"), -1, None
            if errore is not None:
                if flusso is None:
                    raise errore
                print(f"[Voce neurale interrotta ({errore}): continuo con la voce di sistema]")
                if i >= 0:
                    _parla_sistema(" ".join(pezzi[i:]))
                return
            if mp3 is None:
                return
            suono = miniaudio.decode(
                mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000
            )
            if flusso is None:
                flusso = uscita.open(format=pyaudio.paInt16, channels=1, rate=24000, output=True)
                TEMPI["voce"] = time.monotonic() - inizio
            ritardo_ms = 1000 * flusso.get_output_latency()
            HUD.imposta_voce(inviluppo(suono.samples, 24000), time.time() * 1000 + ritardo_ms, 50)
            flusso.write(suono.samples.tobytes())
    finally:
        if flusso is not None:
            flusso.stop_stream()
            flusso.close()
        uscita.terminate()


def _parla_sistema(testo: str) -> None:
    try:
        if IS_MAC:
            cmd = ["say", "-r", str(SPEECH_RATE)]
            if VOCE_OK:
                cmd += ["-v", VOICE]
            cmd.append(testo)
            subprocess.run(cmd, timeout=180)
        elif IS_WIN:
            esito = _powershell(
                PS_PARLA,
                env_extra={
                    "JARVIS_TESTO": testo,
                    "JARVIS_VOCE_WIN": VOCE_WIN,
                    "JARVIS_RATE_WIN": str(SPEECH_RATE_WIN),
                },
                timeout=180,
            )
            if esito.returncode != 0:
                print(f"[Sintesi vocale fallita: {esito.stderr.strip()[:300]}]")
    except FileNotFoundError:
        pass
    except subprocess.SubprocessError as e:
        print(f"[Sintesi vocale fallita: {e}]")


def parla(testo: str) -> None:
    global _neurale_attiva
    testo = (testo or "").strip()
    if not testo:
        return
    print(f"\nJ.A.R.V.I.S.: {testo}")
    HUD.aggiungi("jarvis", testo)
    HUD.imposta("parla")
    if _neurale_attiva:
        try:
            _parla_neurale(per_la_voce(testo, multilingue="Multilingual" in VOCE_NEURALE))
            return
        except Exception as e:  # servizio non ufficiale: se cade, si passa alla voce di sistema
            _neurale_attiva = False
            print(f"[Voce neurale non disponibile ({type(e).__name__}: {e}): "
                  "passo alla voce di sistema fino al prossimo avvio]")
    _parla_sistema(per_la_voce(testo))


# ==========================================================================
# ASCOLTO
# ==========================================================================

class Orecchie:
    def __init__(self) -> None:
        self.recognizer = sr.Recognizer()
        self.recognizer.dynamic_energy_threshold = True
        # Silenzio che chiude la frase: meno attesa prima di rispondere, ma sotto ~0,5 s
        # rischia di tagliare chi fa una pausa a meta' frase.
        self.recognizer.pause_threshold = PAUSA_FINE_FRASE
        try:
            self.mic = sr.Microphone()
        except (OSError, AttributeError) as e:
            if IS_MAC:
                aiuto = (
                    "Su macOS serve: brew install portaudio && pip install --force-reinstall pyaudio\n"
                    "piu' il permesso Microfono per il Terminale in "
                    "Impostazioni > Privacy e sicurezza."
                )
            else:
                aiuto = (
                    "Su Windows serve: pip install pyaudio\n"
                    "piu' l'accesso al microfono per le app desktop in "
                    "Impostazioni > Privacy e sicurezza > Microfono."
                )
            sys.exit(f"Microfono non disponibile ({e}).\n{aiuto}")
        print("[Calibrazione del rumore ambientale, un secondo di silenzio...]")
        with self.mic as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
        print(f"[Soglia energia impostata a {self.recognizer.energy_threshold:.0f}]")

    def ascolta(self, attesa: float | None = None) -> str:
        """attesa: secondi massimi per iniziare a parlare (default LISTEN_TIMEOUT)."""
        with self.mic as source:
            if not SENZA_CONSOLE:   # nel registro riempirebbe una riga ogni 6 secondi
                print("\n[In ascolto... parli pure]")
            try:
                audio = self.recognizer.listen(
                    source,
                    timeout=attesa if attesa is not None else LISTEN_TIMEOUT,
                    phrase_time_limit=PHRASE_TIME_LIMIT,
                )
            except sr.WaitTimeoutError:
                return ""
        fine_parlato = time.monotonic()
        try:
            risultato = self.recognizer.recognize_google(audio, language=STT_LANG, show_all=True)
            TEMPI["trascrizione"] = time.monotonic() - fine_parlato
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            print(f"[Servizio di trascrizione non raggiungibile: {e}]")
            return ""
        testo = scegli_trascrizione(risultato)
        if testo:
            print(f"Tu: {testo}")
        return testo


def scegli_trascrizione(risultato) -> str:
    """
    Google restituisce piu' ipotesi. "Jarvis" e' un nome inglese e il riconoscimento
    italiano spesso lo storpia nella prima ipotesi ("Ehi ya"), ma lo azzecca in un'altra:
    si preferisce la prima ipotesi che lo contiene, altrimenti la piu' probabile.
    """
    if not isinstance(risultato, dict):
        return ""
    ipotesi = [
        a["transcript"] for a in risultato.get("alternative", [])
        if isinstance(a, dict) and a.get("transcript")
    ]
    if not ipotesi:
        return ""
    for testo in ipotesi:
        if rivolta_a_jarvis(testo):
            if testo != ipotesi[0]:
                print(f"[Scelta l'ipotesi con 'Jarvis' invece di: {ipotesi[0]}]")
            return testo
    if len(ipotesi) > 1:
        print(f"[Altre ipotesi di Google: {' | '.join(ipotesi[1:4])}]")
    return ipotesi[0]


# ==========================================================================
# WHITELIST APPLICAZIONI
# ==========================================================================

# macOS: nome parlato -> nome applicazione per 'open -a' e AppleScript.
APP_MAC = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "terminale": "Terminal",
    "finder": "Finder",
    "mail": "Mail",
    "calendario": "Calendar",
    "note": "Notes",
    "promemoria": "Reminders",
    "musica": "Music",
    "spotify": "Spotify",
    "vscode": "Visual Studio Code",
    "anteprima": "Preview",
    "messaggi": "Messages",
    "impostazioni": "System Settings",
}

# Windows: nome parlato -> (cosa aprire, processo da chiudere).
# Processo None = l'app si apre ma non si chiude da voce.
APP_WIN = {
    "chrome": ("chrome.exe", "chrome.exe"),
    "edge": ("msedge.exe", "msedge.exe"),
    "esplora risorse": ("explorer.exe", None),   # chiuderlo farebbe sparire la barra delle applicazioni
    "blocco note": ("notepad.exe", "notepad.exe"),
    "calcolatrice": ("calculator:", "CalculatorApp.exe"),
    "terminale": ("wt.exe", "WindowsTerminal.exe"),
    "word": ("winword.exe", "WINWORD.EXE"),
    "excel": ("excel.exe", "EXCEL.EXE"),
    "outlook": ("outlook.exe", "OUTLOOK.EXE"),
    "spotify": ("spotify:", "Spotify.exe"),
    "impostazioni": ("ms-settings:", "SystemSettings.exe"),
}

# Elenco personale, modificabile a mano: si aggiunge a quello di base e ne sostituisce
# le voci con lo stesso nome. Sta in ~/Jarvis, che gli aggiornamenti non toccano
# (la cartella del codice invece viene sostituita), e MAI dentro la sandbox:
# se il modello potesse scriverci, potrebbe mettere in lista qualunque programma.
FILE_APP = CARTELLA_JARVIS / ("app_windows.json" if IS_WIN else "app_mac.json")


def _voce_app_valida(valore) -> bool:
    if IS_WIN:
        return (
            isinstance(valore, list) and len(valore) == 2
            and isinstance(valore[0], str) and valore[0].strip() != ""
            and (valore[1] is None or isinstance(valore[1], str))
        )
    # Il nome finisce dentro 'tell application "..."': niente virgolette ne' barre.
    return isinstance(valore, str) and valore.strip() != "" and not set('"\\') & set(valore)


def _carica_app(base: dict, percorso: Path) -> dict:
    """Elenco di base + voci del file personale. Se il file ha errori, resta solo la base."""
    if not percorso.exists():
        return dict(base)
    if WORKSPACE.resolve() in percorso.parents:
        print(f"[ATTENZIONE: {percorso.name} e' dentro la cartella di lavoro di J.A.R.V.I.S.: ignorato]")
        return dict(base)
    try:
        dati = json.loads(percorso.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"[ATTENZIONE: {percorso.name} ha un errore alla riga {e.lineno}: {e.msg}. "
              "Uso solo l'elenco di base.]")
        return dict(base)
    except OSError as e:
        print(f"[ATTENZIONE: {percorso.name} non leggibile ({e}). Uso solo l'elenco di base.]")
        return dict(base)
    if not isinstance(dati, dict):
        print(f"[ATTENZIONE: {percorso.name} deve contenere un elenco tra graffe {{ }}. "
              "Uso solo l'elenco di base.]")
        return dict(base)

    risultato = dict(base)
    aggiunte = 0
    for nome, valore in dati.items():
        if not _voce_app_valida(valore):
            formato = '["cosa aprire", "processo.exe"]' if IS_WIN else '"Nome App"'
            print(f"[ATTENZIONE: in {percorso.name} la voce '{nome}' e' scritta male "
                  f"(formato atteso: {formato}). Ignorata.]")
            continue
        risultato[nome.strip().lower()] = tuple(valore) if IS_WIN else valore
        aggiunte += 1
    print(f"[{percorso.name}: {aggiunte} app caricate]")
    return risultato


APP_CONSENTITE = _carica_app(APP_WIN if IS_WIN else APP_MAC, FILE_APP)


# ==========================================================================
# SANDBOX FILE
# ==========================================================================

# Lista bianca: tutto cio' che non e' qui viene rifiutato, eseguibili compresi.
ESTENSIONI_CONSENTITE = {".txt", ".md", ".csv", ".json", ".log"}


def _percorso_sicuro(nome: str, cartella: bool = False) -> Path:
    """Risolve un nome dentro la sandbox, rifiutando ogni fuga."""
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("Nome file vuoto.")
    if ":" in nome:
        # Su Windows 'C:file' e 'file.txt:flusso' sono percorsi insidiosi.
        raise ValueError("Il carattere ':' non e' consentito nei nomi.")
    if Path(nome).is_absolute() or nome.startswith(("/", "\\")):
        raise ValueError("I percorsi assoluti non sono consentiti.")
    if ".." in Path(nome).parts:
        raise ValueError("I riferimenti alla cartella superiore non sono consentiti.")

    radice = WORKSPACE.resolve()
    candidato = (radice / nome).resolve()   # resolve() segue anche i collegamenti simbolici
    if candidato == radice or radice not in candidato.parents:
        raise ValueError("Percorso fuori dalla cartella di lavoro consentita.")
    if not cartella and candidato.suffix.lower() not in ESTENSIONI_CONSENTITE:
        consentite = ", ".join(sorted(ESTENSIONI_CONSENTITE))
        raise ValueError(f"Estensione '{candidato.suffix}' non consentita. Ammesse: {consentite}.")
    return candidato


# ==========================================================================
# IMPLEMENTAZIONE DEGLI STRUMENTI
# ==========================================================================

def tool_apri_app(nome: str) -> str:
    chiave = (nome or "").lower()
    if chiave not in APP_CONSENTITE:
        return f"App '{nome}' non presente nella whitelist."
    if IS_MAC:
        app = APP_CONSENTITE[chiave]
        esito = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=20)
        if esito.returncode == 0:
            return f"{app} aperta."
        return f"Impossibile aprire {app}: {esito.stderr.strip() or 'app non installata'}"
    bersaglio, _ = APP_CONSENTITE[chiave]
    try:
        os.startfile(bersaglio)  # solo Windows; il bersaglio viene dal dizionario, non dal modello
    except OSError as e:
        return f"Impossibile aprire {chiave}: {e}. Forse non e' installata."
    return f"{chiave} aperta."


def tool_chiudi_app(nome: str) -> str:
    chiave = (nome or "").lower()
    if chiave not in APP_CONSENTITE:
        return f"App '{nome}' non presente nella whitelist."
    if IS_MAC:
        app = APP_CONSENTITE[chiave]
        esito = subprocess.run(
            ["osascript", "-e", f'tell application "{app}" to quit'],
            capture_output=True, text=True, timeout=30,
        )
        if esito.returncode == 0:
            return f"{app} chiusa."
        return (
            f"Impossibile chiudere {app}: {esito.stderr.strip()}. "
            "Potrebbe servire il permesso Automazione in Impostazioni > Privacy e sicurezza."
        )
    _, processo = APP_CONSENTITE[chiave]
    if processo is None:
        return f"{chiave} non si puo' chiudere da comando vocale."
    # Senza /F: chiusura gentile, l'app puo' chiedere di salvare. Niente chiusure forzate.
    esito = subprocess.run(
        ["taskkill", "/IM", processo],
        capture_output=True, text=True, errors="replace", timeout=30,
        creationflags=SENZA_FINESTRA,
    )
    if esito.returncode == 0:
        return f"{chiave} chiusa."
    return f"Impossibile chiudere {chiave}: {(esito.stderr or esito.stdout).strip()}"


def tool_scrivi_file(nome_file: str, contenuto: str, modalita: str = "sovrascrivi") -> str:
    try:
        percorso = _percorso_sicuro(nome_file)
    except ValueError as e:
        return f"Rifiutato: {e}"
    percorso.parent.mkdir(parents=True, exist_ok=True)
    esisteva = percorso.exists()
    apertura = "a" if modalita == "aggiungi" else "w"
    try:
        with open(percorso, apertura, encoding="utf-8") as f:
            if apertura == "a" and esisteva:
                f.write("\n")
            f.write(contenuto or "")
    except OSError as e:
        return f"Errore di scrittura: {e}"
    azione = "aggiornato" if esisteva else "creato"
    relativo = percorso.relative_to(WORKSPACE.resolve())
    # Dire sempre DOVE: in una prova reale il modello, senza questa indicazione,
    # ha detto all'utente di aver scritto "nel progetto" un file finito qui.
    return (f"File {azione} nella cartella di lavoro di Jarvis ({WORKSPACE}), NON in un progetto: "
            f"{relativo.as_posix()} ({percorso.stat().st_size} byte)")


def tool_leggi_file(nome_file: str) -> str:
    try:
        percorso = _percorso_sicuro(nome_file)
    except ValueError as e:
        return f"Rifiutato: {e}"
    if not percorso.is_file():
        return f"File non trovato: {nome_file}"
    try:
        dati = percorso.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Errore di lettura: {e}"
    if len(dati) > MAX_BYTE_LETTURA:
        dati = dati[:MAX_BYTE_LETTURA] + "\n[...troncato...]"
    return dati


def tool_elenca_file(sottocartella: str = "") -> str:
    base = WORKSPACE.resolve()
    if sottocartella:
        try:
            base = _percorso_sicuro(sottocartella, cartella=True)
        except ValueError as e:
            return f"Rifiutato: {e}"
    if not base.is_dir():
        return "Cartella non trovata."
    voci = sorted(p.name + ("/" if p.is_dir() else "") for p in base.iterdir())
    return "\n".join(voci) if voci else "(cartella vuota)"


def _batteria() -> str:
    if IS_MAC:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().replace("\n", " ")
    if IS_WIN:
        out = _powershell(PS_BATTERIA, timeout=20)
        return out.stdout.strip() or out.stderr.strip()
    return "n/d su questo sistema"


def _disco() -> str:
    radice = Path.home().anchor or "/"
    uso = shutil.disk_usage(radice)
    gb = 1024 ** 3
    return f"{uso.free / gb:.0f} GB liberi su {uso.total / gb:.0f} GB ({radice})"


GIORNI = ("lunedi'", "martedi'", "mercoledi'", "giovedi'", "venerdi'", "sabato", "domenica")


def tool_stato_sistema(cosa: str = "tutto") -> str:
    pezzi = []
    if cosa in ("ora", "tutto"):
        adesso = datetime.now()
        # Il giorno della settimana va dato esplicitamente: il modello, se deve dedurlo, sbaglia.
        pezzi.append(f"Data e ora: {GIORNI[adesso.weekday()]} {adesso:%d/%m/%Y %H:%M}")
    if cosa in ("batteria", "tutto"):
        try:
            pezzi.append("Batteria: " + _batteria())
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            pezzi.append(f"Batteria non leggibile: {e}")
    if cosa in ("disco", "tutto"):
        try:
            pezzi.append("Disco: " + _disco())
        except OSError as e:
            pezzi.append(f"Disco non leggibile: {e}")
    return "\n".join(pezzi) or "Nessun dato richiesto."


# Mail: Jarvis prepara SOLO la bozza e la apre; la rilegge e la invia l'utente.
# Nessuno strumento puo' spedire: un testo ostile letto sul web non puo' far
# partire una mail, al massimo comporne una che l'utente vede prima di inviarla.
_INDIRIZZO = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MAX_TESTO_MAILTO = 1500   # i collegamenti mailto oltre ~2000 caratteri vengono troncati da Windows


def indirizzi_validi(destinatari: str) -> list[str] | None:
    """Lista di indirizzi separati da virgola o punto e virgola; None se uno non e' valido."""
    parti = [p.strip() for p in re.split(r"[,;]", destinatari or "") if p.strip()]
    return parti if all(_INDIRIZZO.match(p) for p in parti) else None


def link_mailto(destinatari: list[str], oggetto: str, testo: str) -> str:
    from urllib.parse import quote
    return ("mailto:" + quote(",".join(destinatari), safe="@,")
            + "?subject=" + quote(oggetto, safe="") + "&body=" + quote(testo, safe=""))


def _bozza_outlook(destinatari: list[str], oggetto: str, testo: str) -> bool:
    """Outlook classico via COM: bozza aperta a schermo, mai .Send(). False se non disponibile."""
    if not IS_WIN:
        return False
    try:
        import win32com.client
        mail = win32com.client.Dispatch("Outlook.Application").CreateItem(0)   # 0 = messaggio
        mail.To = "; ".join(destinatari)
        mail.Subject = oggetto
        mail.Body = testo
        mail.Display(False)
        return True
    except Exception as e:   # pywin32 assente, nuovo Outlook senza COM, Outlook chiuso male...
        print(f"[Bozza via Outlook non riuscita ({type(e).__name__}): uso il programma di posta predefinito]")
        return False


def tool_prepara_mail(oggetto: str, testo: str, destinatari: str = "") -> str:
    lista = indirizzi_validi(destinatari)
    if lista is None:
        return f"Indirizzo non valido: '{destinatari}'. Chiedi all'utente l'indirizzo corretto."
    oggetto, testo = (oggetto or "").strip()[:200], (testo or "").strip()
    if not testo:
        return "Serve il testo della mail."
    if _bozza_outlook(lista, oggetto, testo):
        dove = "in Outlook"
    else:
        troncato = len(testo) > MAX_TESTO_MAILTO
        webbrowser.open(link_mailto(lista, oggetto, testo[:MAX_TESTO_MAILTO]))
        dove = "nel programma di posta predefinito" + (" (testo accorciato: era troppo lungo)" if troncato else "")
    return (f"Bozza aperta {dove}. NON e' stata inviata: di' all'utente di rileggerla e di "
            "premere Invia lui stesso.")


ESECUTORI = {
    "prepara_mail": tool_prepara_mail,
    "apri_app": tool_apri_app,
    "chiudi_app": tool_chiudi_app,
    "scrivi_file": tool_scrivi_file,
    "leggi_file": tool_leggi_file,
    "elenca_file": tool_elenca_file,
    "stato_sistema": tool_stato_sistema,
}

_NOMI_APP = sorted(APP_CONSENTITE.keys())
_ESTENSIONI = ", ".join(sorted(ESTENSIONI_CONSENTITE))

TOOLS_LOCALI = [
    {
        "name": "prepara_mail",
        "description": (
            "Prepara una bozza di email e la apre sullo schermo (in Outlook se possibile). "
            "NON invia: l'utente la rilegge e preme Invia. Scrivi il testo completo, in italiano, "
            "salvo diversa richiesta. Se l'utente non dice l'indirizzo lascia vuoti i destinatari."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destinatari": {"type": "string", "description": "Indirizzi separati da virgola, o vuoto."},
                "oggetto": {"type": "string"},
                "testo": {"type": "string"},
            },
            "required": ["oggetto", "testo"],
        },
    },
    {
        "name": "apri_app",
        "description": f"Apre un'applicazione sul {NOME_MACCHINA}. Solo le app in whitelist.",
        "input_schema": {
            "type": "object",
            "properties": {"nome": {"type": "string", "enum": _NOMI_APP}},
            "required": ["nome"],
        },
    },
    {
        "name": "chiudi_app",
        "description": f"Chiude un'applicazione aperta sul {NOME_MACCHINA}. Solo le app in whitelist.",
        "input_schema": {
            "type": "object",
            "properties": {"nome": {"type": "string", "enum": _NOMI_APP}},
            "required": ["nome"],
        },
    },
    {
        "name": "scrivi_file",
        "description": (
            "Crea o modifica un file di testo nella cartella di lavoro privata di Jarvis "
            "(appunti, liste, promemoria). "
            f"Usa percorsi relativi, es. 'note/spesa.md'. Estensioni ammesse: {_ESTENSIONI}. "
            "Non puo' scrivere altrove: NON usarlo mai per i progetti dell'utente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_file": {"type": "string", "description": "Percorso relativo, es. 'appunti.txt'"},
                "contenuto": {"type": "string"},
                "modalita": {
                    "type": "string",
                    "enum": ["sovrascrivi", "aggiungi"],
                    "description": "'aggiungi' accoda in fondo senza cancellare il contenuto.",
                },
            },
            "required": ["nome_file", "contenuto"],
        },
    },
    {
        "name": "leggi_file",
        "description": "Legge un file di testo dalla cartella di lavoro privata di Jarvis (non dai progetti).",
        "input_schema": {
            "type": "object",
            "properties": {"nome_file": {"type": "string"}},
            "required": ["nome_file"],
        },
    },
    {
        "name": "elenca_file",
        "description": "Elenca i file nella cartella di lavoro privata di Jarvis o in una sua sottocartella (non nei progetti).",
        "input_schema": {
            "type": "object",
            "properties": {"sottocartella": {"type": "string"}},
        },
    },
    {
        "name": "stato_sistema",
        "description": "Legge data e ora, stato batteria e spazio disco. Sola lettura.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cosa": {"type": "string", "enum": ["ora", "batteria", "disco", "tutto"]}
            },
        },
    },
]

TOOL_WEB = {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_RICERCHE_WEB}
TUTTI_I_TOOLS = TOOLS_LOCALI + [TOOL_WEB]


def esegui_tool(nome: str, argomenti: dict) -> str:
    funzione = ESECUTORI.get(nome)
    if funzione is None:
        return f"Strumento sconosciuto: {nome}"
    try:
        return str(funzione(**(argomenti or {})))
    except TypeError as e:
        return f"Parametri non validi per {nome}: {e}"
    except subprocess.TimeoutExpired:
        return f"{nome} non ha risposto in tempo."
    except Exception as e:  # un tool che fallisce non deve far cadere J.A.R.V.I.S.
        return f"Errore in {nome}: {type(e).__name__}: {e}"


# ==========================================================================
# CLAUDE CODE: lavori di programmazione sui progetti dell'utente
# Protezioni, in ordine:
#  - solo cartelle elencate a mano dall'utente in progetti_windows.json /
#    progetti_mac.json; mai la cartella di J.A.R.V.I.S., ~/Jarvis o la sandbox;
#  - il modello puo' solo PREPARARE il lavoro: parte solo se l'utente dice
#    "conferma" a voce, e la conferma e' riconosciuta qui, non dal modello;
#  - Claude Code in modalita' --restricted, con i soli strumenti sui file:
#    niente comandi, niente web, niente server MCP, niente domande di permesso;
#  - il compito (testo del modello) passa da stdin, mai tra gli argomenti;
#  - senza ANTHROPIC_API_KEY nell'ambiente, cosi' usa l'abbonamento Claude e
#    non il credito API (in modalita' -p la chiave, se c'e', vince sempre).
# ==========================================================================

CARTELLA_CODICE_JARVIS = Path(__file__).resolve().parent
# In ~/Jarvis come l'elenco delle app: sopravvive agli aggiornamenti, fuori dalla sandbox.
FILE_PROGETTI = CARTELLA_JARVIS / ("progetti_windows.json" if IS_WIN else "progetti_mac.json")
CARTELLA_REPORT = CARTELLA_JARVIS / "claude_code"
VERSIONE_MINIMA_CLAUDE = (2, 1, 259)   # --restricted dalla 2.1.248, --permission-prompts dalla 2.1.259
DURATA_MASSIMA_LAVORO = 30 * 60        # secondi
SCADENZA_CONFERMA = 60                 # secondi per dire "conferma"
BUDGET_LAVORO_USD = "5"                # tetto di spesa stimata per lavoro
COMANDI_CONFERMA = {"conferma", "confermo", "si conferma", "sì conferma", "procedi"}
COMANDI_ANNULLA = {"annulla", "no", "lascia stare", "lascia perdere"}
COMANDI_RIPRISTINO = {
    "annulla l ultimo lavoro", "annulla ultimo lavoro", "ripristina l ultimo lavoro",
    "ripristina il progetto", "torna indietro",
}
ISTRUZIONE_CLAUDE_CODE = (
    "Svolgi il compito descritto nel testo ricevuto in ingresso, lavorando solo nella "
    "cartella corrente. Alla fine riassumi in italiano, in due o tre frasi semplici adatte "
    "a essere lette ad alta voce, cosa hai cambiato e in quali file."
)
ISTRUZIONE_LETTURA = (
    "Rispondi alla richiesta descritta nel testo ricevuto in ingresso leggendo e cercando i "
    "file della cartella corrente, senza modificare nulla. La risposta verra' letta ad alta "
    "voce: in italiano, al massimo quattro frasi brevi, nessuna premessa ne' spiegazione di "
    "come hai lavorato, niente elenchi lunghi. Vai subito al contenuto richiesto; se i "
    "risultati sono molti, di' i piu' importanti e quanti sono in tutto."
)

# Copia di sicurezza prima di ogni lavoro che modifica: "Jarvis, annulla l'ultimo lavoro".
CARTELLA_BACKUP = CARTELLA_JARVIS / "backup"
FILE_ULTIMO_LAVORO = CARTELLA_BACKUP / "ultimo_lavoro.json"
MAX_BYTE_BACKUP = 500 * 1024 ** 2      # oltre, niente lavori di modifica: il progetto va gestito con git
BACKUP_DA_TENERE = 5                   # per progetto

ESEGUIBILE_CLAUDE: str | None = None
# nome -> {"percorso": Path, "sola_lettura": bool}
PROGETTI: dict[str, dict] = {}


def _cartelle_protette() -> list[Path]:
    return [CARTELLA_CODICE_JARVIS.resolve(), CARTELLA_JARVIS.resolve(), WORKSPACE.resolve()]


def progetto_non_valido(percorso: Path) -> str | None:
    """Motivo per cui una cartella non puo' essere un progetto, None se va bene."""
    if not percorso.is_absolute():
        return "il percorso deve essere completo (es. C:\\Users\\nome\\Progetti\\sito)"
    cartella = percorso.resolve()
    if not cartella.is_dir():
        return "la cartella non esiste"
    if cartella == Path.home().resolve() or cartella == Path(cartella.anchor):
        return "cartella troppo ampia (la home o un intero disco)"
    for protetta in _cartelle_protette():
        if cartella == protetta or protetta in cartella.parents or cartella in protetta.parents:
            return "coincide con J.A.R.V.I.S. o la contiene: vietato"
    return None


def carica_progetti(percorso: Path) -> dict[str, dict]:
    """
    Formato: {"nome": "C:\\\\percorso"} per un progetto modificabile,
    {"nome": {"percorso": "C:\\\\percorso", "sola_lettura": true}} per una cartella da consultare.
    """
    if not percorso.exists():
        return {}
    try:
        dati = json.loads(percorso.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"[ATTENZIONE: {percorso.name} ha un errore alla riga {e.lineno}: {e.msg}. "
              "Claude Code disattivato.]")
        return {}
    except OSError as e:
        print(f"[ATTENZIONE: {percorso.name} non leggibile ({e}). Claude Code disattivato.]")
        return {}
    if not isinstance(dati, dict):
        print(f"[ATTENZIONE: {percorso.name} deve contenere un elenco tra graffe {{ }}.]")
        return {}
    progetti = {}
    for nome, valore in dati.items():
        sola_lettura = False
        if isinstance(valore, dict):
            sola_lettura = valore.get("sola_lettura") is True
            valore = valore.get("percorso")
        if not isinstance(valore, str) or not valore.strip():
            print(f"[ATTENZIONE: progetto '{nome}': serve il percorso della cartella. Ignorato.]")
            continue
        motivo = progetto_non_valido(Path(valore.strip()).expanduser())
        if motivo:
            print(f"[ATTENZIONE: progetto '{nome}': {motivo}. Ignorato.]")
            continue
        progetti[nome.strip().lower()] = {
            "percorso": Path(valore.strip()).expanduser().resolve(),
            "sola_lettura": sola_lettura,
        }
    return progetti


def versione_claude(eseguibile: str) -> tuple[int, int, int] | None:
    try:
        esito = subprocess.run(
            [eseguibile, "--version"], capture_output=True, text=True, errors="replace",
            timeout=20, creationflags=SENZA_FINESTRA,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    trovata = re.search(r"(\d+)\.(\d+)\.(\d+)", esito.stdout or "")
    return tuple(int(n) for n in trovata.groups()) if trovata else None


def comando_claude_code(eseguibile: str, sola_lettura: bool = False) -> list[str]:
    """Argomenti tutti costanti: il testo del compito arriva da stdin."""
    return [
        eseguibile, "-p", ISTRUZIONE_LETTURA if sola_lettura else ISTRUZIONE_CLAUDE_CODE,
        "--restricted",
        "--tools", "Read,Glob,Grep" if sola_lettura else "Read,Edit,Write,Glob,Grep",
        "--disallowedTools", "mcp__*",
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",
        "--max-turns", "60",
        "--max-budget-usd", BUDGET_LAVORO_USD,
        "--output-format", "json",
    ]


def ambiente_claude_code() -> dict:
    """Ambiente senza chiavi API: Claude Code usa l'abbonamento, non il credito di J.A.R.V.I.S."""
    return {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}


def _prime_frasi(testo: str, massimo: int = 320) -> str:
    testo = re.sub(r"[#*`_>|]", "", testo or "").strip()
    testo = re.sub(r"\s+", " ", testo)
    if len(testo) <= massimo:
        return testo
    taglio = testo[:massimo]
    punto = taglio.rfind(". ")
    return taglio[:punto + 1] if punto > 80 else taglio.rstrip() + "..."


def elenco_file(cartella: Path) -> set[str]:
    return {p.relative_to(cartella).as_posix() for p in cartella.rglob("*") if p.is_file()}


def dimensione_cartella(cartella: Path, limite: int) -> int:
    """Somma le dimensioni fermandosi appena supera il limite (non serve contare tutto)."""
    totale = 0
    for p in cartella.rglob("*"):
        if p.is_file():
            totale += p.stat().st_size
            if totale > limite:
                break
    return totale


def crea_backup(progetto: str, cartella: Path) -> Path:
    if dimensione_cartella(cartella, MAX_BYTE_BACKUP) > MAX_BYTE_BACKUP:
        raise OSError(f"il progetto supera {MAX_BYTE_BACKUP // 1024 ** 2} MB: troppo grande per "
                      "la copia di sicurezza automatica")
    base = CARTELLA_BACKUP / re.sub(r"[^\w-]", "_", progetto)
    destinazione = base / f"{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copytree(cartella, destinazione)
    vecchi = sorted(p for p in base.iterdir() if p.is_dir())[:-BACKUP_DA_TENERE]
    for vecchio in vecchi:
        shutil.rmtree(vecchio, ignore_errors=True)
    return destinazione


class LavoroClaudeCode:
    def __init__(self, esegui=subprocess.run) -> None:
        self._lock = threading.Lock()
        self._esegui_processo = esegui
        self.in_attesa: dict | None = None
        self.in_corso: dict | None = None
        self.avvisi: deque = deque()

    def prepara(self, progetto: str, percorso: Path, compito: str, ora: float,
                sola_lettura: bool = False) -> None:
        with self._lock:
            self.in_attesa = {"tipo": "lavoro", "progetto": progetto, "percorso": percorso,
                              "compito": compito, "sola_lettura": sola_lettura,
                              "scade": ora + SCADENZA_CONFERMA}

    def prepara_ripristino(self, ora: float) -> str:
        """Prepara l'annullamento dell'ultimo lavoro di modifica; parte solo con 'conferma'."""
        ultimo = self.ultimo_lavoro()
        if not ultimo:
            return "Non ho nessun lavoro da annullare."
        if self.occupato():
            return "Claude Code sta lavorando: aspetti che finisca."
        with self._lock:
            self.in_attesa = {"tipo": "ripristino", "progetto": ultimo["progetto"],
                              "scade": ora + SCADENZA_CONFERMA}
        return (f"Riporto il progetto {ultimo['progetto']} com'era prima dell'ultimo lavoro. "
                "Le modifiche fatte dopo a quei file andranno perse. Dica conferma.")

    @staticmethod
    def ultimo_lavoro() -> dict | None:
        try:
            dati = json.loads(FILE_ULTIMO_LAVORO.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return dati if isinstance(dati, dict) and Path(dati.get("backup", "")).is_dir() else None

    def ripristina(self) -> str:
        ultimo = self.ultimo_lavoro()
        if not ultimo:
            return "Non ho nessun lavoro da annullare."
        cartella, backup = Path(ultimo["percorso"]), Path(ultimo["backup"])
        try:
            shutil.copytree(backup, cartella, dirs_exist_ok=True)   # rimette i file modificati o cancellati
            for relativo in ultimo.get("nuovi", []):                 # toglie quelli creati dal lavoro
                (cartella / relativo).unlink(missing_ok=True)
            FILE_ULTIMO_LAVORO.unlink(missing_ok=True)
        except OSError as e:
            return f"Il ripristino non e' riuscito del tutto: {e}. La copia e' in {backup}."
        return f"Fatto: il progetto {ultimo['progetto']} e' tornato com'era prima dell'ultimo lavoro."

    def ha_attesa(self, ora: float) -> bool:
        with self._lock:
            if self.in_attesa and ora > self.in_attesa["scade"]:
                self.in_attesa = None
            return self.in_attesa is not None

    def annulla(self) -> None:
        with self._lock:
            self.in_attesa = None

    def occupato(self) -> str | None:
        with self._lock:
            return self.in_corso["progetto"] if self.in_corso else None

    def avvia(self) -> str:
        """Chiamato SOLO dopo la conferma vocale riconosciuta dal ciclo principale."""
        with self._lock:
            lavoro, self.in_attesa = self.in_attesa, None
            if lavoro is None:
                return "Non c'e' nessun lavoro da confermare."
            if self.in_corso:
                return "Claude Code e' gia' al lavoro su un altro progetto."
            if lavoro["tipo"] == "lavoro":
                lavoro["inizio"] = time.time()
                self.in_corso = lavoro
        if lavoro["tipo"] == "ripristino":
            return self.ripristina()
        threading.Thread(target=self._esegui, args=(lavoro,), daemon=True).start()
        return f"Avviato su {lavoro['progetto']}. La avviso quando ha finito."

    def _esegui(self, lavoro: dict) -> None:
        esito_testo, errore, costo = "", False, None
        cartella = Path(lavoro["percorso"])
        if not lavoro["sola_lettura"]:
            try:
                lavoro["backup"] = crea_backup(lavoro["progetto"], cartella)
                lavoro["prima"] = elenco_file(cartella)
            except OSError as e:
                self._chiudi(lavoro, f"niente copia di sicurezza, lavoro non avviato ({e})", True, None)
                return
        try:
            esito = self._esegui_processo(
                comando_claude_code(ESEGUIBILE_CLAUDE or "claude", lavoro["sola_lettura"]),
                input=lavoro["compito"], cwd=str(lavoro["percorso"]), env=ambiente_claude_code(),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=DURATA_MASSIMA_LAVORO, creationflags=SENZA_FINESTRA,
            )
            try:
                dati = json.loads(esito.stdout or "")
            except ValueError:
                dati = {}
            if not isinstance(dati, dict):
                dati = {}
            esito_testo = str(dati.get("result") or esito.stdout or esito.stderr or "").strip()
            errore = bool(dati.get("is_error")) or esito.returncode != 0
            costo = dati.get("total_cost_usd")
        except subprocess.TimeoutExpired:
            errore, esito_testo = True, f"superato il limite di {DURATA_MASSIMA_LAVORO // 60} minuti"
        except OSError as e:
            errore, esito_testo = True, f"impossibile avviare Claude Code ({e})"
        if "backup" in lavoro:
            # Anche se il lavoro e' fallito puo' aver cambiato qualcosa: si puo' sempre annullare.
            try:
                CARTELLA_BACKUP.mkdir(parents=True, exist_ok=True)
                FILE_ULTIMO_LAVORO.write_text(json.dumps({
                    "progetto": lavoro["progetto"],
                    "percorso": str(cartella),
                    "backup": str(lavoro["backup"]),
                    "nuovi": sorted(elenco_file(cartella) - lavoro["prima"]),
                }), encoding="utf-8")
            except OSError as e:
                print(f"[Dati per l'annullamento non salvati: {e}]")
        self._chiudi(lavoro, esito_testo, errore, costo)

    def _chiudi(self, lavoro: dict, esito_testo: str, errore: bool, costo) -> None:
        durata = max(1, round((time.time() - lavoro["inizio"]) / 60))
        try:
            CARTELLA_REPORT.mkdir(parents=True, exist_ok=True)
            nome_progetto = re.sub(r"[^\w-]", "_", lavoro["progetto"])
            nome = f"{datetime.now():%Y%m%d-%H%M%S}-{nome_progetto}.md"
            (CARTELLA_REPORT / nome).write_text(
                f"# Claude Code - {lavoro['progetto']}\n\nCartella: {lavoro['percorso']}\n"
                f"Durata: circa {durata} min\nCosto stimato: {costo}\nErrore: {errore}\n\n"
                f"## Compito\n\n{lavoro['compito']}\n\n## Esito\n\n{esito_testo}\n",
                encoding="utf-8",
            )
        except OSError as e:
            print(f"[Report di Claude Code non salvato: {e}]")
        # L'esito si legge a voce ma NON entra nella memoria del modello: quello che Claude Code
        # ha letto nei file non puo' finire in una ricerca web o in una mail.
        if errore:
            avviso = (f"Claude Code non e' riuscito a completare il lavoro su {lavoro['progetto']}: "
                      f"{_prime_frasi(esito_testo, 200) or 'motivo sconosciuto'}")
        elif lavoro["sola_lettura"]:
            avviso = f"Da {lavoro['progetto']}: {_prime_frasi(esito_testo, 700)}"
        else:
            avviso = (f"Claude Code ha finito il lavoro su {lavoro['progetto']}. "
                      f"{_prime_frasi(esito_testo)} Se non va bene, dica: annulla l'ultimo lavoro.")
        with self._lock:
            self.in_corso = None
            # a voce solo l'inizio, nella pagina il testo completo
            self.avvisi.append((avviso, _prime_frasi(esito_testo, 3000) or avviso))

    def prossimo_avviso(self) -> str | None:
        """Solo la parte da leggere a voce."""
        prossimo = self.prossimo_avviso_completo()
        return prossimo[0] if prossimo else None

    def prossimo_avviso_completo(self) -> tuple[str, str] | None:
        """(testo da leggere a voce, testo completo per la pagina)."""
        with self._lock:
            return self.avvisi.popleft() if self.avvisi else None

    def stato(self) -> dict:
        with self._lock:
            if self.in_corso:
                return {"fase": "lavoro", "progetto": self.in_corso["progetto"],
                        "secondi": round(time.time() - self.in_corso["inizio"])}
            if self.in_attesa:
                return {"fase": "conferma", "progetto": self.in_attesa["progetto"],
                        "ripristino": self.in_attesa["tipo"] == "ripristino"}
            return {"fase": "inattivo" if ESEGUIBILE_CLAUDE else "non configurato"}


LAVORO = LavoroClaudeCode()


def tool_claude_code(progetto: str, compito: str) -> str:
    chiave = (progetto or "").strip().lower()
    if chiave not in PROGETTI:
        return f"Progetto '{progetto}' non presente nell'elenco dell'utente."
    compito = (compito or "").strip()
    if not compito:
        return "Serve una descrizione del compito."
    if len(compito) > 4000:
        return "Descrizione del compito troppo lunga: riassumila."
    occupato = LAVORO.occupato()
    if occupato:
        return f"Claude Code sta gia' lavorando su {occupato}: bisogna aspettare che finisca."
    progetto_scelto = PROGETTI[chiave]
    LAVORO.prepara(chiave, progetto_scelto["percorso"], compito, time.monotonic(),
                   progetto_scelto["sola_lettura"])
    tipo = ("una ricerca in SOLA LETTURA (non modifichera' nulla)" if progetto_scelto["sola_lettura"]
            else "un lavoro di modifica (prima fara' una copia di sicurezza)")
    return (
        f"PREPARATO ma NON avviato: {tipo} in '{chiave}'. Riassumi all'utente in una frase cosa "
        "fara' Claude Code, poi chiedigli di dire 'conferma' entro un minuto. Non dire che e' "
        "gia' partito: parte solo con la sua conferma a voce. Il risultato lo leggera' Jarvis a "
        "voce quando e' pronto: tu non lo vedrai, quindi non inventarlo."
    )


def configura_claude_code() -> None:
    """Aggiunge lo strumento solo se Claude Code e' installato, aggiornato e ci sono progetti."""
    global ESEGUIBILE_CLAUDE, PROGETTI, SYSTEM_PROMPT
    PROGETTI = carica_progetti(FILE_PROGETTI)
    if not PROGETTI:
        print(f"[Claude Code: nessun progetto. Per attivarlo crea {FILE_PROGETTI.name}]")
        return
    eseguibile = shutil.which("claude")
    if not eseguibile:
        print("[Claude Code non trovato: installalo o controlla che il comando 'claude' funzioni]")
        return
    versione = versione_claude(eseguibile)
    if versione is None or versione < VERSIONE_MINIMA_CLAUDE:
        minima = ".".join(map(str, VERSIONE_MINIMA_CLAUDE))
        print(f"[Claude Code troppo vecchio o illeggibile ({versione}): serve la {minima}. "
              "Aggiorna con: claude update]")
        return
    ESEGUIBILE_CLAUDE = eseguibile
    ESECUTORI["claude_code"] = tool_claude_code
    TUTTI_I_TOOLS.insert(len(TOOLS_LOCALI), {
        "name": "claude_code",
        "description": (
            "Affida a Claude Code un lavoro in una delle cartelle dell'utente: nelle cartelle "
            "modificabili puo' creare, leggere e modificare file (con copia di sicurezza "
            "automatica), in quelle di sola lettura puo' solo cercare e leggere. Non puo' "
            "eseguire comandi. E' l'UNICO modo per toccare quelle cartelle: ogni volta che "
            "l'utente ne nomina una usa questo strumento. Il lavoro NON parte subito: serve la "
            "conferma a voce dell'utente. Descrivi il compito in modo completo e preciso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "progetto": {"type": "string", "enum": sorted(PROGETTI)},
                "compito": {"type": "string", "description": "Cosa deve fare Claude Code, in dettaglio."},
            },
            "required": ["progetto", "compito"],
        },
    })
    elenco = ", ".join(
        f"{nome} ({'sola lettura' if dati['sola_lettura'] else 'modificabile'})"
        for nome, dati in sorted(PROGETTI.items())
    )
    SYSTEM_PROMPT += (
        f"\n\nCartelle dell'utente per Claude Code: {elenco}. Qualunque richiesta che nomina "
        "una di queste cartelle, anche solo per cercare o leggere qualcosa, va affidata allo "
        "strumento claude_code, mai a scrivi_file o leggi_file. Non dire mai di aver fatto "
        "qualcosa in una cartella se claude_code non l'ha fatto: riferisci sempre esattamente "
        "dove e' stato salvato un file. Se l'utente vuole annullare l'ultimo lavoro di Claude "
        "Code, digli di dire esattamente: annulla l'ultimo lavoro."
    )
    print(f"[Claude Code {'.'.join(map(str, versione))}: {elenco}]")


# ==========================================================================
# DIALOGO CON CLAUDE (ciclo di tool use)
# ==========================================================================

def _messaggi(scambi: list[list[dict]]) -> list[dict]:
    return [m for scambio in scambi for m in scambio]


def _archivia(scambi: list[list[dict]], scambio: list[dict]) -> None:
    scambi.append(scambio)
    del scambi[:-MAX_SCAMBI]


def chiedi_a_claude(client, scambi: list[list[dict]], domanda: str) -> str:
    """Un turno completo: puo' includere piu' giri di tool use."""
    scambio: list[dict] = [{"role": "user", "content": domanda}]
    HUD.imposta("elaborazione")
    in_pausa = False

    for _ in range(MAX_GIRI_TOOL):
        partenza = time.monotonic()
        try:
            risposta = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TUTTI_I_TOOLS,
                messages=_messaggi(scambi) + scambio,
            )
            TEMPI["claude"] = TEMPI.get("claude", 0.0) + time.monotonic() - partenza
            TEMPI["chiamate"] = TEMPI.get("chiamate", 0) + 1
        except anthropic.AuthenticationError:
            return "La chiave API non e' valida. Verifichi ANTHROPIC_API_KEY."
        except anthropic.NotFoundError:
            return f"Il modello {MODEL} non risulta disponibile per questo account."
        except anthropic.RateLimitError:
            return "Ho superato il limite di richieste. Attenda qualche secondo."
        except anthropic.APIConnectionError:
            return "Non riesco a raggiungere i server. Controlli la connessione."
        except anthropic.APIStatusError as e:
            print(f"[Errore API {e.status_code}]: {e.message}")
            return "Ho riscontrato un errore tecnico. I dettagli sono a schermo."

        CONSUMI.registra(risposta.usage)
        uso = getattr(risposta.usage, "server_tool_use", None)
        if uso and getattr(uso, "web_search_requests", 0):
            print(f"[Ricerche web effettuate: {uso.web_search_requests}]")

        contenuto = list(risposta.content)
        if in_pausa:
            # Il server riprende da dove si era fermato: e' lo stesso messaggio dell'assistente.
            scambio[-1]["content"] = list(scambio[-1]["content"]) + contenuto
        else:
            scambio.append({"role": "assistant", "content": contenuto})
        in_pausa = False

        if risposta.stop_reason == "pause_turn":
            # La ricerca web lato server non ha finito: si rimanda la conversazione cosi' com'e'.
            in_pausa = True
            continue

        if risposta.stop_reason != "tool_use":
            if risposta.stop_reason == "max_tokens":
                # Risposta troncata: un blocco strumento a meta' renderebbe invalida la memoria.
                scambio[-1]["content"] = [
                    b for b in scambio[-1]["content"] if getattr(b, "type", "") == "text"
                ]
            testo = "".join(
                b.text for b in scambio[-1]["content"] if getattr(b, "type", "") == "text"
            ).strip()
            if scambio[-1]["content"]:
                _archivia(scambi, scambio)
            return testo or "Non ho una risposta da darle, temo."

        risultati = []
        for blocco in contenuto:
            if getattr(blocco, "type", "") != "tool_use":
                continue
            print(f"[Strumento] {blocco.name} {json.dumps(blocco.input, ensure_ascii=False)}")
            HUD.imposta("elaborazione", blocco.name.replace("_", " "))
            esito = esegui_tool(blocco.name, blocco.input)
            print(f"[Esito] {esito[:200]}")
            risultati.append(
                {"type": "tool_result", "tool_use_id": blocco.id, "content": esito}
            )
        scambio.append({"role": "user", "content": risultati})

    _archivia(scambi, scambio)
    return "Mi sono perso in un ciclo di operazioni. Provi a riformulare la richiesta."


# ==========================================================================
# COMANDI GESTITI SENZA API
# ==========================================================================

# La frase deve essere SOLO il comando: "esci" dentro "riesci ad aprire Safari?"
# o "esci da Word" non deve spegnere J.A.R.V.I.S.
COMANDI_USCITA = {
    "spegniti", "spegnimento", "arrivederci", "esci", "termina sessione", "termina la sessione",
}
COMANDI_AZZERA = {"dimentica tutto", "azzera la memoria"}

# Solo le frasi che contengono una di queste parole vengono prese come comandi:
# il resto (conversazioni, TV) non arriva a Claude, non costa e non fa agire.
# Le varianti coprono le trascrizioni sbagliate piu' probabili.
PAROLE_ATTIVAZIONE = {"jarvis", "jarvi", "giarvis", "jervis"}
PAROLE_DI_CORTESIA = PAROLE_ATTIVAZIONE | {"ehi", "hey", "ei", "per", "favore", "grazie"}


def rivolta_a_jarvis(frase: str) -> bool:
    return any(p in PAROLE_ATTIVAZIONE for p in re.findall(r"\w+", frase.lower()))


def _normalizza(frase: str) -> str:
    parole = re.findall(r"\w+", frase.lower())
    return " ".join(p for p in parole if p not in PAROLE_DI_CORTESIA)


def comando_locale(frase: str, scambi: list) -> bool:
    testo = _normalizza(frase)
    if testo in COMANDI_USCITA:
        raise Spegnimento
    if testo in COMANDI_AZZERA:
        scambi.clear()
        parla("Memoria della conversazione azzerata.")
        return True
    return False


# ==========================================================================
# ATTENZIONE: quando J.A.R.V.I.S. ascolta davvero
#  - sveglio: risponde alle frasi con "Jarvis"; dopo ogni risposta, per
#    FINESTRA_ASCOLTO secondi, anche alle frasi senza nome (stile Alexa);
#  - addormentato ("Jarvis, dormi"): ignora tutto tranne "Jarvis, svegliati",
#    "ehi Jarvis" e lo spegnimento.
# ==========================================================================

COMANDI_DORMI = {"dormi", "vai a dormire", "riposo", "pausa", "mettiti in pausa"}
COMANDI_SVEGLIA = {"", "svegliati", "sveglia"}   # "" = solo "ehi Jarvis"


class Attenzione:
    def __init__(self) -> None:
        self.dorme = False
        self.attivo_fino = 0.0

    def finestra_aperta(self, ora: float) -> bool:
        return not self.dorme and ora < self.attivo_fino

    def apri_finestra(self, ora: float) -> None:
        self.attivo_fino = ora + FINESTRA_ASCOLTO

    def chiudi_finestra(self) -> None:
        self.attivo_fino = 0.0

    def valuta(self, frase: str, in_finestra: bool) -> str:
        """Restituisce: 'ignora', 'dormi', 'sveglia', 'attesa' oppure 'comando'."""
        chiamato = rivolta_a_jarvis(frase)
        testo = _normalizza(frase)
        if self.dorme:
            if chiamato and testo in COMANDI_SVEGLIA:
                self.dorme = False
                return "sveglia"
            if chiamato and testo in COMANDI_USCITA:
                return "comando"
            return "ignora"
        if (chiamato or in_finestra) and testo in COMANDI_DORMI:
            self.dorme = True
            self.chiudi_finestra()
            return "dormi"
        if chiamato and testo == "":
            return "attesa"
        if chiamato or in_finestra:
            return "comando"
        return "ignora"


# ==========================================================================
# MAIN
# ==========================================================================

_blocco_istanza = None   # tenuto aperto per tutta la vita del processo


def _unica_istanza() -> bool:
    """Impedisce due J.A.R.V.I.S. insieme (senza finestre non ci si accorgerebbe)."""
    global _blocco_istanza
    CARTELLA_JARVIS.mkdir(parents=True, exist_ok=True)
    f = open(CARTELLA_JARVIS / "jarvis.lock", "a+")
    try:
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _blocco_istanza = f
    return True


def main() -> None:
    if not (IS_MAC or IS_WIN):
        sys.exit(f"Sistema '{SISTEMA}' non supportato: J.A.R.V.I.S. gira su macOS e Windows.")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        if IS_WIN:
            aiuto = 'Esegui:  setx ANTHROPIC_API_KEY "sk-ant-..."   e poi riapri il terminale.'
        else:
            aiuto = 'Esegui:  export ANTHROPIC_API_KEY="sk-ant-..."'
        sys.exit(f"ANTHROPIC_API_KEY non impostata.\n{aiuto}")

    if not _unica_istanza():
        print("[J.A.R.V.I.S. e' gia' in funzione: questa seconda copia si chiude]")
        return

    WORKSPACE.mkdir(parents=True, exist_ok=True)

    global URL_HUD
    URL_HUD = avvia_hud()
    if URL_HUD:
        avvia_monitor_sistema()

    configura_claude_code()
    if _neurale_attiva:
        threading.Thread(target=prepara_frasi_fisse, daemon=True).start()

    client = anthropic.Anthropic()
    orecchie = Orecchie()
    scambi: list[list[dict]] = []
    attenzione = Attenzione()

    if IS_MAC:
        voce = VOICE if VOCE_OK else "predefinita di sistema"
    else:
        voce = VOCE_WIN or "predefinita di sistema (nessuna voce italiana installata)"
    if _neurale_attiva:
        voce = f"{VOCE_NEURALE} (neurale), riserva: {voce}"
    elif not NEURALE_INSTALLATA:
        voce += "  [per la voce neurale: pip install edge-tts miniaudio]"

    print(f"[Sistema: {NOME_MACCHINA}]")
    print(f"[Modello: {MODEL}]")
    print(f"[Voce: {voce}]")
    print(f"[Cartella di lavoro: {WORKSPACE}]")
    print(f"[Strumenti: {', '.join(ESECUTORI)}, web_search]")
    print(f"[Rispondo alle frasi con 'Jarvis' e, per {FINESTRA_ASCOLTO} secondi dopo ogni risposta, "
          "anche senza. 'Jarvis, dormi' per la pausa.]")
    parla(f"{saluto()}. Tutti i sistemi sono operativi.")

    while True:
        try:
            prossimo = LAVORO.prossimo_avviso_completo()
            if prossimo:
                parlato, completo = prossimo
                HUD.aggiungi("claude", completo)
                suona("attivo")
                parla(parlato)
                attenzione.apri_finestra(time.monotonic())
                continue

            TEMPI.clear()
            ora = time.monotonic()
            in_finestra = attenzione.finestra_aperta(ora)
            # In finestra si aspetta solo il tempo rimasto: una frase iniziata dopo
            # la scadenza non deve passare senza "Jarvis".
            attesa = max(1.0, attenzione.attivo_fino - ora) if in_finestra else None
            if attenzione.dorme:
                HUD.imposta("dorme")
            else:
                HUD.imposta("attento" if in_finestra else "ascolto")
            frase = orecchie.ascolta(attesa)
            if not frase:
                if in_finestra:
                    attenzione.chiudi_finestra()
                continue

            azione = attenzione.valuta(frase, in_finestra)
            if azione == "ignora":
                print("[Ignorata: in pausa]" if attenzione.dorme else "[Ignorata: manca 'Jarvis']")
                continue
            HUD.aggiungi("utente", frase)
            if azione == "dormi":
                suona("riposo")
                parla("Modalita' riposo. Mi chiami quando serve.")
                continue
            if azione == "sveglia":
                suona("attivo")
                apri_hud()
                parla("Di nuovo operativo.")
            elif azione == "attesa":
                suona("attivo")
                apri_hud()
                parla("Mi dica.")
            elif LAVORO.ha_attesa(time.monotonic()) and _normalizza(frase) in COMANDI_CONFERMA:
                # La conferma la riconosce J.A.R.V.I.S., non il modello: una pagina web
                # letta durante una ricerca non puo' far partire un lavoro da sola.
                parla(LAVORO.avvia())
            elif LAVORO.ha_attesa(time.monotonic()) and _normalizza(frase) in COMANDI_ANNULLA:
                LAVORO.annulla()
                parla("Annullato.")
            elif _normalizza(frase) in COMANDI_RIPRISTINO:
                # Anche il ripristino lo decide J.A.R.V.I.S., non il modello, e chiede conferma.
                parla(LAVORO.prepara_ripristino(time.monotonic()))
            else:
                LAVORO.annulla()   # qualunque altra frase lascia cadere il lavoro in sospeso
                if not comando_locale(frase, scambi):
                    parla(chiedi_a_claude(client, scambi, frase))
                    print(riepilogo_tempi())
            attenzione.apri_finestra(time.monotonic())
        except Spegnimento:
            parla("Disattivazione dei sistemi. A presto, Signore.")
            HUD.imposta("spento")
            time.sleep(1.0)   # lascia alla pagina il tempo di mostrare lo spegnimento
            return
        except KeyboardInterrupt:
            print("\n[Interruzione manuale]")
            return


def saluto(ora: int | None = None) -> str:
    ora = datetime.now().hour if ora is None else ora
    if 5 <= ora < 13:
        return "Buongiorno"
    if 13 <= ora < 18:
        return "Buon pomeriggio"
    return "Buonasera"


def imposta_credito_da_riga_di_comando(argomenti: list[str]) -> bool:
    """py jarvis.py --credito 5  ->  registra il credito attuale (quello letto nella Console)."""
    if "--credito" not in argomenti:
        return False
    i = argomenti.index("--credito")
    try:
        dollari = float(argomenti[i + 1].replace(",", "."))
        if dollari < 0:
            raise ValueError
    except (IndexError, ValueError):
        sys.exit("Uso: py jarvis.py --credito 5   (il credito in dollari che vedi nella Console)")
    CONSUMI.imposta_credito(dollari)
    print(f"Credito impostato a {dollari:.2f} $. Da ora il residuo mostrato e' una stima "
          "basata sui consumi di Jarvis.")
    return True


def avvia() -> None:
    """Senza finestre un errore all'avvio sarebbe invisibile: lo si dice a voce."""
    if imposta_credito_da_riga_di_comando(sys.argv[1:]):
        return
    try:
        main()
    except SystemExit as e:
        if SENZA_CONSOLE and isinstance(e.code, str):
            print(e.code)
            parla("Non riesco ad avviarmi. I dettagli sono nel file jarvis punto log, "
                  "nella cartella Jarvis.")
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        if SENZA_CONSOLE:
            parla("Si e' verificato un errore grave. I dettagli sono nel file jarvis punto log.")
        raise


if __name__ == "__main__":
    avvia()
