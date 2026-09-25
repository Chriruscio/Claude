#!/usr/bin/env python3
"""
J.A.R.V.I.S. - assistente vocale per macOS e Windows con controllo del sistema.

Il modello NON esegue comandi arbitrari: puo' solo invocare gli strumenti
dichiarati in TOOLS_LOCALI, e lettura e scrittura file sono confinate a una sandbox.

Requisiti macOS:
    brew install portaudio
    pip install anthropic SpeechRecognition pyaudio edge-tts miniaudio
    export ANTHROPIC_API_KEY="sk-ant-..."

Requisiti Windows (PowerShell):
    pip install anthropic SpeechRecognition pyaudio edge-tts miniaudio
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

# Voce neurale: facoltativa. Se manca, si usa la voce di sistema.
try:
    import asyncio
    import edge_tts
    import miniaudio
    import pyaudio
    NEURALE_INSTALLATA = True
except ImportError:
    NEURALE_INSTALLATA = False


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

HUD_ATTIVO = os.environ.get("JARVIS_HUD", "1") != "0"
HUD_PORTA = int(os.environ.get("JARVIS_HUD_PORTA", "8765"))
FILE_HUD = Path(__file__).resolve().parent / "hud.html"
PHRASE_TIME_LIMIT = 15
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
            }

    def pagina_aperta(self) -> bool:
        with self._lock:
            return time.monotonic() - self.ultimo_contatto < 2.0

    def segna_apertura(self) -> None:
        """Lascia alla pagina appena aperta qualche secondo per collegarsi, senza riaprirla."""
        with self._lock:
            self.ultimo_contatto = time.monotonic() + 5.0


HUD = Hud()
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


async def _sintetizza(testo: str) -> bytes:
    audio = bytearray()
    async for pezzo in edge_tts.Communicate(testo, VOCE_NEURALE).stream():
        if pezzo["type"] == "audio":
            audio += pezzo["data"]
    return bytes(audio)


def _parla_neurale(testo: str) -> None:
    mp3 = asyncio.run(asyncio.wait_for(_sintetizza(testo), timeout=15))
    if not mp3:
        raise RuntimeError("nessun audio ricevuto")
    suono = miniaudio.decode(
        mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000
    )
    uscita = pyaudio.PyAudio()
    try:
        flusso = uscita.open(format=pyaudio.paInt16, channels=1, rate=24000, output=True)
        flusso.write(suono.samples.tobytes())
        flusso.stop_stream()
        flusso.close()
    finally:
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
        self.recognizer.pause_threshold = 0.8
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
        try:
            risultato = self.recognizer.recognize_google(audio, language=STT_LANG, show_all=True)
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
# le voci con lo stesso nome. Sta accanto a jarvis.py, MAI dentro la sandbox:
# se il modello potesse scriverci, potrebbe mettere in lista qualunque programma.
FILE_APP = Path(__file__).resolve().parent / ("app_windows.json" if IS_WIN else "app_mac.json")


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
    return f"File {azione}: {relativo.as_posix()} ({percorso.stat().st_size} byte)"


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


ESECUTORI = {
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
            "Crea o modifica un file di testo nella cartella di lavoro dell'utente. "
            f"Usa percorsi relativi, es. 'note/spesa.md'. Estensioni ammesse: {_ESTENSIONI}. "
            "Non puo' scrivere altrove."
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
        "description": "Legge un file di testo dalla cartella di lavoro dell'utente.",
        "input_schema": {
            "type": "object",
            "properties": {"nome_file": {"type": "string"}},
            "required": ["nome_file"],
        },
    },
    {
        "name": "elenca_file",
        "description": "Elenca i file nella cartella di lavoro o in una sua sottocartella.",
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
        try:
            risposta = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TUTTI_I_TOOLS,
                messages=_messaggi(scambi) + scambio,
            )
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
                parla("Modalita' riposo. Mi chiami quando serve.")
                continue
            if azione == "sveglia":
                apri_hud()
                parla("Di nuovo operativo.")
            elif azione == "attesa":
                apri_hud()
                parla("Mi dica.")
            elif not comando_locale(frase, scambi):
                parla(chiedi_a_claude(client, scambi, frase))
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
