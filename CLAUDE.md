# J.A.R.V.I.S. — contesto del progetto

## 1. Cos'è
Assistente vocale in italiano per macOS e Windows basato sull'API Claude: ascolta dal microfono, risponde con voce sintetica e agisce sul computer tramite strumenti dichiarati (apre e chiude app, legge e scrive file, legge lo stato del sistema, cerca sul web). Due istanze indipendenti, una per macchina, dallo stesso sorgente.

## 2. File coinvolti
- `jarvis.py` — unico file del programma. Contiene configurazione, rilevamento del sistema operativo, sintesi vocale, ascolto microfonico, whitelist applicazioni (una per sistema), sandbox dei file, implementazione degli strumenti, ciclo di dialogo con l'API e loop principale.
- `app_windows.json` / `app_mac.json` (facoltativi, non versionati) — elenco personale di app, accanto a `jarvis.py`. Si aggiunge all'elenco di base e ne sostituisce le voci con lo stesso nome. Se il file ha errori, J.A.R.V.I.S. lo segnala all'avvio e usa solo l'elenco di base. Esempio da copiare: `app_windows.esempio.json`.
- `test_jarvis.py` — test della logica indipendente dall'hardware (sandbox, comandi locali, ciclo di dialogo con client finto). `python3 -m unittest test_jarvis -v`.

## 3. Logica principale
Loop infinito: ascolto → trascrizione → frasi senza "Jarvis" (o varianti di trascrizione `giarvis`, `jervis`) ignorate senza chiamare l'API → comandi gestiti in locale (spegnimento, azzeramento memoria) → altrimenti chiamata all'API con la lista degli strumenti. Se il modello richiede uno strumento, lo script lo esegue localmente e gli restituisce l'esito, ripetendo finché `stop_reason` è diverso da `tool_use` (massimo 6 giri). Con `pause_turn` (web search lato server non concluso) la conversazione viene rimandata senza messaggi aggiuntivi e le due parti della risposta vengono unite in un unico messaggio dell'assistente. Con `max_tokens` vengono tenuti in memoria solo i blocchi di testo, per non lasciare strumenti senza risultato. La risposta finale viene letta ad alta voce. Memoria conversazionale: ultimi 6 scambi completi, archiviati come blocchi interi per non spezzare mai una coppia tool_use / tool_result.

Differenze per sistema (scelte in base a `platform.system()`):
- Voce: macOS `say`; Windows sintetizzatore di sistema (System.Speech) tramite PowerShell, con la prima voce italiana installata.
- App: macOS `open -a` e `osascript ... quit`; Windows `os.startfile` e `taskkill /IM` senza `/F` (chiusura gentile).
- Batteria: macOS `pmset`; Windows `Win32_Battery` via PowerShell. Disco: `shutil.disk_usage` su entrambi.

## 4. Comunicazione con altri componenti
Nessuna. Non esiste bridge, né protocollo, né scambio di messaggi tra il Mac e il PC o con altri processi. Le uniche comunicazioni esterne sono HTTPS verso l'API Anthropic e verso il servizio di trascrizione Google usato da SpeechRecognition.

## 5. Decisioni prese e perché
- Modello `claude-haiku-4-5-20251001`: scelto per latenza e costo; supporta il web search nella variante `web_search_20250305` (le varianti più recenti richiedono modelli più grandi).
- Chiave API letta da `ANTHROPIC_API_KEY`, mai scritta nel sorgente.
- Sintesi vocale con `say` su macOS invece di pyttsx3: più stabile e voci italiane migliori. Su Windows PowerShell + System.Speech: niente dipendenze extra; costo circa mezzo secondo di avvio per frase.
- Script PowerShell costanti: il testo da leggere passa tramite variabile d'ambiente, mai interpolato nello script.
- Whitelist di strumenti invece di esecuzione shell libera: il modello non deve avere potere arbitrario sul computer.
- Nomi delle app come `enum` nello schema e risolti tramite dizionario: nessun testo generato dal modello raggiunge `open`, `osascript`, `os.startfile` o `taskkill`.
- Lettura e scrittura file confinate a una sandbox (default `~/Jarvis/workspace`), controllata con `resolve()` (segue i collegamenti simbolici). Estensioni in lista bianca (`.txt .md .csv .json .log`); il carattere `:` è vietato (percorsi `C:file` e flussi alternativi NTFS).
- Elenco app personale in un file separato: aggiornare `jarvis.py` non cancella le app aggiunte dall'utente. Il file viene ignorato se si trova dentro la sandbox, perché il modello non deve poter allargare la whitelist. Su macOS i nomi con `"` o `\` vengono rifiutati, perché finiscono dentro AppleScript.
- Su Windows `esplora risorse` si apre ma non si chiude da voce: chiudere `explorer.exe` fa sparire la barra delle applicazioni.
- Comandi locali riconosciuti solo se la frase è esattamente il comando (a meno di "Jarvis", "per favore", "grazie"): prima "riesci ad aprire Safari?" conteneva "esci" e spegneva l'assistente.
- Parola di attivazione "Jarvis" obbligatoria in ogni frase, in qualunque punto: conversazioni e TV nella stanza non costano e non fanno agire. Nessuna fase separata di attivazione ("ehi Jarvis" e poi il comando): con la parola obbligatoria in ogni frase non aggiungerebbe protezione. Limite: l'audio viene comunque trascritto da Google per cercare la parola.
- `max_uses: 3` sul web search: ogni ricerca costa circa 0,01 $, più i token dei risultati che entrano nel contesto.
- Dettatura dentro app già aperte esclusa dal progetto: richiederebbe il permesso Accessibilità ed è fragile, perché scrive in qualunque finestra abbia il focus.

## 6. Cose da NON fare
- Non aggiungere uno strumento che esegua comandi shell arbitrari.
- Non interpolare testo generato dal modello dentro `osascript`, script PowerShell o `subprocess`.
- Non reintrodurre pyttsx3, né la chiave API nel sorgente.
- Non rimuovere i controlli su percorsi assoluti, `..`, `:` e il confronto dopo `resolve()` nella sandbox.
- Non tornare a una lista nera di estensioni.
- Non mettere il file dell'elenco app dentro la sandbox, né dare al modello uno strumento per modificarlo.
- Non dare a J.A.R.V.I.S. accesso in scrittura al proprio codice.
- Non usare `taskkill /F`: perde il lavoro non salvato.
- Non richiedere il permesso Accessibilità di macOS.
- Non aggiungere un collegamento di rete tra le due istanze senza una revisione di sicurezza dedicata.

## 7. Problemi aperti
- Credito API non ancora caricato sulla Console Anthropic: senza credito le chiamate falliscono con errori di fatturazione della classe 400.
- `portaudio` e `pyaudio` non ancora installati sul Mac.
- Permessi macOS non ancora concessi: Microfono per il Terminale, Automazione per `chiudi_app`.
- Windows (verificato il 25/09/2026 con Python 3.13): test automatici OK, voce italiana "Microsoft Elsa Desktop", microfono e trascrizione, apertura e chiusura di Blocco note, stato del sistema. Serve Python 3.13 (`py -3.13`): `pyaudio` 0.2.14 non ha pacchetti pronti per la 3.14.
- Windows: gli altri bersagli in `APP_WIN` non sono ancora stati provati; i nomi dei processi di Calcolatrice, Impostazioni e Spotify vanno confermati con Gestione attività.
- Mai eseguite finora: chiamate API reali (su entrambi i sistemi) e tutto il lato Mac.
