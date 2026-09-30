#!/usr/bin/env python3
"""
Installazione di J.A.R.V.I.S. su Windows, in un colpo solo:
  1. installa i pacchetti Python necessari;
  2. controlla la chiave API;
  3. crea l'icona "Jarvis" sul desktop, che lo avvia senza finestre;
  4. a richiesta, lo fa partire da solo all'accensione del PC.

Avvio (dalla cartella di jarvis.py):
    py -3.13 installa.py

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

PACCHETTI = ["anthropic", "SpeechRecognition", "pyaudio", "edge-tts", "miniaudio", "psutil", "pywin32", "pillow"]
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


def main() -> None:
    if platform.system() != "Windows":
        sys.exit("Questo programma di installazione e' per Windows. Il Mac lo configuriamo a parte.")
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
        # Nuovo processo: un pacchetto appena installato (pywin32) non sempre e'
        # importabile nel processo che l'ha installato.
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--collegamenti"])
        print("\nTutto pronto.")


if __name__ == "__main__":
    main()
