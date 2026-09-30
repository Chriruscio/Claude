#!/usr/bin/env python3
"""
Installazione di J.A.R.V.I.S. su Windows e su Mac, in un colpo solo:
  1. installa i pacchetti Python necessari;
  2. controlla la chiave API (su Mac la salva nel Portachiavi);
  3. crea l'icona "Jarvis" (Windows: sul desktop; Mac: app in Applicazioni e sul
     desktop), che lo avvia senza finestre;
  4. a richiesta, lo fa partire da solo all'accensione;
  5. a richiesta, attiva il riconoscimento vocale sul computer (Whisper).

Avvio (dalla cartella di jarvis.py):
    py -3.13 installa.py        (Windows)
    python3 installa.py         (Mac)

Rilanciarlo e' innocuo: ricrea le icone e reinstalla solo cio' che manca.
"""

from __future__ import annotations

import os
import platform
import struct
import subprocess
import sys
import zlib
from pathlib import Path

PACCHETTI_COMUNI = ["anthropic", "SpeechRecognition", "pyaudio", "edge-tts", "miniaudio", "psutil", "pillow"]
PACCHETTI = PACCHETTI_COMUNI + (["pywin32"] if platform.system() == "Windows" else [])
CARTELLA_CODICE = Path(__file__).resolve().parent
CARTELLA_JARVIS = Path.home() / "Jarvis"          # fuori dalla cartella del codice: sopravvive agli aggiornamenti
FILE_ICONA = CARTELLA_JARVIS / "jarvis.ico"

# ==========================================================================
# ICONA: disegnata qui, senza file esterni (anello e nucleo azzurri)
# ==========================================================================

def _png(larghezza: int, altezza: int, pixel) -> bytes:
    def blocco(tipo: bytes, dati: bytes) -> bytes:
        return struct.pack(">I", len(dati)) + tipo + dati + struct.pack(">I", zlib.crc32(tipo + dati))

    righe = b"".join(b"\x00" + bytes(riga) for riga in pixel)
    return (
        b"\x89PNG\r\n\x1a\n"
        + blocco(b"IHDR", struct.pack(">IIBBBBB", larghezza, altezza, 8, 6, 0, 0, 0))
        + blocco(b"IDAT", zlib.compress(righe, 9))
        + blocco(b"IEND", b"")
    )


def disegna_icona(lato: int = 256) -> bytes:
    """PNG RGBA: disco scuro, anello azzurro, nucleo luminoso. Bordi sfumati."""
    c = (lato - 1) / 2
    pixel = []
    for y in range(lato):
        riga = []
        for x in range(lato):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5 / (lato / 2)   # 0 al centro, 1 al bordo
            copertura = max(0.0, min(1.0, (0.98 - d) * lato / 2))    # antialias del bordo esterno
            r, g, b = 4, 14, 26                                         # fondo blu notte
            anello = max(0.0, 1 - abs(d - 0.72) / 0.07)
            nucleo = max(0.0, 1 - d / 0.34) ** 1.5
            alone = max(0.0, 1 - abs(d - 0.72) / 0.2) * 0.35
            luce = min(1.0, anello + nucleo + alone)
            r = int(r + (69 - r) * luce + 90 * nucleo * 0.6)
            g = int(g + (212 - g) * luce + 40 * nucleo * 0.6)
            b = int(b + (255 - b) * luce)
            riga += [min(255, r), min(255, g), min(255, b), int(255 * copertura)]
        pixel.append(riga)
    return _png(lato, lato, pixel)


def icona_ico(png: bytes) -> bytes:
    """File .ico con un'unica immagine PNG 256x256 (formato accettato da Windows Vista in poi)."""
    intestazione = struct.pack("<HHH", 0, 1, 1)
    voce = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 6 + 16)   # 0 = 256 pixel
    return intestazione + voce + png


# ==========================================================================
# PASSI DELL'INSTALLAZIONE
# ==========================================================================

def installa_pacchetti() -> bool:
    print("\n[1/4] Installo i pacchetti Python (puo' volerci un minuto)...")
    esito = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *PACCHETTI])
    if esito.returncode != 0:
        print("  Qualcosa e' andato storto: leggi il messaggio qui sopra.")
        if platform.system() == "Darwin":
            print("  Sul Mac, se l'errore riguarda pyaudio, servono gli strumenti di Apple:")
            print("    xcode-select --install     e poi rilancia questo programma.")
        return False
    print("  Fatto.")
    return True


def controlla_chiave() -> None:
    print("\n[2/4] Chiave API...")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  Presente.")
    else:
        print("  NON trovata. Jarvis non partira' finche' non la imposti:")
        print('  [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", (Read-Host "Incolla la chiave"), "User")')


def pythonw() -> Path:
    """pythonw.exe della stessa installazione di Python: avvia senza finestra nera."""
    candidato = Path(sys.executable).with_name("pythonw.exe")
    return candidato if candidato.exists() else Path(sys.executable)


def crea_collegamento(dove: str) -> Path | None:
    """
    dove: 'Desktop' oppure 'Startup' (cartella Esecuzione automatica).
    Il collegamento si crea da Python con il componente WScript.Shell di Windows
    (tramite pywin32), non con un comando PowerShell: quello veniva bloccato con
    "Accesso negato", probabilmente dall'antivirus, perche' creare collegamenti
    da PowerShell e' una tecnica tipica dei programmi malevoli.
    """
    try:
        import pywintypes
        import win32com.client
    except ImportError:
        print("  Manca pywin32. Esegui:  py -3.13 -m pip install pywin32  e rilancia.")
        return None
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        percorso = Path(shell.SpecialFolders(dove)) / "Jarvis.lnk"
        collegamento = shell.CreateShortcut(str(percorso))
        collegamento.TargetPath = str(pythonw())
        collegamento.Arguments = f'"{CARTELLA_CODICE / "jarvis.py"}"'
        collegamento.WorkingDirectory = str(CARTELLA_CODICE)
        collegamento.IconLocation = str(FILE_ICONA)
        collegamento.Description = "J.A.R.V.I.S. - assistente vocale"
        collegamento.Save()
    except (pywintypes.com_error, OSError) as e:
        print(f"  Impossibile creare il collegamento: {e}")
        return None
    return percorso


def crea_icone() -> None:
    print("\n[3/4] Icona sul desktop...")
    CARTELLA_JARVIS.mkdir(parents=True, exist_ok=True)
    FILE_ICONA.write_bytes(icona_ico(disegna_icona()))
    percorso = crea_collegamento("Desktop")
    if percorso:
        print(f"  Creata: {percorso}")
        print("  Doppio clic per avviare Jarvis. Per spegnerlo: \"Jarvis, spegniti\".")


def chiedi_avvio_automatico() -> None:
    print("\n[4/4] Avvio automatico all'accensione del PC")
    print("  Se attivo, Jarvis parte da solo e ascolta sempre il microfono (in attesa di 'Jarvis').")
    risposta = input("  Vuoi attivarlo? Scrivi s oppure n e premi Invio: ").strip().lower()
    if risposta in ("s", "si", "sì", "y", "yes"):
        percorso = crea_collegamento("Startup")
        if percorso:
            print(f"  Attivato. Per toglierlo cancella questo file: {percorso}")
    else:
        print("  Non attivato. Puoi rilanciare questo programma quando vuoi.")


# ==========================================================================
# MAC
# ==========================================================================

SERVIZIO_PORTACHIAVI = "jarvis-anthropic-api-key"   # lo stesso che legge jarvis.py
APP_MAC = Path.home() / "Applications" / "Jarvis.app"
AGENTE_AVVIO_MAC = Path.home() / "Library" / "LaunchAgents" / "it.jarvis.assistente.plist"
FILE_IMPOSTAZIONI_MAC = CARTELLA_JARVIS / "impostazioni.sh"   # variabili lette dall'app all'avvio


def prepara_portaudio_mac() -> bool:
    """pyaudio sul Mac va compilato e ha bisogno di portaudio, che si installa con Homebrew."""
    brew = next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if Path(b).exists()), None)
    if brew is None:
        print("\n  Serve Homebrew per installare il componente del microfono (portaudio).")
        print("  Installalo seguendo le istruzioni su https://brew.sh e poi rilancia questo programma.")
        return False
    print("\n[0/4] Installo portaudio con Homebrew (serve al microfono)...")
    return subprocess.run([brew, "install", "portaudio"]).returncode == 0


def chiave_nel_portachiavi_mac() -> None:
    print("\n[2/4] Chiave API...")
    utente = os.environ.get("USER", "")
    trovata = subprocess.run(["security", "find-generic-password", "-a", utente, "-s", SERVIZIO_PORTACHIAVI],
                             capture_output=True).returncode == 0
    if trovata and input("  C'e' gia' una chiave nel Portachiavi. La sostituisco? (s/n): ").strip().lower() not in ("s", "si", "sì"):
        print("  Tengo quella che c'e'.")
        return
    print("  Incolla la chiave API (non si vede mentre scrivi), premi Invio, poi incollala di nuovo.")
    # -w senza valore: security la chiede lui, cosi' la chiave non passa tra gli argomenti
    esito = subprocess.run(["security", "add-generic-password", "-U", "-a", utente,
                            "-s", SERVIZIO_PORTACHIAVI, "-w"])
    print("  Salvata nel Portachiavi." if esito.returncode == 0 else "  Salvataggio non riuscito.")


def _icona_mac(cartella_risorse: Path) -> bool:
    """Icona .icns generata con iconutil dalle stesse immagini dell'icona di Windows."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        insieme = Path(tmp) / "jarvis.iconset"
        insieme.mkdir()
        for lato in (128, 256, 512):
            (insieme / f"icon_{lato}x{lato}.png").write_bytes(disegna_icona(lato))
        esito = subprocess.run(["iconutil", "-c", "icns", str(insieme), "-o",
                                str(cartella_risorse / "jarvis.icns")], capture_output=True)
        return esito.returncode == 0


def crea_app_mac() -> None:
    print("\n[3/4] App Jarvis...")
    contenuti = APP_MAC / "Contents"
    (contenuti / "MacOS").mkdir(parents=True, exist_ok=True)
    (contenuti / "Resources").mkdir(parents=True, exist_ok=True)
    CARTELLA_JARVIS.mkdir(parents=True, exist_ok=True)
    ha_icona = _icona_mac(contenuti / "Resources")
    (contenuti / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Jarvis</string>
  <key>CFBundleIdentifier</key><string>it.jarvis.assistente</string>
  <key>CFBundleExecutable</key><string>Jarvis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  {'<key>CFBundleIconFile</key><string>jarvis</string>' if ha_icona else ''}
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Jarvis ascolta i comandi vocali.</string>
</dict></plist>
""", encoding="utf-8")
    # Niente Terminale: l'app lancia Python direttamente e scrive i messaggi nel registro.
    avvio = contenuti / "MacOS" / "Jarvis"
    avvio.write_text(f"""#!/bin/bash
[ -f "$HOME/Jarvis/impostazioni.sh" ] && source "$HOME/Jarvis/impostazioni.sh"
export JARVIS_SENZA_CONSOLE=1
cd "{CARTELLA_CODICE}"
exec "{sys.executable}" "{CARTELLA_CODICE / 'jarvis.py'}" >> "$HOME/Jarvis/jarvis.log" 2>&1
""", encoding="utf-8")
    avvio.chmod(0o755)
    scrivania = Path.home() / "Desktop" / "Jarvis.app"
    if not scrivania.exists():
        scrivania.symlink_to(APP_MAC)
    print(f"  Creata: {APP_MAC} (e un collegamento sulla Scrivania).")
    print("  Al primo avvio il Mac chiedera' il permesso per il microfono: rispondi Consenti.")


def chiedi_avvio_automatico_mac() -> None:
    print("\n[4/4] Avvio automatico all'accensione del Mac")
    print("  Se attivo, Jarvis parte da solo e ascolta sempre il microfono (in attesa di 'Jarvis').")
    risposta = input("  Vuoi attivarlo? Scrivi s oppure n e premi Invio: ").strip().lower()
    if risposta not in ("s", "si", "sì", "y", "yes"):
        AGENTE_AVVIO_MAC.unlink(missing_ok=True)
        print("  Non attivato.")
        return
    AGENTE_AVVIO_MAC.parent.mkdir(parents=True, exist_ok=True)
    AGENTE_AVVIO_MAC.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>it.jarvis.assistente</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>{APP_MAC}</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
""", encoding="utf-8")
    print(f"  Attivato dal prossimo accesso. Per toglierlo cancella: {AGENTE_AVVIO_MAC}")


def imposta_variabile_mac(nome: str, valore: str) -> None:
    righe = []
    if FILE_IMPOSTAZIONI_MAC.exists():
        righe = [r for r in FILE_IMPOSTAZIONI_MAC.read_text(encoding="utf-8").splitlines()
                 if not r.startswith(f"export {nome}=")]
    righe.append(f"export {nome}={valore}")
    FILE_IMPOSTAZIONI_MAC.parent.mkdir(parents=True, exist_ok=True)
    FILE_IMPOSTAZIONI_MAC.write_text("\n".join(righe) + "\n", encoding="utf-8")


def installa_mac() -> None:
    print(f"Installazione di J.A.R.V.I.S. (Mac) dalla cartella {CARTELLA_CODICE}")
    if not prepara_portaudio_mac() or not installa_pacchetti():
        return
    chiave_nel_portachiavi_mac()
    chiedi_whisper()
    crea_app_mac()
    chiedi_avvio_automatico_mac()
    print("\nTutto pronto. Apri Jarvis dalla Scrivania o da Applicazioni.")
    print("Permessi che il Mac chiedera' man mano: Microfono; Registrazione schermo (per")
    print("'cosa c'e' sul mio schermo'); Automazione (per chiudere app, musica e calendario).")


def chiedi_whisper() -> None:
    print("\n[Facoltativo] Riconoscimento vocale sul PC (Whisper)")
    print("  L'audio non va piu' a Google e 'Jarvis' si riconosce meglio. Richiede circa")
    print("  500 MB (scaricati al primo avvio) e usa il processore mentre trascrive.")
    risposta = input("  Vuoi attivarlo? Scrivi s oppure n e premi Invio: ").strip().lower()
    if risposta not in ("s", "si", "sì", "y", "yes"):
        print("  Resta Google. Puoi cambiare idea rilanciando questo programma.")
        imposta_variabile("JARVIS_STT", "google")
        return
    esito = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "faster-whisper"])
    if esito.returncode != 0:
        print("  Installazione di Whisper non riuscita: resta Google.")
        return
    imposta_variabile("JARVIS_STT", "whisper")
    print("  Attivato. Vale dal prossimo avvio di Jarvis (per tornare a Google rilancia e rispondi n).")


def imposta_variabile(nome: str, valore: str) -> None:
    """Windows: variabile dell'utente (setx). Mac: file letto dall'app Jarvis all'avvio."""
    if platform.system() == "Windows":
        subprocess.run(["setx", nome, valore], capture_output=True)
    else:
        imposta_variabile_mac(nome, valore)


def main() -> None:
    if platform.system() == "Darwin":
        installa_mac()
        return
    if platform.system() != "Windows":
        sys.exit("Questo programma di installazione e' per Windows e Mac.")
    if sys.version_info[:2] != (3, 13):
        print(f"ATTENZIONE: stai usando Python {sys.version.split()[0]}. "
              "Per il microfono serve la 3.13: rilancia con  py -3.13 installa.py")
        if input("Continuo lo stesso? (s/n): ").strip().lower() not in ("s", "si", "sì"):
            return
    if "--collegamenti" in sys.argv:
        crea_icone()
        chiedi_avvio_automatico()
        return
    print(f"Installazione di J.A.R.V.I.S. dalla cartella {CARTELLA_CODICE}")
    if installa_pacchetti():
        controlla_chiave()
        chiedi_whisper()
        # Nuovo processo: un pacchetto appena installato (pywin32) non sempre e'
        # importabile nel processo che l'ha installato.
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--collegamenti"])
        print("\nTutto pronto.")


if __name__ == "__main__":
    main()
