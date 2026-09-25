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
        for frase in ["Jarvis che ore sono", "apri Chrome, Jarvis", "ehi Giarvis spegniti"]:
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
