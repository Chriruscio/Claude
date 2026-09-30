"""
Test della logica di J.A.R.V.I.S. che non richiede microfono, voce o API reale.

Avvio:  python3 -m unittest test_jarvis -v      (Windows: py -m unittest test_jarvis -v)
"""

import array
import http.client
import json
import math
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

import jarvis


def _testo(t):
    return SimpleNamespace(type="text", text=t)


def _tool_use(nome, argomenti, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=nome, input=argomenti, id=id_)


def _risposta(stop_reason, *blocchi):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(blocchi),
        usage=SimpleNamespace(server_tool_use=None),
    )


class ClientFinto:
    """Restituisce le risposte preparate e registra i messaggi inviati."""

    def __init__(self, *risposte):
        self._risposte = list(risposte)
        self.chiamate = []
        self.messages = self

    def create(self, **kwargs):
        self.chiamate.append([dict(m) for m in kwargs["messages"]])
        return self._risposte.pop(0)


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._originale = jarvis.WORKSPACE
        jarvis.WORKSPACE = Path(self._tmp.name) / "workspace"
        jarvis.WORKSPACE.mkdir()

    def tearDown(self):
        jarvis.WORKSPACE = self._originale
        self._tmp.cleanup()

    def test_percorso_valido(self):
        p = jarvis._percorso_sicuro("note/spesa.md")
        self.assertEqual(p, jarvis.WORKSPACE.resolve() / "note" / "spesa.md")

    def test_rifiuti(self):
        for nome in [
            "", "/etc/passwd", "\\Windows\\win.ini", "../fuori.txt", "a/../../fuori.txt",
            "C:fuori.txt", "nota.txt:flusso", "script.py", "lancia.command", "senza_estensione",
        ]:
            with self.subTest(nome=nome):
                with self.assertRaises(ValueError):
                    jarvis._percorso_sicuro(nome)

    @unittest.skipIf(os.name == "nt", "i collegamenti simbolici su Windows richiedono privilegi")
    def test_collegamento_simbolico_verso_fuori(self):
        fuori = Path(self._tmp.name) / "segreti"
        fuori.mkdir()
        (fuori / "chiave.txt").write_text("segreto")
        (jarvis.WORKSPACE / "link").symlink_to(fuori)
        with self.assertRaises(ValueError):
            jarvis._percorso_sicuro("link/chiave.txt")

    def test_scrivi_e_leggi(self):
        self.assertIn("creato", jarvis.tool_scrivi_file("a.txt", "uno"))
        self.assertIn("aggiornato", jarvis.tool_scrivi_file("a.txt", "due", "aggiungi"))
        self.assertEqual(jarvis.tool_leggi_file("a.txt"), "uno\ndue")

    def test_elenca_sottocartella(self):
        jarvis.tool_scrivi_file("note/x.md", "ciao")
        self.assertEqual(jarvis.tool_elenca_file("note"), "x.md")
        self.assertIn("Rifiutato", jarvis.tool_elenca_file("../"))


class TestFileApp(unittest.TestCase):
    BASE_WIN = {"blocco note": ("notepad.exe", "notepad.exe")}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cartella = Path(self._tmp.name)
        self._originali = (jarvis.IS_WIN, jarvis.WORKSPACE)
        jarvis.IS_WIN = True
        jarvis.WORKSPACE = self.cartella / "workspace"

    def tearDown(self):
        jarvis.IS_WIN, jarvis.WORKSPACE = self._originali
        self._tmp.cleanup()

    def _file(self, testo, cartella=None):
        cartella = cartella or self.cartella
        cartella.mkdir(parents=True, exist_ok=True)
        p = cartella / "app_windows.json"
        p.write_text(testo, encoding="utf-8")
        return p

    def test_file_assente_usa_la_base(self):
        risultato = jarvis._carica_app(self.BASE_WIN, self.cartella / "manca.json")
        self.assertEqual(risultato, self.BASE_WIN)

    def test_aggiunge_e_sostituisce(self):
        p = self._file('{"Discord": ["discord:", "Discord.exe"], "blocco note": ["notepad.exe", null]}')
        risultato = jarvis._carica_app(self.BASE_WIN, p)
        self.assertEqual(risultato["discord"], ("discord:", "Discord.exe"))
        self.assertEqual(risultato["blocco note"], ("notepad.exe", None))

    def test_json_rotto_usa_la_base(self):
        p = self._file('{\n "discord": ["discord:", "Discord.exe"]\n "x": ["a", "b"]\n}')
        self.assertEqual(jarvis._carica_app(self.BASE_WIN, p), self.BASE_WIN)

    def test_voce_scritta_male_ignorata(self):
        p = self._file('{"buona": ["a.exe", "a.exe"], "cattiva": "a.exe", "vuota": ["", "a.exe"]}')
        risultato = jarvis._carica_app(self.BASE_WIN, p)
        self.assertIn("buona", risultato)
        self.assertNotIn("cattiva", risultato)
        self.assertNotIn("vuota", risultato)

    def test_file_dentro_la_sandbox_ignorato(self):
        p = self._file('{"cmd": ["cmd.exe", "cmd.exe"]}', cartella=jarvis.WORKSPACE)
        self.assertNotIn("cmd", jarvis._carica_app(self.BASE_WIN, p))

    def test_nome_mac_con_virgolette_rifiutato(self):
        jarvis.IS_WIN = False
        self.assertTrue(jarvis._voce_app_valida("Google Chrome"))
        self.assertFalse(jarvis._voce_app_valida('Finder" to do shell script "x'))


class TestSceltaTrascrizione(unittest.TestCase):
    def _r(self, *testi):
        return {"alternative": [{"transcript": t} for t in testi], "final": True}

    def test_preferisce_ipotesi_con_jarvis(self):
        self.assertEqual(jarvis.scegli_trascrizione(self._r("Ehi ya", "Ehi Jarvis")), "Ehi Jarvis")

    def test_senza_jarvis_prende_la_prima(self):
        self.assertEqual(jarvis.scegli_trascrizione(self._r("apri Chrome", "a pri crom")), "apri Chrome")

    def test_risultati_vuoti(self):
        for vuoto in [[], {}, {"alternative": []}, None]:
            with self.subTest(vuoto=vuoto):
                self.assertEqual(jarvis.scegli_trascrizione(vuoto), "")


class TestVoce(unittest.TestCase):
    def test_nome_pronunciabile(self):
        casi = {
            "Sistemi pronti. J.A.R.V.I.S. operativo": "Sistemi pronti. Giarvis operativo",
            "Sono Jarvis, Signore.": "Sono Giarvis, Signore.",
            "JARVIS risponde": "Giarvis risponde",
            "Il signor Jarvisson": "Il signor Jarvisson",   # non tocca parole piu' lunghe
        }
        for testo, atteso in casi.items():
            with self.subTest(testo=testo):
                self.assertEqual(jarvis.per_la_voce(testo), atteso)

    def test_ripiego_sulla_voce_di_sistema(self):
        detto = []
        originali = (jarvis._parla_neurale, jarvis._parla_sistema, jarvis._neurale_attiva)

        def neurale_rotta(testo):
            raise OSError("servizio irraggiungibile")

        jarvis._parla_neurale = neurale_rotta
        jarvis._parla_sistema = detto.append
        jarvis._neurale_attiva = True
        try:
            jarvis.parla("Prova, J.A.R.V.I.S.")
            jarvis.parla("Seconda frase")
            self.assertEqual(detto, ["Prova, Giarvis", "Seconda frase"])
            self.assertFalse(jarvis._neurale_attiva)   # non riprova a ogni frase
        finally:
            jarvis._parla_neurale, jarvis._parla_sistema, jarvis._neurale_attiva = originali


class TestPronuncia(unittest.TestCase):
    def test_parole_inglesi(self):
        self.assertEqual(jarvis.per_la_voce("Ho salvato il file."), "Ho salvato il fàil.")
        self.assertEqual(jarvis.per_la_voce("Download completato"), "dàunlod completato")
        self.assertEqual(jarvis.per_la_voce("la tua e-mail"), "la tua imèil")

    def test_voce_multilingue_non_riscrive(self):
        self.assertEqual(
            jarvis.per_la_voce("J.A.R.V.I.S. ha salvato il file", multilingue=True),
            "Giarvis ha salvato il file",
        )

    def test_non_tocca_parole_simili(self):
        for testo in ["profile", "filetto", "webcam", "file-system"]:
            with self.subTest(testo=testo):
                self.assertEqual(jarvis.per_la_voce(testo), testo)


class TestConsumi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.percorso = Path(self._tmp.name) / "consumi.json"
        self._modello = jarvis.MODEL
        jarvis.MODEL = "claude-haiku-4-5-20251001"

    def tearDown(self):
        jarvis.MODEL = self._modello
        self._tmp.cleanup()

    def _usage(self, entrata, uscita, ricerche=0):
        return SimpleNamespace(
            input_tokens=entrata, output_tokens=uscita,
            cache_creation_input_tokens=None, cache_read_input_tokens=None,
            server_tool_use=SimpleNamespace(web_search_requests=ricerche),
        )

    def test_prezzi(self):
        self.assertEqual(jarvis.prezzi_del_modello("claude-haiku-4-5-20251001"), (1.0, 5.0))
        self.assertIsNone(jarvis.prezzi_del_modello("claude-opus-5-5"))   # non confondere con opus-5

    def test_costo_e_residuo(self):
        c = jarvis.Contatore(self.percorso)
        c.imposta_credito(5.0)
        c.registra(self._usage(2_000, 100))          # 0,002 + 0,0005
        c.registra(self._usage(3_000, 200, ricerche=1))   # 0,003 + 0,001 + 0,01
        r = c.riepilogo()
        self.assertEqual(r["sessione"]["richieste"], 2)
        self.assertEqual(r["sessione"]["token_input"], 5_000)
        self.assertAlmostEqual(r["totale"]["spesa"], 0.0165)
        self.assertAlmostEqual(r["residuo"], 4.9835)

    def test_persistenza_tra_avvii(self):
        c = jarvis.Contatore(self.percorso)
        c.imposta_credito(5.0)
        c.registra(self._usage(1_000_000, 0))
        nuovo = jarvis.Contatore(self.percorso)
        self.assertAlmostEqual(nuovo.riepilogo()["residuo"], 4.0)
        self.assertEqual(nuovo.riepilogo()["sessione"]["richieste"], 0)

    def test_senza_credito(self):
        r = jarvis.Contatore(self.percorso).riepilogo()
        self.assertIsNone(r["residuo"])

    def test_file_rovinato(self):
        self.percorso.write_text("non json", encoding="utf-8")
        self.assertIsNone(jarvis.Contatore(self.percorso).riepilogo()["credito"])

    def test_riga_di_comando(self):
        originale = jarvis.CONSUMI
        jarvis.CONSUMI = jarvis.Contatore(self.percorso)
        try:
            self.assertFalse(jarvis.imposta_credito_da_riga_di_comando([]))
            self.assertTrue(jarvis.imposta_credito_da_riga_di_comando(["--credito", "4,75"]))
            self.assertEqual(jarvis.CONSUMI.credito, 4.75)
            with self.assertRaises(SystemExit):
                jarvis.imposta_credito_da_riga_di_comando(["--credito", "tanti"])
        finally:
            jarvis.CONSUMI = originale


class TestSaluto(unittest.TestCase):
    def test_ore(self):
        self.assertEqual(jarvis.saluto(9), "Buongiorno")
        self.assertEqual(jarvis.saluto(15), "Buon pomeriggio")
        self.assertEqual(jarvis.saluto(22), "Buonasera")
        self.assertEqual(jarvis.saluto(2), "Buonasera")


class TestHudVivo(unittest.TestCase):
    def test_inviluppo(self):
        silenzio = [0] * 2400
        forte = [10000, -10000] * 1200
        livelli = jarvis.inviluppo(silenzio + forte, 24000, 50)
        self.assertEqual(len(livelli), 4)                    # 4800 campioni / 1200 per blocco
        self.assertEqual(livelli[:2], [0.0, 0.0])
        self.assertEqual(livelli[2:], [1.0, 1.0])
        self.assertEqual(jarvis.inviluppo([0] * 100, 24000), [0.0])
        self.assertEqual(jarvis.inviluppo([], 24000), [])

    def test_segnale_sonoro(self):
        dati = jarvis.sintetizza_segnale([(880.0, 0.1)])
        self.assertEqual(len(dati), 2 * int(jarvis.FREQUENZA_AUDIO * 0.1))
        import array
        campioni = array.array("h", dati)
        self.assertEqual(campioni[0], 0)                      # attacco morbido, niente click
        self.assertLess(max(abs(c) for c in campioni), 32767 * 0.2)

    def test_raccogli_sistema_senza_psutil(self):
        originale = jarvis.psutil
        jarvis.psutil = None
        try:
            dati = jarvis.raccogli_sistema()
        finally:
            jarvis.psutil = originale
        self.assertIsNone(dati["cpu"])
        self.assertIsNotNone(dati["disco"])

    def test_voce_e_sistema_nello_stato(self):
        jarvis.HUD.imposta_voce([0.1, 0.9], 1000.0, 50)
        jarvis.HUD.imposta_sistema({"cpu": 12.5})
        dati = jarvis.HUD.istantanea()
        self.assertEqual(dati["voce"]["livelli"], [0.1, 0.9])
        self.assertEqual(dati["sistema"]["cpu"], 12.5)


class TestInstallatore(unittest.TestCase):
    def test_icona_valida(self):
        import struct
        import installa
        png = installa.disegna_icona(32)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", png[16:24]), (32, 32))
        ico = installa.icona_ico(png)
        self.assertEqual(struct.unpack("<HHH", ico[:6]), (0, 1, 1))
        self.assertEqual(ico[22:], png)

    def test_niente_powershell_per_i_collegamenti(self):
        import installa
        self.assertFalse(hasattr(installa, "PS_COLLEGAMENTO"))
        self.assertNotIn("pywin32", installa.PACCHETTI_COMUNI)      # su Mac non esiste
        self.assertEqual("pywin32" in installa.PACCHETTI, installa.platform.system() == "Windows")

    def test_impostazioni_mac(self):
        import installa
        with tempfile.TemporaryDirectory() as tmp:
            originale = installa.FILE_IMPOSTAZIONI_MAC
            installa.FILE_IMPOSTAZIONI_MAC = Path(tmp) / "impostazioni.sh"
            try:
                installa.imposta_variabile_mac("JARVIS_STT", "whisper")
                installa.imposta_variabile_mac("JARVIS_PAUSA", "1.0")
                installa.imposta_variabile_mac("JARVIS_STT", "google")      # sostituisce, non duplica
                testo = installa.FILE_IMPOSTAZIONI_MAC.read_text(encoding="utf-8")
            finally:
                installa.FILE_IMPOSTAZIONI_MAC = originale
        self.assertEqual(testo, "export JARVIS_PAUSA=1.0\nexport JARVIS_STT=google\n")


class TestClaudeCode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.radice = Path(self._tmp.name).resolve()
        self.progetto = self.radice / "sito"
        self.progetto.mkdir()
        self.documenti = self.radice / "documenti"
        self.documenti.mkdir()
        self._originali = (jarvis.CARTELLA_REPORT, jarvis.PROGETTI, jarvis.LAVORO,
                           jarvis.CARTELLA_BACKUP, jarvis.FILE_ULTIMO_LAVORO)
        jarvis.CARTELLA_REPORT = self.radice / "report"
        jarvis.CARTELLA_BACKUP = self.radice / "backup"
        jarvis.FILE_ULTIMO_LAVORO = jarvis.CARTELLA_BACKUP / "ultimo_lavoro.json"
        jarvis.PROGETTI = {
            "sito": {"percorso": self.progetto, "sola_lettura": False},
            "documenti": {"percorso": self.documenti, "sola_lettura": True},
        }
        jarvis.LAVORO = jarvis.LavoroClaudeCode()

    def tearDown(self):
        (jarvis.CARTELLA_REPORT, jarvis.PROGETTI, jarvis.LAVORO,
         jarvis.CARTELLA_BACKUP, jarvis.FILE_ULTIMO_LAVORO) = self._originali
        self._tmp.cleanup()

    def _esegui_sincrono(self, lavoro, progetto, compito):
        dati = jarvis.PROGETTI[progetto]
        lavoro.prepara(progetto, dati["percorso"], compito, 0, dati["sola_lettura"])
        dettagli = lavoro.in_attesa
        dettagli["inizio"] = 0
        lavoro.in_corso = dettagli
        lavoro._esegui(dettagli)          # sincrono, senza thread
        return lavoro.prossimo_avviso()

    def test_cartelle_vietate(self):
        self.assertIsNone(jarvis.progetto_non_valido(self.progetto))
        self.assertIsNotNone(jarvis.progetto_non_valido(Path("relativo/sito")))
        self.assertIsNotNone(jarvis.progetto_non_valido(self.radice / "non-esiste"))
        self.assertIsNotNone(jarvis.progetto_non_valido(Path.home()))
        self.assertIsNotNone(jarvis.progetto_non_valido(Path(self.radice.anchor)))
        # la cartella del codice di J.A.R.V.I.S., una sua sottocartella o una che la contiene
        self.assertIsNotNone(jarvis.progetto_non_valido(jarvis.CARTELLA_CODICE_JARVIS))
        self.assertIsNotNone(jarvis.progetto_non_valido(jarvis.CARTELLA_CODICE_JARVIS.parent))

    def test_carica_progetti(self):
        f = self.radice / "progetti.json"
        f.write_text(json.dumps({
            "Sito": str(self.progetto),
            "Documenti": {"percorso": str(self.documenti), "sola_lettura": True},
            "jarvis": str(jarvis.CARTELLA_CODICE_JARVIS),
            "vuoto": "",
        }), encoding="utf-8")
        self.assertEqual(jarvis.carica_progetti(f), {
            "sito": {"percorso": self.progetto, "sola_lettura": False},
            "documenti": {"percorso": self.documenti, "sola_lettura": True},
        })
        self.assertEqual(jarvis.carica_progetti(self.radice / "manca.json"), {})

    def test_sola_lettura_senza_strumenti_di_modifica(self):
        argomenti = jarvis.comando_claude_code("claude", sola_lettura=True)
        strumenti = argomenti[argomenti.index("--tools") + 1]
        self.assertEqual(strumenti, "Read,Glob,Grep")
        self.assertIn(jarvis.ISTRUZIONE_LETTURA, argomenti)

    def test_comando_senza_testo_del_modello_e_senza_chiave(self):
        argomenti = jarvis.comando_claude_code("claude")
        for obbligatorio in ["--restricted", "--permission-prompts", "none", "--tools",
                             "Read,Edit,Write,Glob,Grep", "--output-format", "json"]:
            self.assertIn(obbligatorio, argomenti)
        self.assertNotIn("Bash", " ".join(argomenti))
        originale = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-prova"
        try:
            self.assertNotIn("ANTHROPIC_API_KEY", jarvis.ambiente_claude_code())
        finally:
            if originale is None:
                del os.environ["ANTHROPIC_API_KEY"]
            else:
                os.environ["ANTHROPIC_API_KEY"] = originale

    def test_prepara_non_avvia(self):
        esito = jarvis.tool_claude_code("sito", "aggiungi un titolo alla home")
        self.assertIn("NON avviato", esito)
        self.assertTrue(jarvis.LAVORO.ha_attesa(0))
        self.assertIsNone(jarvis.LAVORO.occupato())
        self.assertIn("non presente", jarvis.tool_claude_code("altro", "x"))

    def test_conferma_senza_nome_fuori_finestra(self):
        a = jarvis.Attenzione()
        self.assertFalse(jarvis.risposta_a_conferma("conferma", a, 0))      # niente in sospeso
        jarvis.tool_claude_code("sito", "compito")
        adesso = jarvis.time.monotonic()
        self.assertTrue(jarvis.risposta_a_conferma("Conferma", a, adesso))
        self.assertTrue(jarvis.risposta_a_conferma("annulla", a, adesso))
        self.assertFalse(jarvis.risposta_a_conferma("confermi che piove?", a, adesso))
        a.dorme = True
        self.assertFalse(jarvis.risposta_a_conferma("conferma", a, adesso))

    def test_conferma_scaduta(self):
        jarvis.tool_claude_code("sito", "compito")
        self.assertFalse(jarvis.LAVORO.ha_attesa(10 ** 12))

    def test_esecuzione_e_avviso(self):
        chiamate = []

        def finto_run(argomenti, **opzioni):
            chiamate.append((argomenti, opzioni))
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"result": "Ho aggiunto il titolo in index.html.", "is_error": False, "total_cost_usd": 0.12}))

        lavoro = jarvis.LavoroClaudeCode(esegui=finto_run)
        avviso = self._esegui_sincrono(lavoro, "sito", "aggiungi un titolo")
        argomenti, opzioni = chiamate[0]
        self.assertEqual(opzioni["input"], "aggiungi un titolo")   # il compito passa da stdin
        self.assertNotIn("aggiungi un titolo", argomenti)
        self.assertEqual(opzioni["cwd"], str(self.progetto))
        self.assertIn("ha finito il lavoro su sito", avviso)
        self.assertIn("index.html", avviso)
        self.assertIn("annulla l'ultimo lavoro", avviso)
        self.assertIsNone(lavoro.occupato())
        self.assertEqual(len(list(jarvis.CARTELLA_REPORT.glob("*.md"))), 1)

    def test_copia_di_sicurezza_e_ripristino(self):
        (self.progetto / "index.html").write_text("originale", encoding="utf-8")
        (self.progetto / "da_cancellare.txt").write_text("resto", encoding="utf-8")

        def lavoro_che_modifica(argomenti, cwd, **opzioni):
            cartella = Path(cwd)
            (cartella / "index.html").write_text("modificato", encoding="utf-8")
            (cartella / "da_cancellare.txt").unlink()
            (cartella / "nuovo.txt").write_text("creato dal lavoro", encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"result": "fatto"}))

        lavoro = jarvis.LavoroClaudeCode(esegui=lavoro_che_modifica)
        self._esegui_sincrono(lavoro, "sito", "modifica")
        self.assertEqual((self.progetto / "index.html").read_text(encoding="utf-8"), "modificato")
        # un file creato dall'utente DOPO il lavoro non deve sparire col ripristino
        (self.progetto / "mio.txt").write_text("mio", encoding="utf-8")

        self.assertIn("Dica conferma", lavoro.prepara_ripristino(0))
        self.assertTrue(lavoro.ha_attesa(0))
        self.assertIn("tornato com'era", lavoro.avvia())
        self.assertEqual((self.progetto / "index.html").read_text(encoding="utf-8"), "originale")
        self.assertTrue((self.progetto / "da_cancellare.txt").exists())
        self.assertFalse((self.progetto / "nuovo.txt").exists())
        self.assertTrue((self.progetto / "mio.txt").exists())
        self.assertEqual(lavoro.prepara_ripristino(0), "Non ho nessun lavoro da annullare.")

    def test_sola_lettura_niente_copia_e_risposta_letta(self):
        lavoro = jarvis.LavoroClaudeCode(esegui=lambda *a, **k: SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({"result": "Il contratto scade a marzo."})))
        avviso = self._esegui_sincrono(lavoro, "documenti", "quando scade il contratto?")
        self.assertEqual(avviso, "Da documenti: Il contratto scade a marzo.")
        self.assertFalse(jarvis.CARTELLA_BACKUP.exists())
        self.assertIsNone(lavoro.ultimo_lavoro())

    def test_progetto_troppo_grande_non_parte(self):
        originale = jarvis.MAX_BYTE_BACKUP
        jarvis.MAX_BYTE_BACKUP = 3
        (self.progetto / "grande.txt").write_text("troppi byte", encoding="utf-8")
        chiamate = []
        lavoro = jarvis.LavoroClaudeCode(esegui=lambda *a, **k: chiamate.append(1))
        try:
            avviso = self._esegui_sincrono(lavoro, "sito", "modifica")
        finally:
            jarvis.MAX_BYTE_BACKUP = originale
        self.assertEqual(chiamate, [])
        self.assertIn("niente copia di sicurezza", avviso)

    def test_errore_riportato(self):
        lavoro = jarvis.LavoroClaudeCode(esegui=lambda *a, **k: SimpleNamespace(
            returncode=1, stderr="", stdout=json.dumps({"result": "Not logged in", "is_error": True})))
        self.assertIn("non e' riuscito", self._esegui_sincrono(lavoro, "sito", "x"))

    def test_configurazione_istruisce_il_modello(self):
        originali = (jarvis.SYSTEM_PROMPT, list(jarvis.TUTTI_I_TOOLS), dict(jarvis.ESECUTORI),
                     jarvis.carica_progetti, jarvis.shutil.which, jarvis.versione_claude,
                     jarvis.ESEGUIBILE_CLAUDE)
        jarvis.carica_progetti = lambda percorso: dict(jarvis.PROGETTI)
        jarvis.shutil.which = lambda nome: "claude"
        jarvis.versione_claude = lambda eseguibile: (2, 1, 285)
        try:
            jarvis.configura_claude_code()
            self.assertIn("claude_code", jarvis.ESECUTORI)
            self.assertIn("documenti (sola lettura), sito (modificabile)", jarvis.SYSTEM_PROMPT)
            self.assertIn("mai a scrivi_file", jarvis.SYSTEM_PROMPT)
        finally:
            (jarvis.SYSTEM_PROMPT, strumenti, esecutori, jarvis.carica_progetti,
             jarvis.shutil.which, jarvis.versione_claude, jarvis.ESEGUIBILE_CLAUDE) = originali
            jarvis.TUTTI_I_TOOLS[:] = strumenti
            jarvis.ESECUTORI.clear()
            jarvis.ESECUTORI.update(esecutori)

    def test_versione(self):
        risposta = SimpleNamespace(stdout="2.1.284 (Claude Code)\n")
        originale = jarvis.subprocess.run
        jarvis.subprocess.run = lambda *a, **k: risposta
        try:
            self.assertEqual(jarvis.versione_claude("claude"), (2, 1, 284))
        finally:
            jarvis.subprocess.run = originale
        self.assertGreaterEqual((2, 1, 284), jarvis.VERSIONE_MINIMA_CLAUDE)


class TestVelocita(unittest.TestCase):
    def test_dividi_frasi(self):
        testo = ("Sono le dieci. Il sole splende su Roma e le previsioni danno bel tempo per "
                 "tutta la settimana, salvo sorprese. Domani ventiquattro gradi. Dopodomani anche. Ok.")
        pezzi = jarvis.dividi_frasi(testo)
        self.assertEqual(" ".join(pezzi), testo)          # nessuna parola persa o duplicata
        self.assertLess(len(pezzi[0]), 120)               # il primo pezzo e' corto: si parte prima
        self.assertTrue(all(len(p) >= 30 for p in pezzi[1:]))
        self.assertEqual(jarvis.dividi_frasi("Ciao."), ["Ciao."])
        self.assertEqual(jarvis.dividi_frasi(""), [])

    def test_memoria_della_voce(self):
        chiamate = []

        async def finta_sintesi(testo, misure=None):
            chiamate.append(testo)
            if misure is not None:
                misure.update(primo=0.4, totale=0.9)
            return b"mp3-finto"

        with tempfile.TemporaryDirectory() as tmp:
            originali = (jarvis.CARTELLA_CACHE_VOCE, jarvis._sintetizza)
            jarvis.CARTELLA_CACHE_VOCE = Path(tmp)
            jarvis._sintetizza = finta_sintesi
            try:
                misure = {}
                self.assertEqual(jarvis.audio_del_pezzo("Mi dica.", misure), b"mp3-finto")
                self.assertEqual(misure, {"primo": 0.4, "totale": 0.9})
                misure = {}
                self.assertEqual(jarvis.audio_del_pezzo("Mi dica.", misure), b"mp3-finto")
                self.assertTrue(misure.get("da_cache"))
                self.assertEqual(chiamate, ["Mi dica."])            # la seconda volta niente servizio
                jarvis.audio_del_pezzo("x" * 200)                   # frasi lunghe: mai in memoria
                self.assertEqual(len(list(Path(tmp).glob("*.mp3"))), 1)
            finally:
                jarvis.CARTELLA_CACHE_VOCE, jarvis._sintetizza = originali

    def test_riepilogo_tempi(self):
        jarvis.TEMPI.clear()
        jarvis.TEMPI.update({"trascrizione": 0.62, "claude": 2.4, "chiamate": 2, "voce": 0.5})
        self.assertEqual(jarvis.riepilogo_tempi(),
                         "[Tempi: trascrizione 0.6 s · Claude 2.4 s (2 chiamate) · voce pronta in 0.5 s]")
        jarvis.TEMPI.clear()
        self.assertEqual(jarvis.riepilogo_tempi(), "")


class TestNovitaDaAltriProgetti(unittest.TestCase):
    def test_pezzi_pronti(self):
        pezzi, resto = jarvis.pezzi_pronti("Sono le 10.30 di sera. Domani piove", 0)
        self.assertEqual(pezzi, ["Sono le 10.30 di sera."])      # "10.30" non viene spezzato
        self.assertEqual(resto, " Domani piove")
        pezzi, resto = jarvis.pezzi_pronti("Ok. Allora", 0)
        self.assertEqual(pezzi, [])                               # troppo corto per partire
        pezzi, _ = jarvis.pezzi_pronti("Frase breve. Altra breve. " * 3, 1)
        self.assertTrue(all(len(p) >= 60 for p in pezzi))

    def test_barriera_di_contaminazione(self):
        aperte = []
        originali = (jarvis._bozza_outlook, jarvis.webbrowser.open)
        jarvis._bozza_outlook = lambda *a: aperte.append("outlook") or True
        jarvis.webbrowser.open = aperte.append
        risultato_web = SimpleNamespace(type="web_search_tool_result")
        client = ClientFinto(
            _risposta("tool_use", risultato_web,
                      _tool_use("prepara_mail", {"oggetto": "x", "testo": "dati", "destinatari": "a@b.it"})),
            _risposta("end_turn", _testo("Ho trovato la pagina.")),
        )
        try:
            jarvis.chiedi_a_claude(client, [], "cerca e manda")
        finally:
            jarvis._bozza_outlook, jarvis.webbrowser.open = originali
        self.assertEqual(aperte, [])                               # nessuna bozza aperta
        esito = client.chiamate[1][-1]["content"][0]["content"]
        self.assertEqual(esito, jarvis.MESSAGGIO_CONTAMINAZIONE)

    def test_senza_contaminazione_la_mail_parte(self):
        originale = jarvis._bozza_outlook
        jarvis._bozza_outlook = lambda *a: True
        client = ClientFinto(
            _risposta("tool_use", _tool_use("prepara_mail", {"oggetto": "x", "testo": "y"})),
            _risposta("end_turn", _testo("Fatto.")),
        )
        try:
            jarvis.chiedi_a_claude(client, [], "prepara una mail")
        finally:
            jarvis._bozza_outlook = originale
        self.assertIn("Bozza aperta", client.chiamate[1][-1]["content"][0]["content"])

    def test_screenshot_non_resta_in_memoria(self):
        originale = jarvis.cattura_schermo
        jarvis.cattura_schermo = lambda: b"jpeg-finto"
        client = ClientFinto(
            _risposta("tool_use", _tool_use("guarda_schermo", {})),
            _risposta("end_turn", _testo("Vedo un foglio di calcolo.")),
        )
        scambi = []
        try:
            jarvis.chiedi_a_claude(client, scambi, "cosa c'e' sullo schermo?")
        finally:
            jarvis.cattura_schermo = originale
        inviato = client.chiamate[1][-1]["content"][0]["content"]
        self.assertEqual(inviato[0]["type"], "image")               # Claude l'ha vista...
        archiviato = scambi[0][2]["content"][0]["content"]
        self.assertNotIn("image", [p["type"] for p in archiviato])  # ...ma non resta in memoria

    def test_apri_sito(self):
        aperti = []
        originale = jarvis.webbrowser.open
        jarvis.webbrowser.open = aperti.append
        try:
            self.assertIn("Aperto", jarvis.tool_apri_sito("https://www.example.com/pagina"))
            for vietato in ["file:///C:/Windows", "javascript:alert(1)", "ftp://x.it", "example.com"]:
                with self.subTest(indirizzo=vietato):
                    self.assertIn("non valido", jarvis.tool_apri_sito(vietato))
        finally:
            jarvis.webbrowser.open = originale
        self.assertEqual(aperti, ["https://www.example.com/pagina"])

    def test_parlato_in_flusso_ordine_e_ripiego(self):
        suonati, di_sistema = [], []

        class FintoFlusso:
            def get_output_latency(self): return 0.0
            def write(self, dati): suonati.append(dati)
            def stop_stream(self): pass
            def close(self): pass

        class FintoPyAudio:
            def open(self, **k): return FintoFlusso()
            def terminate(self): pass

        def finto_audio(pezzo, misure=None):
            if "rotto" in pezzo:
                raise OSError("servizio giu'")
            return pezzo.encode()

        originali = (jarvis.audio_del_pezzo, jarvis.miniaudio, jarvis.pyaudio, jarvis._parla_sistema)
        import array
        jarvis.audio_del_pezzo = finto_audio
        jarvis.miniaudio = SimpleNamespace(
            SampleFormat=SimpleNamespace(SIGNED16=1),
            decode=lambda dati, **k: SimpleNamespace(samples=array.array("h", [100] * 10), dati=dati))
        jarvis.pyaudio = SimpleNamespace(PyAudio=FintoPyAudio, paInt16=8)
        jarvis._parla_sistema = di_sistema.append
        try:
            voce = jarvis.ParlatoInFlusso()
            for parte in ["Prima frase abbastanza lunga. ", "Seconda frase che deve essere ",
                          "almeno di sessanta caratteri per partire. ", "Qui il servizio e' rotto. ",
                          "E questa finisce."]:
                voce.aggiungi(parte)
            self.assertTrue(voce.chiudi())
        finally:
            (jarvis.audio_del_pezzo, jarvis.miniaudio, jarvis.pyaudio, jarvis._parla_sistema) = originali
        self.assertEqual(len(suonati), 2)                 # i primi due pezzi con la voce neurale
        self.assertEqual(len(di_sistema), 1)              # dopo il guasto, il resto con quella di sistema
        self.assertIn("rotto", di_sistema[0])
        self.assertIn("finisce", di_sistema[0])

    def test_risposta_in_flusso(self):
        class Flusso:
            def __init__(self, pezzi, finale):
                self.text_stream, self._finale = iter(pezzi), finale

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return self._finale

        class ClientInFlusso:
            def __init__(self):
                self.messages = self

            def stream(self, **kwargs):
                return Flusso(["Sono le ", "dieci. ", "Buona serata."],
                              _risposta("end_turn", _testo("Sono le dieci. Buona serata.")))

        ricevuto = []
        voce = SimpleNamespace(aggiungi=ricevuto.append)
        testo = jarvis.chiedi_a_claude(ClientInFlusso(), [], "che ore sono", voce)
        self.assertEqual("".join(ricevuto), "Sono le dieci. Buona serata.")
        self.assertEqual(testo, "Sono le dieci. Buona serata.")


class TestApplausoEWhisper(unittest.TestCase):
    F = 16000

    def _silenzio(self, secondi, livello=40):
        import random
        rnd = random.Random(1)
        return [int(rnd.uniform(-livello, livello)) for _ in range(int(self.F * secondi))]

    def _colpo(self):
        import random
        rnd = random.Random(2)
        n = int(self.F * 0.03)   # 30 ms che si spengono in fretta
        return [int(20000 * math.exp(-i / (n / 5)) * rnd.uniform(-1, 1)) for i in range(n)]

    def _voce(self, secondi):
        n = int(self.F * secondi)
        return [int(6000 * math.sin(2 * math.pi * 180 * i / self.F)
                    * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * i / self.F))) for i in range(n)]

    def test_doppio_applauso(self):
        segnale = self._silenzio(0.5) + self._colpo() + self._silenzio(0.3) + self._colpo() + self._silenzio(1.0)
        self.assertTrue(jarvis.rileva_doppio_applauso(segnale))

    def test_non_applausi(self):
        casi = {
            "singolo": self._silenzio(0.5) + self._colpo() + self._silenzio(1.2),
            "triplo": self._silenzio(0.3) + (self._colpo() + self._silenzio(0.25)) * 3 + self._silenzio(0.8),
            "troppo distanti": self._silenzio(0.2) + self._colpo() + self._silenzio(1.2) + self._colpo() + self._silenzio(0.5),
            "voce": self._silenzio(0.3) + self._voce(1.5) + self._silenzio(0.5),
            "silenzio": self._silenzio(2.0),
        }
        for nome, segnale in casi.items():
            with self.subTest(nome):
                self.assertFalse(jarvis.rileva_doppio_applauso(segnale))

    def test_whisper_filtra_allucinazioni(self):
        seg = lambda testo, p=0.1: SimpleNamespace(text=testo, no_speech_prob=p)
        self.assertEqual(jarvis.testo_da_whisper([seg(" Jarvis, che ore sono?")]), "Jarvis, che ore sono?")
        self.assertEqual(jarvis.testo_da_whisper([seg(" Sottotitoli a cura di QTSS")]), "")
        self.assertEqual(jarvis.testo_da_whisper([seg(" Grazie.", p=0.9)]), "")    # probabile silenzio
        self.assertEqual(jarvis.testo_da_whisper([]), "")


class TestVitaQuotidiana(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cartella = Path(self._tmp.name)
        self._originali = (jarvis.FILE_MEMORIA, jarvis.PROMEMORIA)
        jarvis.FILE_MEMORIA = cartella / "memoria.json"
        jarvis.PROMEMORIA = jarvis.Promemoria(cartella / "promemoria.json")

    def tearDown(self):
        jarvis.FILE_MEMORIA, jarvis.PROMEMORIA = self._originali
        self._tmp.cleanup()

    def test_memoria(self):
        self.assertEqual(jarvis.testo_memoria_per_il_prompt(), "")
        jarvis.tool_ricorda("Mia moglie si chiama Anna")
        jarvis.tool_ricorda("Il codice del cancello e' al lavoro")
        self.assertIn("- Mia moglie si chiama Anna", jarvis.testo_memoria_per_il_prompt())
        self.assertIn("Dimenticate 1", jarvis.tool_dimentica("cancello"))
        self.assertNotIn("cancello", jarvis.testo_memoria_per_il_prompt())

    def test_memoria_e_nel_prompt_di_ogni_chiamata(self):
        jarvis.tool_ricorda("Preferisco il caffe' amaro")
        inviato = {}

        class Client:
            def __init__(self):
                self.messages = self

            def create(self, **k):
                inviato.update(k)
                return _risposta("end_turn", _testo("Certo."))

        jarvis.chiedi_a_claude(Client(), [], "come prendo il caffe'?")
        self.assertIn("caffe' amaro", inviato["system"])

    def test_calcola_quando(self):
        from datetime import datetime
        adesso = datetime(2026, 9, 30, 18, 0)
        self.assertEqual(jarvis.calcola_quando(10, None, adesso), datetime(2026, 9, 30, 18, 10))
        self.assertEqual(jarvis.calcola_quando(None, "19:30", adesso), datetime(2026, 9, 30, 19, 30))
        self.assertEqual(jarvis.calcola_quando(None, "7.15", adesso), datetime(2026, 10, 1, 7, 15))  # domani
        for sbagliato in [(0, None), (-5, None), (None, "25:00"), (None, "domani"), (None, None)]:
            with self.subTest(sbagliato):
                self.assertIsNone(jarvis.calcola_quando(*sbagliato, adesso))

    def test_promemoria_scadono_una_volta(self):
        from datetime import datetime, timedelta
        adesso = datetime.now()
        jarvis.PROMEMORIA.aggiungi(adesso - timedelta(seconds=1), "togliere la pasta")
        jarvis.PROMEMORIA.aggiungi(adesso + timedelta(hours=1), "chiamare Mario")
        self.assertEqual(jarvis.PROMEMORIA.scaduti(adesso), ["togliere la pasta"])
        self.assertEqual(jarvis.PROMEMORIA.scaduti(adesso), [])
        self.assertIn("1. ", jarvis.tool_elenca_promemoria())
        self.assertIn("Cancellato: chiamare Mario", jarvis.tool_cancella_promemoria(1))
        self.assertEqual(jarvis.tool_elenca_promemoria(), "Nessun promemoria attivo.")

    def test_audio_azione_sconosciuta(self):
        self.assertIn("sconosciuta", jarvis.tool_controlla_audio("esplodi"))

    def test_meteo(self):
        risposte = iter([
            {"results": [{"name": "Roma", "latitude": 41.9, "longitude": 12.5}]},
            {"current": {"temperature_2m": 21.4, "weather_code": 1, "wind_speed_10m": 8.2},
             "daily": {"time": ["a", "b", "c"], "weather_code": [0, 61, 95],
                       "temperature_2m_min": [14, 13, 12], "temperature_2m_max": [24, 20, 18],
                       "precipitation_probability_max": [0, 70, None]}},
        ])
        originale = jarvis._scarica_json
        jarvis._scarica_json = lambda indirizzo: next(risposte)
        try:
            esito = jarvis.tool_meteo("Roma")
        finally:
            jarvis._scarica_json = originale
        self.assertIn("Roma adesso: poco nuvoloso, 21 gradi", esito)
        self.assertIn("Domani: pioggia leggera, min 13 max 20, pioggia 70%", esito)
        self.assertIn("Dopodomani: temporale", esito)

    def test_agenda_e_memoria_nella_barriera(self):
        self.assertIn("agenda", jarvis.STRUMENTI_CHE_LEGGONO)
        self.assertTrue({"ricorda", "dimentica"} <= jarvis.STRUMENTI_CHE_AGISCONO)


class ModelloParolaFinto:
    """Come openWakeWord: predict() per blocco; qui il punteggio sale ai blocchi indicati."""

    def __init__(self, scatta_a=()):
        self.scatta_a = set(scatta_a)
        self.n = -1
        self.azzerato = 0

    def predict(self, blocco):
        self.n += 1
        return {"hey_jarvis": 0.9 if self.n in self.scatta_a else 0.01}

    def reset(self):
        self.azzerato += 1


def _blocco(livello=0):
    """80 ms di audio: silenzio (0) oppure un'onda quadra dell'ampiezza indicata."""
    return array.array("h", [livello if i % 2 else -livello for i in range(jarvis.BLOCCO_PAROLA)]).tobytes()


class TestParolaLocale(unittest.TestCase):
    VOCE, SILENZIO = _blocco(2000), _blocco(0)

    def test_frase_detta_dopo_il_nome(self):
        # 10 blocchi di silenzio, scatta, poi 0,4 s di silenzio, 1 s di voce, silenzio
        blocchi = [self.SILENZIO] * 11 + [self.SILENZIO] * 5 + [self.VOCE] * 12 + [self.SILENZIO] * 30
        modello = ModelloParolaFinto(scatta_a=[10])
        esito = jarvis.AscoltoParola(modello).aspetta(blocchi, soglia_voce=300, applauso=False)
        self.assertIsInstance(esito, bytes)
        self.assertIn(self.VOCE * 12, esito)
        self.assertEqual(modello.azzerato, 1)   # niente doppio scatto sulla stessa parola

    def test_comando_gia_cominciato_quando_scatta(self):
        # Il modello scatta in ritardo: "ehi Jarvis" (voce), pausa, e il comando e' gia' partito.
        nome, pausa, inizio_comando = [self.VOCE] * 8, [self.SILENZIO] * 2, [_blocco(1500)] * 4
        blocchi = [self.SILENZIO] * 5 + nome + pausa + inizio_comando + [self.VOCE] * 6 + [self.SILENZIO] * 30
        scatto = 5 + 8 + 2 + 4 - 1   # all'ultimo blocco dell'inizio del comando
        esito = jarvis.AscoltoParola(ModelloParolaFinto([scatto])).aspetta(
            blocchi, soglia_voce=300, applauso=False)
        self.assertTrue(esito.startswith(self.SILENZIO + _blocco(1500) * 4))
        self.assertIn(self.VOCE * 6, esito)
        self.assertNotIn(self.VOCE * 8 + self.SILENZIO, esito)   # "ehi Jarvis" resta fuori

    def test_solo_ehi_jarvis(self):
        blocchi = [self.SILENZIO] * 5 + [self.SILENZIO] * 40
        esito = jarvis.AscoltoParola(ModelloParolaFinto([4])).aspetta(blocchi, soglia_voce=300, applauso=False)
        self.assertEqual(esito, b"")

    def test_senza_nome_non_esce_niente(self):
        blocchi = [self.VOCE] * 50   # si parla, ma il modello non scatta mai
        self.assertIsNone(jarvis.AscoltoParola(ModelloParolaFinto()).aspetta(
            blocchi, soglia_voce=300, applauso=False))

    def test_interrotto_da_un_avviso(self):
        chiamate = []

        def interrompi():
            chiamate.append(1)
            return len(chiamate) == 2

        esito = jarvis.AscoltoParola(ModelloParolaFinto()).aspetta(
            iter(lambda: self.SILENZIO, None), interrompi, applauso=False)
        self.assertIsNone(esito)

    def test_doppio_applauso(self):
        colpo = _blocco(8000)
        blocchi = [self.SILENZIO] * 20 + [colpo] + [self.SILENZIO] * 4 + [colpo] + [self.SILENZIO] * 20
        esito = jarvis.AscoltoParola(ModelloParolaFinto()).aspetta(blocchi, soglia_voce=300, applauso=True)
        self.assertEqual(esito, jarvis.APPLAUSO)

    def test_senza_openwakeword_si_torna_a_google(self):
        with mock.patch.dict(sys.modules, {"openwakeword": None, "openwakeword.utils": None,
                                           "openwakeword.model": None}):
            self.assertIsNone(jarvis.carica_parola_locale())

    def test_promemoria_scaduto_senza_consumarlo(self):
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as tmp:
            p = jarvis.Promemoria(Path(tmp) / "p.json")
            adesso = datetime.now()
            self.assertFalse(p.ce_ne_scaduti(adesso))
            p.aggiungi(adesso - timedelta(seconds=1), "pasta")
            self.assertTrue(p.ce_ne_scaduti(adesso))
            self.assertTrue(p.ce_ne_scaduti(adesso))          # non lo toglie
            self.assertEqual(p.scaduti(adesso), ["pasta"])


class TestComodita(unittest.TestCase):
    """Appunti, note, briefing del mattino, finestra che si allunga, pannello dell'HUD."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cartella = Path(self._tmp.name)
        self._originali = (jarvis.FILE_NOTE, jarvis.FILE_BRIEFING, jarvis.FILE_MEMORIA, jarvis.PROMEMORIA,
                           jarvis.WORKSPACE, jarvis.BRIEFING_ATTIVO)
        jarvis.WORKSPACE = cartella
        jarvis.FILE_NOTE = cartella / "note.md"
        jarvis.FILE_BRIEFING = cartella / "ultimo_briefing.txt"
        jarvis.FILE_MEMORIA = cartella / "memoria.json"
        jarvis.PROMEMORIA = jarvis.Promemoria(cartella / "promemoria.json")
        jarvis.BRIEFING_ATTIVO = True

    def tearDown(self):
        (jarvis.FILE_NOTE, jarvis.FILE_BRIEFING, jarvis.FILE_MEMORIA, jarvis.PROMEMORIA,
         jarvis.WORKSPACE, jarvis.BRIEFING_ATTIVO) = self._originali
        self._tmp.cleanup()

    def test_finestra_che_si_allunga(self):
        self.assertEqual(jarvis.durata_finestra("Preferisce il treno o l'aereo?", 0), jarvis.FINESTRA_DOMANDA)
        self.assertEqual(jarvis.durata_finestra('Ha detto "domani?"', 0), jarvis.FINESTRA_DOMANDA)
        self.assertEqual(jarvis.durata_finestra("Fatto.", 3), jarvis.FINESTRA_CONVERSAZIONE)
        self.assertEqual(jarvis.durata_finestra("Fatto.", 1), jarvis.FINESTRA_ASCOLTO)
        self.assertEqual(jarvis.durata_finestra("", 0), jarvis.FINESTRA_ASCOLTO)

    def test_conta_i_comandi_recenti(self):
        a = jarvis.Attenzione()
        for t in (0, 50, 100, 110):
            a.segna_comando(t)
        self.assertEqual(a.comandi_recenti(110), 4)
        self.assertEqual(a.comandi_recenti(200), 2)   # 0 e 50 sono fuori dai 2 minuti
        a.apri_finestra(10, 20)
        self.assertTrue(a.finestra_aperta(29))
        self.assertFalse(a.finestra_aperta(31))

    def test_briefing_una_volta_al_mattino(self):
        from datetime import datetime
        mattina = datetime(2026, 10, 1, 8, 30)
        self.assertTrue(jarvis.briefing_da_fare(mattina))
        jarvis.segna_briefing(mattina)
        self.assertFalse(jarvis.briefing_da_fare(mattina.replace(hour=10)))
        self.assertTrue(jarvis.briefing_da_fare(datetime(2026, 10, 2, 7, 0)))    # il giorno dopo
        self.assertFalse(jarvis.briefing_da_fare(datetime(2026, 10, 2, 15, 0)))  # pomeriggio
        jarvis.BRIEFING_ATTIVO = False
        self.assertFalse(jarvis.briefing_da_fare(datetime(2026, 10, 3, 8, 0)))

    def test_note(self):
        self.assertEqual(jarvis.tool_leggi_note(), "Nessuna nota ancora.")
        for i in range(7):
            jarvis.tool_prendi_nota(f"nota numero {i}")
        esito = jarvis.tool_leggi_note(3)
        self.assertIn("Ultime 3 note su 7", esito)
        self.assertIn("nota numero 6", esito)
        self.assertNotIn("nota numero 3", esito)
        self.assertEqual(jarvis.tool_prendi_nota("   "), "Niente da annotare.")

    def test_appunti_windows_testo_mai_nello_script(self):
        chiamate = []

        def finto(script, env_extra=None, timeout=30, encoding=None):
            chiamate.append((script, env_extra))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        testo = "'; Remove-Item C:\\ -Recurse; '"
        with mock.patch.object(jarvis, "IS_WIN", True), mock.patch.object(jarvis, "IS_MAC", False), \
                mock.patch.object(jarvis, "_powershell", finto):
            esito = jarvis.tool_copia_negli_appunti(testo)
        self.assertIn("Copiato negli appunti", esito)
        script, env = chiamate[0]
        self.assertEqual(script, jarvis.PS_SCRIVI_APPUNTI)
        self.assertEqual(env, {"JARVIS_APPUNTI": testo.strip()})

    def test_appunti_mac(self):
        visti = {}

        def finto(argomenti, **k):
            visti["argomenti"], visti["input"] = argomenti, k.get("input")
            return SimpleNamespace(returncode=0, stdout="Ciao è così\n".encode("utf-8"), stderr=b"")

        with mock.patch.object(jarvis, "IS_WIN", False), mock.patch.object(jarvis, "IS_MAC", True), \
                mock.patch.object(jarvis.subprocess, "run", finto):
            self.assertIn("Ciao è così", jarvis.tool_leggi_appunti())
            jarvis.tool_copia_negli_appunti("Perché sì")
        self.assertEqual(visti["argomenti"], ["pbcopy"])
        self.assertEqual(visti["input"], "Perché sì".encode("utf-8"))

    def test_appunti_vuoti(self):
        vuoto = SimpleNamespace(returncode=0, stdout="  \r\n", stderr="")
        with mock.patch.object(jarvis, "IS_WIN", True), mock.patch.object(jarvis, "IS_MAC", False), \
                mock.patch.object(jarvis, "_powershell", lambda *a, **k: vuoto):
            self.assertIn("vuoti", jarvis.tool_leggi_appunti())

    def test_appunti_e_note_nella_barriera(self):
        self.assertTrue({"leggi_appunti", "leggi_note"} <= jarvis.STRUMENTI_CHE_LEGGONO)
        # voluto: "correggi quello che ho copiato" legge e riscrive gli appunti nello stesso turno
        self.assertNotIn("copia_negli_appunti", jarvis.STRUMENTI_CHE_AGISCONO)
        nomi = {t["name"] for t in jarvis.TOOLS_LOCALI}
        self.assertTrue({"leggi_appunti", "copia_negli_appunti", "prendi_nota", "leggi_note"} <= nomi)

    def test_correggi_quello_che_ho_copiato(self):
        """Legge e riscrive gli appunti nello stesso turno; una mail invece resta bloccata."""
        client = ClientFinto(
            _risposta("tool_use", _tool_use("leggi_appunti", {}, "a")),
            _risposta("tool_use", _tool_use("copia_negli_appunti", {"testo": "Buongiorno, ecco il file."}, "b"),
                      _tool_use("prepara_mail", {"oggetto": "x", "testo": "y"}, "c")),
            _risposta("end_turn", _testo("Fatto, e' negli appunti.")),
        )
        copiati, mail = [], []
        esecutori = dict(jarvis.ESECUTORI,
                         leggi_appunti=lambda: "Testo copiato dall'utente:\nbuongiono ecco il fail",
                         copia_negli_appunti=lambda testo: copiati.append(testo) or "Copiato negli appunti.",
                         prepara_mail=lambda **k: mail.append(k) or "Bozza aperta.")
        with mock.patch.object(jarvis, "ESECUTORI", esecutori):
            jarvis.chiedi_a_claude(client, [], "correggi quello che ho copiato")
        self.assertEqual(copiati, ["Buongiorno, ecco il file."])
        self.assertEqual(mail, [])   # barriera: dopo aver letto gli appunti niente mail nello stesso turno

    def test_riepilogo_per_l_hud(self):
        from datetime import datetime, timedelta
        adesso = datetime(2026, 10, 1, 9, 0)
        jarvis.PROMEMORIA.aggiungi(adesso + timedelta(hours=1), "pasta")
        jarvis.PROMEMORIA.aggiungi(adesso + timedelta(days=1), "commercialista")
        jarvis.PROMEMORIA.aggiungi(adesso + timedelta(days=5), "dentista")
        jarvis.tool_ricorda("Abito a Milano")
        r = jarvis.riepilogo_ricordi(adesso)
        self.assertEqual([p["quando"] for p in r["promemoria"]], ["10:00", "domani 09:00", "06/10 09:00"])
        self.assertEqual(r["memoria"], ["Abito a Milano"])
        self.assertEqual((r["promemoria_totale"], r["memoria_totale"]), (3, 1))


class TestMail(unittest.TestCase):
    def test_indirizzi(self):
        self.assertEqual(jarvis.indirizzi_validi(""), [])
        self.assertEqual(jarvis.indirizzi_validi("a@b.it; c@d.com"), ["a@b.it", "c@d.com"])
        self.assertIsNone(jarvis.indirizzi_validi("mario rossi"))
        self.assertIsNone(jarvis.indirizzi_validi("a@b.it?bcc=spia@x.com"))

    def test_link_mailto_codificato(self):
        link = jarvis.link_mailto(["a@b.it"], "Ciao & saluti", "Riga 1\nRiga 2?cc=x@y.z")
        self.assertTrue(link.startswith("mailto:a@b.it?subject="))
        self.assertNotIn("\n", link)
        self.assertNotIn("&saluti", link)
        self.assertEqual(link.count("?"), 1)          # niente parametri iniettati dal testo
        self.assertNotIn("cc=x@y.z", link)

    def test_bozza_mai_inviata(self):
        aperti = []
        originali = (jarvis._bozza_outlook, jarvis.webbrowser.open)
        jarvis._bozza_outlook = lambda *a: False
        jarvis.webbrowser.open = aperti.append
        try:
            esito = jarvis.tool_prepara_mail("Oggetto", "Testo", "a@b.it")
        finally:
            jarvis._bozza_outlook, jarvis.webbrowser.open = originali
        self.assertEqual(len(aperti), 1)
        self.assertIn("NON e' stata inviata", esito)
        self.assertIn("non valido", jarvis.tool_prepara_mail("x", "y", "non un indirizzo"))


class TestStatoSistema(unittest.TestCase):
    def test_giorno_della_settimana(self):
        from datetime import datetime
        atteso = jarvis.GIORNI[datetime.now().weekday()]
        self.assertIn(atteso, jarvis.tool_stato_sistema("ora"))


class TestComandiLocali(unittest.TestCase):
    def test_uscita(self):
        for frase in ["Esci", "Jarvis, spegniti", "arrivederci jarvis", "termina la sessione"]:
            with self.subTest(frase=frase):
                with self.assertRaises(jarvis.Spegnimento):
                    jarvis.comando_locale(frase, [])

    def test_frasi_che_non_devono_spegnere(self):
        for frase in ["Riesci ad aprire Safari?", "esci da Word", "quanti pesci ci sono"]:
            with self.subTest(frase=frase):
                self.assertFalse(jarvis.comando_locale(frase, []))

    def test_parola_di_attivazione(self):
        for frase in ["Jarvis che ore sono", "apri Chrome, Jarvis", "ehi Giarvis spegniti", "Jarvi apri Chrome"]:
            with self.subTest(frase=frase):
                self.assertTrue(jarvis.rivolta_a_jarvis(frase))
        for frase in ["che ore sono", "apri Chrome", "ho visto Travis ieri", ""]:
            with self.subTest(frase=frase):
                self.assertFalse(jarvis.rivolta_a_jarvis(frase))

    def test_uscita_con_ehi(self):
        with self.assertRaises(jarvis.Spegnimento):
            jarvis.comando_locale("Ehi Jarvis, spegniti", [])

    def test_azzera_memoria(self):
        scambi = [["qualcosa"]]
        vecchio, jarvis.parla = jarvis.parla, lambda testo: None
        try:
            self.assertTrue(jarvis.comando_locale("Dimentica tutto", scambi))
        finally:
            jarvis.parla = vecchio
        self.assertEqual(scambi, [])


class TestAttenzione(unittest.TestCase):
    def test_senza_nome_ignorata_fuori_finestra(self):
        a = jarvis.Attenzione()
        self.assertEqual(a.valuta("apri Chrome", in_finestra=False), "ignora")
        self.assertEqual(a.valuta("Jarvis apri Chrome", in_finestra=False), "comando")

    def test_stile_alexa(self):
        a = jarvis.Attenzione()
        self.assertEqual(a.valuta("Ehi Jarvis", in_finestra=False), "attesa")
        a.apri_finestra(100.0)
        self.assertTrue(a.finestra_aperta(100.0 + jarvis.FINESTRA_ASCOLTO - 1))
        self.assertFalse(a.finestra_aperta(100.0 + jarvis.FINESTRA_ASCOLTO + 1))
        self.assertEqual(a.valuta("apri Chrome", in_finestra=True), "comando")

    def test_pausa_e_risveglio(self):
        a = jarvis.Attenzione()
        a.apri_finestra(100.0)
        self.assertEqual(a.valuta("Jarvis, dormi", in_finestra=True), "dormi")
        self.assertFalse(a.finestra_aperta(100.0))
        for frase in ["apri Chrome", "Jarvis apri Chrome", "che ore sono"]:
            with self.subTest(frase=frase):
                self.assertEqual(a.valuta(frase, in_finestra=False), "ignora")
        self.assertEqual(a.valuta("Jarvis, spegniti", in_finestra=False), "comando")
        self.assertEqual(a.valuta("Ehi Jarvis", in_finestra=False), "sveglia")
        self.assertFalse(a.dorme)

    def test_svegliati(self):
        a = jarvis.Attenzione()
        a.valuta("Jarvis vai a dormire", in_finestra=False)
        self.assertEqual(a.valuta("Jarvis svegliati", in_finestra=False), "sveglia")


class TestUnicaIstanza(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "comportamento del blocco su Windows da verificare a mano")
    def test_seconda_copia_rifiutata(self):
        with tempfile.TemporaryDirectory() as tmp:
            originale = jarvis.CARTELLA_JARVIS
            jarvis.CARTELLA_JARVIS = Path(tmp)
            try:
                self.assertTrue(jarvis._unica_istanza())
                primo = jarvis._blocco_istanza
                self.assertFalse(jarvis._unica_istanza())
                primo.close()
            finally:
                jarvis.CARTELLA_JARVIS = originale


class TestServerHud(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = jarvis._crea_server_hud(0)   # porta libera qualsiasi
        cls.porta = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _richiesta(self, metodo, percorso, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        c.putrequest(metodo, percorso, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{self.porta}")
        c.endheaders()
        r = c.getresponse()
        corpo = r.read()
        c.close()
        return r, corpo

    def test_pagina(self):
        r, corpo = self._richiesta("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"J.A.R.V.I.S.", corpo)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))

    def test_stato(self):
        jarvis.HUD.imposta("parla", "prova")
        r, corpo = self._richiesta("GET", "/stato")
        self.assertEqual(r.status, 200)
        dati = json.loads(corpo)
        self.assertEqual((dati["stato"], dati["dettaglio"]), ("parla", "prova"))
        self.assertIn("residuo", dati["consumi"])
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_host_estraneo_rifiutato(self):
        # Difesa dal DNS rebinding: un sito esterno che punta a 127.0.0.1 ha un Host diverso.
        for host in ["sito-malevolo.com", f"sito-malevolo.com:{self.porta}", "127.0.0.1:1"]:
            with self.subTest(host=host):
                r, _ = self._richiesta("GET", "/stato", host=host)
                self.assertEqual(r.status, 403)

    def test_solo_lettura(self):
        for metodo in ["POST", "PUT", "DELETE"]:
            with self.subTest(metodo=metodo):
                r, _ = self._richiesta(metodo, "/stato")
                self.assertEqual(r.status, 501)

    def test_percorso_sconosciuto(self):
        r, _ = self._richiesta("GET", "/../jarvis.py")
        self.assertEqual(r.status, 404)


class TestCicloDialogo(unittest.TestCase):
    def test_risposta_semplice(self):
        client = ClientFinto(_risposta("end_turn", _testo("Buongiorno, Signore.")))
        scambi = []
        self.assertEqual(jarvis.chiedi_a_claude(client, scambi, "ciao"), "Buongiorno, Signore.")
        self.assertEqual(len(scambi), 1)

    def test_giro_di_strumento(self):
        client = ClientFinto(
            _risposta("tool_use", _tool_use("stato_sistema", {"cosa": "ora"})),
            _risposta("end_turn", _testo("Sono le dieci.")),
        )
        scambi = []
        self.assertEqual(jarvis.chiedi_a_claude(client, scambi, "che ore sono"), "Sono le dieci.")
        ultimo_invio = client.chiamate[-1]
        self.assertEqual(ultimo_invio[-1]["content"][0]["type"], "tool_result")
        self.assertEqual(ultimo_invio[-1]["content"][0]["tool_use_id"], "tu_1")

    def test_pause_turn_riprende_senza_messaggi_extra(self):
        client = ClientFinto(
            _risposta("pause_turn", _testo("Cerco. ")),
            _risposta("end_turn", _testo("Trovato.")),
        )
        scambi = []
        risposta = jarvis.chiedi_a_claude(client, scambi, "notizie di oggi")
        self.assertEqual(risposta, "Cerco. Trovato.")
        # La seconda chiamata rimanda domanda + risposta in pausa, senza "continua".
        self.assertEqual([m["role"] for m in client.chiamate[1]], ["user", "assistant"])
        # In memoria resta un solo messaggio dell'assistente per il turno.
        self.assertEqual([m["role"] for m in scambi[0]], ["user", "assistant"])

    def test_max_tokens_non_lascia_strumenti_orfani(self):
        client = ClientFinto(
            _risposta("max_tokens", _testo("Dunque"), _tool_use("leggi_file", {})),
        )
        scambi = []
        self.assertEqual(jarvis.chiedi_a_claude(client, scambi, "leggi"), "Dunque")
        tipi = [b.type for b in scambi[0][-1]["content"]]
        self.assertEqual(tipi, ["text"])

    def test_limite_giri(self):
        client = ClientFinto(
            *[_risposta("tool_use", _tool_use("stato_sistema", {"cosa": "ora"}))
              for _ in range(jarvis.MAX_GIRI_TOOL)]
        )
        risposta = jarvis.chiedi_a_claude(client, [], "loop")
        self.assertIn("ciclo", risposta)


if __name__ == "__main__":
    unittest.main()
