#!/usr/bin/env python3
"""
J.A.R.V.I.S. - assistente vocale per macOS con controllo del sistema.

Il modello NON esegue comandi arbitrari: puo' solo invocare gli strumenti
dichiarati in TOOLS_LOCALI, e la scrittura file e' confinata a una sandbox.

Requisiti:
    brew install portaudio
    pip install anthropic SpeechRecognition pyaudio
    export ANTHROPIC_API_KEY="sk-ant-..."

Avvio:
    python3 jarvis.py
"""

from __future__ import annotations

import json
import os
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

MODEL = os.environ.get("JARVIS_MODEL", "claude-haiku-4-5-20251001")
VOICE = os.environ.get("JARVIS_VOICE", "Alice")
SPEECH_RATE = int(os.environ.get("JARVIS_RATE", "190"))
STT_LANG = "it-IT"

MAX_TOKENS = 1024
MAX_SCAMBI = 6                # scambi completi tenuti in memoria
MAX_RICERCHE_WEB = 3          # tetto per richiesta: ogni ricerca costa ~0,01 $
MAX_GIRI_TOOL = 6             # anti-loop sul ciclo di tool use
LISTEN_TIMEOUT = 6
PHRASE_TIME_LIMIT = 15
MAX_BYTE_LETTURA = 20_000     # troncamento in lettura file

WORKSPACE = Path(
    os.environ.get("JARVIS_WORKSPACE", str(Path.home() / "Jarvis" / "workspace"))
).expanduser()

SYSTEM_PROMPT = (
    "Sei J.A.R.V.I.S., l'intelligenza artificiale integrata nel MacBook di Christian. "
    "Rispondi sempre in italiano, con tono formale, efficiente e leggermente sarcastico "
    "ma sempre rispettoso, rivolgendoti all'utente come 'Signore'.\n\n"
    "Le tue risposte vengono lette ad alta voce da un sintetizzatore vocale. Quindi: "
    "massimo tre frasi, prosa continua, niente elenchi puntati, niente markdown, "
    "niente emoji, niente URL letti per esteso (di' 'secondo il sito X'), niente codice.\n\n"
    "Hai a disposizione degli strumenti per agire sul Mac. Usali quando servono, "
    "senza chiedere conferma per azioni innocue come aprire un'app o leggere un file. "
    "Chiedi conferma a voce prima di sovrascrivere un file gia' esistente.\n\n"
    "Puoi cercare sul web quando la domanda riguarda fatti attuali o che non conosci. "
    "Non cercare per cose che sai gia': ogni ricerca ha un costo. "
    "Se non conosci la risposta e non puoi cercarla, dillo in una frase invece di inventare."
)


class Spegnimento(Exception):
    """Sollevata quando l'utente chiede di terminare la sessione."""


# ==========================================================================
# SINTESI VOCALE (comando nativo macOS 'say')
# ==========================================================================

def _voce_installata(nome: str) -> bool:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    return any(riga.startswith(nome + " ") for riga in out.stdout.splitlines())


VOCE_OK = _voce_installata(VOICE)


def parla(testo: str) -> None:
    testo = (testo or "").strip()
    if not testo:
        return
    print(f"\nJ.A.R.V.I.S.: {testo}")
    cmd = ["say", "-r", str(SPEECH_RATE)]
    if VOCE_OK:
        cmd += ["-v", VOICE]
    cmd.append(testo)
    try:
        subprocess.run(cmd, timeout=180)
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
            sys.exit(
                f"Microfono non disponibile ({e}).\n"
                "Su macOS serve: brew install portaudio && pip install --force-reinstall pyaudio\n"
                "piu' il permesso Microfono per il Terminale in "
                "Impostazioni > Privacy e sicurezza."
            )
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

APP_CONSENTITE = {
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


# ==========================================================================
# SANDBOX FILE
# ==========================================================================

ESTENSIONI_VIETATE = {
    ".command", ".sh", ".bash", ".zsh", ".app", ".scpt",
    ".py", ".pl", ".rb", ".plist", ".dylib", ".pkg", ".dmg",
}


def _percorso_sicuro(nome_file: str) -> Path:
    """Risolve un nome file dentro la sandbox, rifiutando ogni fuga."""
    nome_file = (nome_file or "").strip()
    if not nome_file:
        raise ValueError("Nome file vuoto.")
    if Path(nome_file).is_absolute():
        raise ValueError("I percorsi assoluti non sono consentiti.")
    if ".." in Path(nome_file).parts:
        raise ValueError("I riferimenti alla cartella superiore non sono consentiti.")

    radice = WORKSPACE.resolve()
    candidato = (radice / nome_file).resolve()
    if candidato == radice or radice not in candidato.parents:
        raise ValueError("Percorso fuori dalla cartella di lavoro consentita.")
    if candidato.suffix.lower() in ESTENSIONI_VIETATE:
        raise ValueError(f"Estensione {candidato.suffix} non consentita.")
    return candidato


# ==========================================================================
# IMPLEMENTAZIONE DEGLI STRUMENTI
# ==========================================================================

def tool_apri_app(nome: str) -> str:
    app = APP_CONSENTITE.get((nome or "").lower())
    if not app:
        return f"App '{nome}' non presente nella whitelist."
    esito = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=20)
    if esito.returncode == 0:
        return f"{app} aperta."
    return f"Impossibile aprire {app}: {esito.stderr.strip() or 'app non installata'}"


def tool_chiudi_app(nome: str) -> str:
    app = APP_CONSENTITE.get((nome or "").lower())
    if not app:
        return f"App '{nome}' non presente nella whitelist."
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
    return f"File {azione}: {relativo} ({percorso.stat().st_size} byte)"


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
            base = _percorso_sicuro(sottocartella)
        except ValueError as e:
            return f"Rifiutato: {e}"
    if not base.is_dir():
        return "Cartella non trovata."
    voci = sorted(p.name + ("/" if p.is_dir() else "") for p in base.iterdir())
    return "\n".join(voci) if voci else "(cartella vuota)"


def tool_stato_sistema(cosa: str = "tutto") -> str:
    pezzi = []
    if cosa in ("ora", "tutto"):
        pezzi.append(f"Data e ora: {datetime.now():%d/%m/%Y %H:%M}")
    if cosa in ("batteria", "tutto"):
        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10)
            pezzi.append("Batteria: " + out.stdout.strip().replace("\n", " "))
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            pezzi.append(f"Batteria non leggibile: {e}")
    if cosa in ("disco", "tutto"):
        try:
            out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=10)
            righe = out.stdout.strip().splitlines()
            pezzi.append("Disco: " + (" ".join(righe[-1].split()) if len(righe) > 1 else "n/d"))
        except (FileNotFoundError, subprocess.SubprocessError) as e:
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

TOOLS_LOCALI = [
    {
        "name": "apri_app",
        "description": "Apre un'applicazione sul Mac. Solo le app in whitelist.",
        "input_schema": {
            "type": "object",
            "properties": {"nome": {"type": "string", "enum": _NOMI_APP}},
            "required": ["nome"],
        },
    },
    {
        "name": "chiudi_app",
        "description": "Chiude un'applicazione aperta sul Mac. Solo le app in whitelist.",
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
            "Usa percorsi relativi, es. 'note/spesa.md'. Non puo' scrivere altrove."
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

        scambio.append({"role": "assistant", "content": risposta.content})

        if risposta.stop_reason != "tool_use":
            testo = "".join(
                b.text for b in risposta.content if getattr(b, "type", "") == "text"
            ).strip()
            _archivia(scambi, scambio)
            return testo or "Non ho prodotto alcuna risposta, Signore."

        risultati = []
        for blocco in risposta.content:
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

PAROLE_USCITA = ("spegniti", "spegnimento", "arrivederci", "esci", "termina sessione")


def comando_locale(frase: str, scambi: list) -> bool:
    testo = frase.lower().strip()
    if any(p in testo for p in PAROLE_USCITA):
        raise Spegnimento
    if "dimentica tutto" in testo or "azzera la memoria" in testo:
        scambi.clear()
        parla("Memoria della conversazione azzerata, Signore.")
        return True
    return False


# ==========================================================================
# MAIN
# ==========================================================================

def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY non impostata.\n"
            'Esegui:  export ANTHROPIC_API_KEY="sk-ant-..."'
        )

    WORKSPACE.mkdir(parents=True, exist_ok=True)

    client = anthropic.Anthropic()
    orecchie = Orecchie()
    scambi: list[list[dict]] = []

    print(f"[Modello: {MODEL}]")
    print(f"[Voce: {VOICE if VOCE_OK else 'predefinita di sistema'}]")
    print(f"[Cartella di lavoro: {WORKSPACE}]")
    print(f"[Strumenti: {', '.join(ESECUTORI)}, web_search]")
    parla("Sistemi online. J.A.R.V.I.S. operativo, Signore.")

    while True:
        try:
            frase = orecchie.ascolta()
            if not frase:
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
