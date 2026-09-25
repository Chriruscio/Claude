# J.A.R.V.I.S. — contesto del progetto

## 1. Cos'è
Assistente vocale in italiano per macOS e Windows basato sull'API Claude: ascolta dal microfono, risponde con voce sintetica e agisce sul computer tramite strumenti dichiarati (apre e chiude app, legge e scrive file, legge lo stato del sistema, cerca sul web). Due istanze indipendenti, una per macchina, dallo stesso sorgente. Una pagina web locale (HUD) mostra lo stato di J.A.R.V.I.S. e la conversazione.

## 2. File coinvolti
- `jarvis.py` — unico file del programma. Contiene configurazione, rilevamento del sistema operativo, sintesi vocale, ascolto microfonico, whitelist applicazioni (una per sistema), sandbox dei file, implementazione degli strumenti, ciclo di dialogo con l'API e loop principale.
- `hud.html` — la pagina dell'HUD (reattore animato, stato, ultime 6 battute). Legge `/stato` ogni 300 ms e inserisce i testi solo con `textContent`.
- `app_windows.json` / `app_mac.json` (facoltativi, non versionati) — elenco personale di app, accanto a `jarvis.py`. Si aggiunge all'elenco di base e ne sostituisce le voci con lo stesso nome. Se il file ha errori, J.A.R.V.I.S. lo segnala all'avvio e usa solo l'elenco di base. Esempio da copiare: `app_windows.esempio.json`.
- `test_jarvis.py` — test della logica indipendente dall'hardware (sandbox, comandi locali, ciclo di dialogo con client finto). `python3 -m unittest test_jarvis -v`.

## 3. Logica principale
Loop infinito: ascolto → trascrizione → filtro di attenzione (classe `Attenzione`) → comandi gestiti in locale (spegnimento, azzeramento memoria) → altrimenti chiamata all'API con la lista degli strumenti. Se il modello richiede uno strumento, lo script lo esegue localmente e gli restituisce l'esito, ripetendo finché `stop_reason` è diverso da `tool_use` (massimo 6 giri). Con `pause_turn` (web search lato server non concluso) la conversazione viene rimandata senza messaggi aggiuntivi e le due parti della risposta vengono unite in un unico messaggio dell'assistente. Con `max_tokens` vengono tenuti in memoria solo i blocchi di testo, per non lasciare strumenti senza risultato. La risposta finale viene letta ad alta voce. Memoria conversazionale: ultimi 6 scambi completi, archiviati come blocchi interi per non spezzare mai una coppia tool_use / tool_result.

Filtro di attenzione:
- Sveglio: passano le frasi con "Jarvis" (anche le varianti di trascrizione `giarvis`, `jervis`) e, per `FINESTRA_ASCOLTO` (8) secondi dopo ogni risposta, anche quelle senza nome (stile Alexa). "Ehi Jarvis" da solo risponde "Sì, Signore?" e apre la finestra. In finestra l'ascolto aspetta solo il tempo rimasto, così una frase iniziata dopo la scadenza non passa senza nome.
- Addormentato ("Jarvis, dormi" / "vai a dormire" / "pausa"): ignora tutto tranne "Jarvis, svegliati", "ehi Jarvis" e lo spegnimento. L'audio viene comunque trascritto da Google: la pausa non è un microfono spento.
- Le frasi ignorate non raggiungono mai l'API.

Avvio senza finestre (Windows: `pyw -3.13 jarvis.py`): con `pythonw` stdout e stderr non esistono, quindi vengono rediretti su `~/Jarvis/jarvis.log` (azzerato all'avvio oltre 1 MB) prima di ogni import che può fallire. I processi figli (PowerShell, `taskkill`) partono con `CREATE_NO_WINDOW`. Un errore all'avvio viene annunciato a voce. Un file di blocco (`~/Jarvis/jarvis.lock`) impedisce due istanze contemporanee.

HUD: all'avvio parte in sottofondo un server HTTP su `127.0.0.1:8765` (`JARVIS_HUD_PORTA`; `JARVIS_HUD=0` per disattivarlo; se la porta è occupata J.A.R.V.I.S. continua senza). Stati: `avvio`, `ascolto`, `attento` (finestra di 8 s), `elaborazione` (con il nome dello strumento), `parla`, `dorme`, `spento`; la pagina mostra `offline` se il server non risponde. "Ehi Jarvis" e "Jarvis, svegliati" aprono la pagina solo se non è già collegata (nessuna richiesta negli ultimi 2 s); su Windows in Edge modalità app (finestra senza barre), altrimenti nel browser predefinito.

Differenze per sistema (scelte in base a `platform.system()`):
- Voce: prima scelta la voce neurale `it-IT-DiegoNeural` tramite `edge-tts` (audio mp3 decodificato con `miniaudio` e suonato con `pyaudio`, su entrambi i sistemi). Riserva: macOS `say`; Windows sintetizzatore di sistema (System.Speech) tramite PowerShell, con la prima voce italiana installata. Alla prima frase in cui la voce neurale fallisce si passa alla riserva fino al riavvio. Prima di parlare il nome ("J.A.R.V.I.S.", "Jarvis") viene riscritto "Giarvis", la grafia che le voci italiane pronunciano come il nome inglese; sullo schermo e nell'HUD resta com'è.
- App: macOS `open -a` e `osascript ... quit`; Windows `os.startfile` e `taskkill /IM` senza `/F` (chiusura gentile).
- Batteria: macOS `pmset`; Windows `Win32_Battery` via PowerShell. Disco: `shutil.disk_usage` su entrambi.

## 4. Comunicazione con altri componenti
Nessun collegamento tra il Mac e il PC. Le comunicazioni esterne sono HTTPS verso l'API Anthropic e verso il servizio di trascrizione Google usato da SpeechRecognition.

L'unico punto in ascolto è il server dell'HUD: solo `127.0.0.1`, solo `GET /` e `GET /stato` (ogni altro metodo risponde 501), nessuna richiesta può comandare J.A.R.V.I.S. Rifiuta con 403 ogni header `Host` diverso da `127.0.0.1:<porta>` o `localhost:<porta>` (difesa dal DNS rebinding) e non invia intestazioni CORS, quindi i siti aperti nel browser non possono leggere la conversazione. La pagina ha una Content-Security-Policy che vieta risorse esterne.

## 5. Decisioni prese e perché
- Modello `claude-haiku-4-5-20251001`: scelto per latenza e costo; supporta il web search nella variante `web_search_20250305` (le varianti più recenti richiedono modelli più grandi).
- Chiave API letta da `ANTHROPIC_API_KEY`, mai scritta nel sorgente.
- Voce neurale Microsoft via `edge-tts` (`JARVIS_VOCE_NEURALE`, `0` per disattivarla): qualità molto superiore alle voci di sistema e voce maschile, gratis. Rischi accettati: è il servizio di lettura di Edge usato in modo non ufficiale, quindi può smettere di funzionare senza preavviso (da qui la riserva automatica); il testo delle risposte viene inviato a Microsoft.
- Sintesi vocale di riserva con `say` su macOS invece di pyttsx3: più stabile e voci italiane migliori. Su Windows PowerShell + System.Speech: niente dipendenze extra; costo circa mezzo secondo di avvio per frase.
- Script PowerShell costanti: il testo da leggere passa tramite variabile d'ambiente, mai interpolato nello script.
- Whitelist di strumenti invece di esecuzione shell libera: il modello non deve avere potere arbitrario sul computer.
- Nomi delle app come `enum` nello schema e risolti tramite dizionario: nessun testo generato dal modello raggiunge `open`, `osascript`, `os.startfile` o `taskkill`.
- Lettura e scrittura file confinate a una sandbox (default `~/Jarvis/workspace`), controllata con `resolve()` (segue i collegamenti simbolici). Estensioni in lista bianca (`.txt .md .csv .json .log`); il carattere `:` è vietato (percorsi `C:file` e flussi alternativi NTFS).
- Elenco app personale in un file separato: aggiornare `jarvis.py` non cancella le app aggiunte dall'utente. Il file viene ignorato se si trova dentro la sandbox, perché il modello non deve poter allargare la whitelist. Su macOS i nomi con `"` o `\` vengono rifiutati, perché finiscono dentro AppleScript.
- Su Windows `esplora risorse` si apre ma non si chiude da voce: chiudere `explorer.exe` fa sparire la barra delle applicazioni.
- Comandi locali riconosciuti solo se la frase è esattamente il comando (a meno di "Jarvis", "per favore", "grazie"): prima "riesci ad aprire Safari?" conteneva "esci" e spegneva l'assistente.
- Parola di attivazione "Jarvis" richiesta fuori dalla finestra di 8 secondi: conversazioni e TV nella stanza non costano e non fanno agire. Compromesso accettato: dentro la finestra una frase della TV passa. Limite: l'audio viene comunque trascritto da Google per cercare la parola.
- `max_uses: 3` sul web search: ogni ricerca costa circa 0,01 $, più i token dei risultati che entrano nel contesto.
- Dettatura dentro app già aperte esclusa dal progetto: richiederebbe il permesso Accessibilità ed è fragile, perché scrive in qualunque finestra abbia il focus.

## 6. Cose da NON fare
- Non aggiungere uno strumento che esegua comandi shell arbitrari.
- Non interpolare testo generato dal modello dentro `osascript`, script PowerShell o `subprocess`.
- Non reintrodurre pyttsx3, né la chiave API nel sorgente.
- Non rimuovere i controlli su percorsi assoluti, `..`, `:` e il confronto dopo `resolve()` nella sandbox.
- Non tornare a una lista nera di estensioni.
- Non aggiungere all'HUD endpoint che modificano lo stato o eseguono azioni, né intestazioni CORS; non metterlo in ascolto su un indirizzo diverso da `127.0.0.1`; non togliere il controllo sull'header `Host`.
- Non inserire nella pagina testi della conversazione con `innerHTML`: arrivano dal modello e dal web.
- Non mettere il file dell'elenco app dentro la sandbox, né dare al modello uno strumento per modificarlo.
- Non dare a J.A.R.V.I.S. accesso in scrittura al proprio codice.
- Non lanciare processi figli su Windows senza `creationflags=SENZA_FINESTRA`.
- Non usare `taskkill /F`: perde il lavoro non salvato.
- Non richiedere il permesso Accessibilità di macOS.
- Non aggiungere un collegamento di rete tra le due istanze senza una revisione di sicurezza dedicata.

## 7. Problemi aperti
- Credito API non ancora caricato sulla Console Anthropic: senza credito le chiamate falliscono con errori di fatturazione della classe 400.
- `portaudio` e `pyaudio` non ancora installati sul Mac.
- Permessi macOS non ancora concessi: Microfono per il Terminale, Automazione per `chiudi_app`.
- Windows (verificato il 25/09/2026 con Python 3.13): test automatici OK, voce italiana "Microsoft Elsa Desktop", microfono e trascrizione, apertura e chiusura di Blocco note, stato del sistema. Serve Python 3.13 (`py -3.13`): `pyaudio` 0.2.14 non ha pacchetti pronti per la 3.14.
- Windows: gli altri bersagli in `APP_WIN` non sono ancora stati provati; i nomi dei processi di Calcolatrice, Impostazioni e Spotify vanno confermati con Gestione attività.
- HUD verificato solo su Linux (Chromium headless, schermate desktop e telefono). Mai aperto su Windows né in Edge modalità app.
- Voce neurale mai ascoltata: l'ambiente di sviluppo blocca `speech.platform.bing.com`. Pronuncia di "Giarvis" da verificare a orecchio.
- Mai eseguite finora: chiamate API reali (su entrambi i sistemi) e tutto il lato Mac.
