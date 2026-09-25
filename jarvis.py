#!/usr/bin/env python3
"""
J.A.R.V.I.S. - assistente vocale per macOS e Windows con controllo del sistema.

Il modello NON esegue comandi arbitrari: puo' solo invocare gli strumenti
dichiarati in TOOLS_LOCALI, e lettura e scrittura file sono confinate a una sandbox.

Requisiti macOS:
    brew install portaudio
    pip install anthropic SpeechRecognition pyaudio
    export ANTHROPIC_API_KEY="sk-ant-..."

Requisiti Windows (PowerShell):
    pip install anthropic SpeechRecognition pyaudio
    setx ANTHROPIC_API_KEY "sk-ant-..."     (poi riaprire il terminale)

Avvio:
    python3 jarvis.py        (macOS)
    py jarvis.py             (Windows)
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit("Manca il pacchetto 'anthropic'. Esegui: pip install anthropic")

try:
    import speech_recognition as sr
except ImportError:
    sys.exit("Manca 'SpeechRecognition'. Esegui: pip install SpeechRecognition pyaudio")


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

MAX_TOKENS = 1024
MAX_SCAMBI = 6                # scambi completi tenuti in memoria
MAX_RICERCHE_WEB = 3          # tetto per richiesta: ogni ricerca costa ~0,01 $ piu' i token dei risultati
MAX_GIRI_TOOL = 6             # anti-loop sul ciclo di tool use
LISTEN_TIMEOUT = 6
PHRASE_TIME_LIMIT = 15
MAX_BYTE_LETTURA = 20_000     # troncamento in lettura file

WORKSPACE = Path(
    os.environ.get("JARVIS_WORKSPACE", str(Path.home() / "Jarvis" / "workspace"))
).expanduser()

NOME_MACCHINA = "MacBook" if IS_MAC else "PC Windows"

SYSTEM_PROMPT = (
    f"Sei J.A.R.V.I.S., l'intelligenza artificiale integrata nel {NOME_MACCHINA} di Christian. "
    "Rispondi sempre in italiano, con tono formale, efficiente e leggermente sarcastico "
    "ma sempre rispettoso, rivolgendoti all'utente come 'Signore'.\n\n"
    "Le tue risposte vengono lette ad alta voce da un sintetizzatore vocale. Quindi: "
    "massimo tre frasi, prosa continua, niente elenchi puntati, niente markdown, "
    "niente emoji, niente URL letti per esteso (di' 'secondo il sito X'), niente codice.\n\n"
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


def _powershell(script: str, env_extra: dict | None = None, timeout: int = 30):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, errors="replace", timeout=timeout, env=env,
    )


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


def parla(testo: str) -> None:
    testo = (testo or "").strip()
    if not testo:
        return
    print(f"\nJ.A.R.V.I.S.: {testo}")
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

    def ascolta(self) -> str:
        with self.mic as source:
            print("\n[In ascolto... parli pure]")
            try:
                audio = self.recognizer.listen(
                    source, timeout=LISTEN_TIMEOUT, phrase_time_limit=PHRASE_TIME_LIMIT
                )
            except sr.WaitTimeoutError:
                return ""
        try:
            testo = self.recognizer.recognize_google(audio, language=STT_LANG)
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            print(f"[Servizio di trascrizione non raggiungibile: {e}]")
            return ""
        print(f"Tu: {testo}")
        return testo


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


def tool_stato_sistema(cosa: str = "tutto") -> str:
    pezzi = []
    if cosa in ("ora", "tutto"):
        pezzi.append(f"Data e ora: {datetime.now():%d/%m/%Y %H:%M}")
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
            return "La chiave API non e' valida, Signore. Verifichi ANTHROPIC_API_KEY."
        except anthropic.NotFoundError:
            return f"Il modello {MODEL} non risulta disponibile per questo account, Signore."
        except anthropic.RateLimitError:
            return "Ho superato il limite di richieste, Signore. Attenda qualche secondo."
        except anthropic.APIConnectionError:
            return "Non riesco a raggiungere i server, Signore. Controlli la connessione."
        except anthropic.APIStatusError as e:
            print(f"[Errore API {e.status_code}]: {e.message}")
            return "Ho riscontrato un errore tecnico, Signore. I dettagli sono a schermo."

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
            return testo or "Non ho prodotto alcuna risposta, Signore."

        risultati = []
        for blocco in contenuto:
            if getattr(blocco, "type", "") != "tool_use":
                continue
            print(f"[Strumento] {blocco.name} {json.dumps(blocco.input, ensure_ascii=False)}")
            esito = esegui_tool(blocco.name, blocco.input)
            print(f"[Esito] {esito[:200]}")
            risultati.append(
                {"type": "tool_result", "tool_use_id": blocco.id, "content": esito}
            )
        scambio.append({"role": "user", "content": risultati})

    _archivia(scambi, scambio)
    return "Mi sono perso in un ciclo di operazioni, Signore. Riformuli la richiesta."


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
PAROLE_ATTIVAZIONE = {"jarvis", "giarvis", "jervis"}
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
        parla("Memoria della conversazione azzerata, Signore.")
        return True
    return False


# ==========================================================================
# MAIN
# ==========================================================================

def main() -> None:
    if not (IS_MAC or IS_WIN):
        sys.exit(f"Sistema '{SISTEMA}' non supportato: J.A.R.V.I.S. gira su macOS e Windows.")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        if IS_WIN:
            aiuto = 'Esegui:  setx ANTHROPIC_API_KEY "sk-ant-..."   e poi riapri il terminale.'
        else:
            aiuto = 'Esegui:  export ANTHROPIC_API_KEY="sk-ant-..."'
        sys.exit(f"ANTHROPIC_API_KEY non impostata.\n{aiuto}")

    WORKSPACE.mkdir(parents=True, exist_ok=True)

    client = anthropic.Anthropic()
    orecchie = Orecchie()
    scambi: list[list[dict]] = []

    if IS_MAC:
        voce = VOICE if VOCE_OK else "predefinita di sistema"
    else:
        voce = VOCE_WIN or "predefinita di sistema (nessuna voce italiana installata)"

    print(f"[Sistema: {NOME_MACCHINA}]")
    print(f"[Modello: {MODEL}]")
    print(f"[Voce: {voce}]")
    print(f"[Cartella di lavoro: {WORKSPACE}]")
    print(f"[Strumenti: {', '.join(ESECUTORI)}, web_search]")
    print("[Rispondo solo alle frasi che contengono 'Jarvis']")
    parla("Sistemi online. J.A.R.V.I.S. operativo, Signore.")

    while True:
        try:
            frase = orecchie.ascolta()
            if not frase:
                continue
            if not rivolta_a_jarvis(frase):
                print("[Ignorata: manca 'Jarvis']")
                continue
            if comando_locale(frase, scambi):
                continue
            parla(chiedi_a_claude(client, scambi, frase))
        except Spegnimento:
            parla("Disattivazione dei sistemi. Buona giornata, Signore.")
            return
        except KeyboardInterrupt:
            print("\n[Interruzione manuale]")
            return


if __name__ == "__main__":
    main()
