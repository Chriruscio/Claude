"""
Test della logica di J.A.R.V.I.S. che non richiede microfono, voce o API reale.

Avvio:  python3 -m unittest test_jarvis -v      (Windows: py -m unittest test_jarvis -v)
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
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
        self.assertIn("pywin32", installa.PACCHETTI)


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
