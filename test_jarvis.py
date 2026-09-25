"""
Test della logica di J.A.R.V.I.S. che non richiede microfono, voce o API reale.

Avvio:  python3 -m unittest test_jarvis -v      (Windows: py -m unittest test_jarvis -v)
"""

import os
import tempfile
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

    def test_azzera_memoria(self):
        scambi = [["qualcosa"]]
        vecchio, jarvis.parla = jarvis.parla, lambda testo: None
        try:
            self.assertTrue(jarvis.comando_locale("Dimentica tutto", scambi))
        finally:
            jarvis.parla = vecchio
        self.assertEqual(scambi, [])


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
