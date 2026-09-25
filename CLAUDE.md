# J.A.R.V.I.S. — contesto del progetto

## 1. Cos'è
Assistente vocale in italiano per macOS basato sull'API Claude: ascolta dal microfono, risponde con voce sintetica e agisce sul Mac tramite strumenti dichiarati (apre e chiude app, legge e scrive file, legge lo stato del sistema, cerca sul web).

## 2. File coinvolti
- `jarvis.py` — unico file del progetto. Contiene configurazione, sintesi vocale, ascolto microfonico, whitelist applicazioni, sandbox dei file, implementazione degli strumenti, ciclo di dialogo con l'API e loop principale.

## 3. Logica principale
Loop infinito: ascolto → trascrizione → comandi gestiti in locale (spegnimento, azzeramento memoria) → altrimenti chiamata all'API con la lista degli strumenti. Se il modello richiede uno strumento, lo script lo esegue localmente e gli restituisce l'esito, ripetendo finché `stop_reason` è diverso da `tool_use` (massimo 6 giri). La risposta finale viene letta ad alta voce. Memoria conversazionale: ultimi 6 scambi completi, archiviati come blocchi interi per non spezzare mai una coppia tool_use / tool_result.

## 4. Comunicazione con altri componenti
Nessuna. Non esiste bridge, né protocollo, né scambio di messaggi con altri processi. Le uniche comunicazioni esterne sono HTTPS verso l'API Anthropic e verso il servizio di trascrizione Google usato da SpeechRecognition.

## 5. Decisioni prese e perché
- Modello `claude-haiku-4-5-20251001`: scelto per latenza e costo; verificato sui doc ufficiali che supporta il web search.
- Chiave API letta da `ANTHROPIC_API_KEY`, mai scritta nel sorgente.
- Sintesi vocale con il comando nativo `say` invece di pyttsx3: più stabile e voci italiane migliori.
- Whitelist di strumenti invece di esecuzione shell libera: il modello non deve avere potere arbitrario sul Mac.
- Nomi delle app come `enum` nello schema e risolti tramite dizionario: nessun testo generato dal modello raggiunge `open` o `osascript`.
- Scrittura file confinata a una sandbox (default `~/Jarvis/workspace`); estensioni eseguibili rifiutate.
- `max_uses: 3` sul web search: ogni ricerca costa circa 0,01 $.
- Dettatura dentro app già aperte esclusa dal progetto: richiederebbe il permesso Accessibilità ed è fragile, perché scrive in qualunque finestra abbia il focus.

## 6. Cose da NON fare
- Non aggiungere uno strumento che esegua comandi shell arbitrari.
- Non interpolare testo generato dal modello dentro `osascript` o `subprocess`.
- Non reintrodurre pyttsx3, né la chiave API nel sorgente.
- Non rimuovere i controlli su percorsi assoluti e `..` nella sandbox.
- Non richiedere il permesso Accessibilità di macOS.

## 7. Problemi aperti
- Credito API non ancora caricato sulla Console Anthropic: senza credito le chiamate falliscono con errori di fatturazione della classe 400.
- `portaudio` e `pyaudio` non ancora installati sul Mac.
- Permessi macOS non ancora concessi: Microfono per il Terminale, Automazione per `chiudi_app`.
- Verificati finora solo la compilazione del file e la funzione di sandbox. Microfono, `say`, AppleScript e chiamate API non sono mai stati eseguiti.
