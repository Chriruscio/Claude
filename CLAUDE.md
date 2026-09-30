# J.A.R.V.I.S. — contesto del progetto

## 1. Cos'è
Assistente vocale in italiano per macOS e Windows basato sull'API Claude: ascolta dal microfono, risponde con voce sintetica e agisce sul computer tramite strumenti dichiarati (apre e chiude app, legge e scrive file, legge lo stato del sistema, cerca sul web). Due istanze indipendenti, una per macchina, dallo stesso sorgente. Una pagina web locale (HUD) mostra lo stato di J.A.R.V.I.S. e la conversazione.

## 2. File coinvolti
- `jarvis.py` — unico file del programma. Contiene configurazione, rilevamento del sistema operativo, sintesi vocale, ascolto microfonico, whitelist applicazioni (una per sistema), sandbox dei file, implementazione degli strumenti, ciclo di dialogo con l'API e loop principale.
- `hud.html` — la pagina dell'HUD: sfera di particelle 3D disegnata su canvas (nessuna libreria esterna) con anelli e quadrante, che segue lo stato e il volume della voce; onda della voce; pannello SISTEMA (CPU, memoria, disco, batteria) e pannello SESSIONE (token, speso, credito); ultime 6 battute. Legge `/stato` ogni 300 ms e inserisce i testi solo con `textContent`.
- `~/Jarvis/consumi.json` — credito indicato dall'utente e consumi sommati da allora (token, ricerche, richieste, spesa stimata). Fuori dalla sandbox.
- `~/Jarvis/app_windows.json` / `~/Jarvis/app_mac.json` (facoltativi) — elenco personale di app. Stanno in `~/Jarvis` e non accanto a `jarvis.py` perché ogni aggiornamento cancella e ricrea la cartella del codice. Si aggiunge all'elenco di base e ne sostituisce le voci con lo stesso nome. Se il file ha errori, J.A.R.V.I.S. lo segnala all'avvio e usa solo l'elenco di base. Esempio da copiare: `app_windows.esempio.json`.
- `installa.py` — installazione su Windows (`py -3.13 installa.py`): pacchetti pip, controllo della chiave, icona `~/Jarvis/jarvis.ico` disegnata in Python, collegamento "Jarvis" sul desktop che lancia `pythonw.exe jarvis.py` (nessuna finestra), a richiesta collegamento nella cartella Esecuzione automatica. Collegamenti creati da Python con WScript.Shell tramite `pywin32`, in un secondo processo (`--collegamenti`) perché pywin32 appena installato può non essere importabile nel processo che lo installa. La prima versione li creava con un comando PowerShell ed è fallita su Windows con "Accesso negato" al lancio di PowerShell: quasi certamente l'antivirus, che sorveglia PowerShell usato per creare collegamenti (tecnica tipica di persistenza dei malware).
- `~/Jarvis/progetti_windows.json` / `~/Jarvis/progetti_mac.json` (facoltativi) — cartelle su cui Claude Code può lavorare, nome → percorso completo. Esempio: `progetti_windows.esempio.json`. Scartate: percorsi relativi o inesistenti, la home, un intero disco, e ogni cartella che coincide con, contiene o sta dentro la cartella del codice, `~/Jarvis` o la sandbox.
- `~/Jarvis/claude_code/` — un report Markdown per ogni lavoro di Claude Code (compito, esito, durata, costo stimato).
- `test_jarvis.py` — test della logica indipendente dall'hardware (sandbox, comandi locali, ciclo di dialogo con client finto). `python3 -m unittest test_jarvis -v`.

## 3. Logica principale
Loop infinito: ascolto → trascrizione → filtro di attenzione (classe `Attenzione`) → comandi gestiti in locale (spegnimento, azzeramento memoria) → altrimenti chiamata all'API con la lista degli strumenti. Se il modello richiede uno strumento, lo script lo esegue localmente e gli restituisce l'esito, ripetendo finché `stop_reason` è diverso da `tool_use` (massimo 6 giri). Con `pause_turn` (web search lato server non concluso) la conversazione viene rimandata senza messaggi aggiuntivi e le due parti della risposta vengono unite in un unico messaggio dell'assistente. Con `max_tokens` vengono tenuti in memoria solo i blocchi di testo, per non lasciare strumenti senza risultato. La risposta finale viene letta ad alta voce. Memoria conversazionale: ultimi 6 scambi completi, archiviati come blocchi interi per non spezzare mai una coppia tool_use / tool_result.

Filtro di attenzione:
- Sveglio: passano le frasi con "Jarvis" (anche le varianti di trascrizione `jarvi`, `giarvis`, `jervis`) e, per `FINESTRA_ASCOLTO` (8) secondi dopo ogni risposta, anche quelle senza nome (stile Alexa). "Ehi Jarvis" da solo risponde "Mi dica." e apre la finestra. In finestra l'ascolto aspetta solo il tempo rimasto, così una frase iniziata dopo la scadenza non passa senza nome.
- Addormentato ("Jarvis, dormi" / "vai a dormire" / "pausa"): ignora tutto tranne "Jarvis, svegliati", "ehi Jarvis" e lo spegnimento. L'audio viene comunque trascritto da Google: la pausa non è un microfono spento.
- Le frasi ignorate non raggiungono mai l'API.

Avvio senza finestre (Windows: `pyw -3.13 jarvis.py`): con `pythonw` stdout e stderr non esistono, quindi vengono rediretti su `~/Jarvis/jarvis.log` (azzerato all'avvio oltre 1 MB) prima di ogni import che può fallire. I processi figli (PowerShell, `taskkill`) partono con `CREATE_NO_WINDOW`. Un errore all'avvio viene annunciato a voce. Un file di blocco (`~/Jarvis/jarvis.lock`) impedisce due istanze contemporanee.

HUD: all'avvio parte in sottofondo un server HTTP su `127.0.0.1:8765` (`JARVIS_HUD_PORTA`; `JARVIS_HUD=0` per disattivarlo; se la porta è occupata J.A.R.V.I.S. continua senza). Stati: `avvio`, `ascolto`, `attento` (finestra di 8 s), `elaborazione` (con il nome dello strumento), `parla`, `dorme`, `spento`; la pagina mostra `offline` se il server non risponde. "Ehi Jarvis" e "Jarvis, svegliati" aprono la pagina solo se non è già collegata (nessuna richiesta negli ultimi 2 s); su Windows in Edge modalità app (finestra senza barre), altrimenti nel browser predefinito.

HUD vivo: con la voce neurale `_parla_neurale` calcola l'inviluppo del volume (un valore 0-1 ogni 50 ms, `inviluppo()`) e lo pubblica con l'istante di inizio (`time.time()` + latenza d'uscita); la pagina, sulla stessa macchina, legge il livello del momento con `Date.now()`. Con la voce di sistema non c'è inviluppo e la pagina simula un parlato generico. Un thread aggiorna ogni 2 s i dati di sistema (`raccogli_sistema()`, CPU e memoria solo con `psutil` installato). Segnali sonori sintetizzati in Python e suonati con `pyaudio` (`JARVIS_SUONI=0` per toglierli): salita all'attivazione, discesa alla pausa. Non li suona la pagina perché i browser bloccano l'audio senza un clic.

Claude Code: lo strumento `claude_code` (progetto dall'elenco, compito) esiste solo se c'è almeno un progetto valido e il comando `claude` è installato in versione ≥ 2.1.259. Il modello può solo preparare il lavoro; parte se entro 60 s l'utente dice "conferma" (riconosciuto dal ciclo principale, non dal modello), "annulla"/"no" lo cancella, qualunque altra frase lo lascia cadere. Esecuzione in un thread: `claude -p <istruzione fissa> --restricted --tools Read,Edit,Write,Glob,Grep --disallowedTools mcp__* --permission-mode acceptEdits --permission-prompts none --max-turns 60 --max-budget-usd 5 --output-format json`, con il compito su stdin, la cartella del progetto come directory di lavoro, un ambiente senza `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` e un limite di 30 minuti. Alla fine J.A.R.V.I.S. legge a voce le prime frasi del riassunto e salva il report; nell'HUD il riquadro CLAUDE CODE mostra pronto / conferma / al lavoro.

Consumi: dopo ogni chiamata all'API `CONSUMI.registra(risposta.usage)` somma token e ricerche web e stima il costo con `PREZZI_MODELLI` (Haiku 4.5: 1 $ / 5 $ per milione di token in / out; ricerca web 0,01 $). Per un modello fuori listino la spesa risulta `n/d`. `py jarvis.py --credito 5` registra il credito letto nella Console e azzera i totali: il residuo mostrato è credito meno spesa stimata. Nella prima prova reale una domanda semplice pesava circa 3.000 token in ingresso (istruzioni + strumenti + memoria).

Differenze per sistema (scelte in base a `platform.system()`):
- Voce: prima scelta la voce neurale `it-IT-GiuseppeMultilingualNeural` tramite `edge-tts` (audio mp3 decodificato con `miniaudio` e suonato con `pyaudio`, su entrambi i sistemi). Riserva: macOS `say`; Windows sintetizzatore di sistema (System.Speech) tramite PowerShell, con la prima voce italiana installata. Alla prima frase in cui la voce neurale fallisce si passa alla riserva fino al riavvio. Prima di parlare il nome ("J.A.R.V.I.S.", "Jarvis") viene riscritto "Giarvis", la grafia che le voci italiane pronunciano come il nome inglese; sullo schermo e nell'HUD resta com'è.
- App: macOS `open -a` e `osascript ... quit`; Windows `os.startfile` e `taskkill /IM` senza `/F` (chiusura gentile).
- Batteria: macOS `pmset`; Windows `Win32_Battery` via PowerShell. Disco: `shutil.disk_usage` su entrambi.

## 4. Comunicazione con altri componenti
Nessun collegamento tra il Mac e il PC. Le comunicazioni esterne sono HTTPS verso l'API Anthropic e verso il servizio di trascrizione Google usato da SpeechRecognition.

L'unico punto in ascolto è il server dell'HUD: solo `127.0.0.1`, solo `GET /` e `GET /stato` (ogni altro metodo risponde 501), nessuna richiesta può comandare J.A.R.V.I.S. Rifiuta con 403 ogni header `Host` diverso da `127.0.0.1:<porta>` o `localhost:<porta>` (difesa dal DNS rebinding) e non invia intestazioni CORS, quindi i siti aperti nel browser non possono leggere la conversazione. La pagina ha una Content-Security-Policy che vieta risorse esterne.

## 5. Decisioni prese e perché
- Modello `claude-haiku-4-5-20251001`: scelto per latenza e costo; supporta il web search nella variante `web_search_20250305` (le varianti più recenti richiedono modelli più grandi).
- Chiave API letta da `ANTHROPIC_API_KEY`, mai scritta nel sorgente.
- Il saldo reale della Console non è leggibile con una chiave API normale (servirebbe l'Admin API con chiave amministratore): il credito residuo nell'HUD è una stima e va riallineato con `--credito` guardando la Console.
- Parole inglesi frequenti riscritte per la voce italiana (`PRONUNCIA`: "file" → "fàil" ecc.), perché nessuna voce maschile italiana disponibile le pronuncia all'inglese. Grafie da ritoccare a orecchio.
- Personalità sul modello del J.A.R.V.I.S. di Iron Man: maggiordomo inglese calmo e misurato, ironia asciutta e rara, dritto al punto, del Lei. "Signore" solo ogni tanto (saluto, momenti solenni): ripetuto in ogni frase stancava e la voce lo enfatizzava troppo. Le frasi fisse (avvio con saluto in base all'ora, pausa, errori) non lo usano, tranne il congedo.
- Con una voce neurale "Multilingual" non si applica `PRONUNCIA`: quelle voci leggono l'inglese da sole. "Giarvis" invece sì: in una frase italiana anche Giuseppe multilingue legge "Jarvis" come "Iarvis" (provato a orecchio: "Giarvis" e "Giàrvis" corretti). La riscrittura resta per le voci solo italiane e per la voce di sistema di riserva.
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
- Non dare a J.A.R.V.I.S. accesso in scrittura al proprio codice: `progetto_non_valido()` deve continuare a escludere la cartella del codice, `~/Jarvis` e la sandbox.
- Non far partire un lavoro di Claude Code senza la conferma vocale riconosciuta dal ciclo principale; non passare il compito tra gli argomenti (su Windows `claude` può essere un `.cmd`, e gli argomenti di un `.cmd` passano da cmd.exe); non togliere `--restricted` né aggiungere Bash agli strumenti; non lasciare `ANTHROPIC_API_KEY` nell'ambiente del processo.
- Non lanciare processi figli su Windows senza `creationflags=SENZA_FINESTRA`.
- Non usare `taskkill /F`: perde il lavoro non salvato.
- Non richiedere il permesso Accessibilità di macOS.
- Non aggiungere un collegamento di rete tra le due istanze senza una revisione di sicurezza dedicata.

## 7. Problemi aperti
- Credito API caricato (5 $, ricarica automatica disattivata di proposito: il credito precaricato è il tetto di spesa). Chiave `jarvis-pc` salvata come variabile d'ambiente utente su Windows.
- `portaudio` e `pyaudio` non ancora installati sul Mac.
- Permessi macOS non ancora concessi: Microfono per il Terminale, Automazione per `chiudi_app`.
- Windows (verificato il 25/09/2026 con Python 3.13): test automatici OK, voce italiana "Microsoft Elsa Desktop", microfono e trascrizione, apertura e chiusura di Blocco note, stato del sistema. Serve Python 3.13 (`py -3.13`): `pyaudio` 0.2.14 non ha pacchetti pronti per la 3.14.
- Windows: gli altri bersagli in `APP_WIN` non sono ancora stati provati; i nomi dei processi di Calcolatrice, Impostazioni e Spotify vanno confermati con Gestione attività.
- HUD nuovo (sfera 3D, pannelli, onda, segnali sonori) verificato anche su Windows dall'utente.
- Voce neurale verificata su Windows: l'utente ha scelto it-IT-GiuseppeMultilingualNeural tra le voci maschili multilingue. Grafie di `PRONUNCIA` non ancora ascoltate.
- Windows: ciclo completo verificato con chiamate API reali (domanda, strumento, risposta a voce). "Ehi Jarvis" da solo trascritto male da Google ("Ehi ya"): più affidabile "Jarvis" all'inizio di una frase.
- `installa.py` verificato su Windows (30/09/2026): pacchetti, collegamento sul desktop e avvio automatico creati con pywin32. La versione con PowerShell era stata bloccata ("Accesso negato").
- Verificato su Windows (30/09/2026) con avvio dall'icona, senza finestre: saluto a voce, "Ehi Jarvis" (segnale, "Mi dica", apertura dell'HUD), sfera sincronizzata con la voce neurale, pausa e risveglio.
- Claude Code dentro J.A.R.V.I.S.: implementato su richiesta esplicita dell'utente (dopo un primo blocco del controllo di sicurezza dell'ambiente di sviluppo, superato cambiando la modalità dei permessi della sessione). Sul PC di Jarvis `claude` 2.1.285 è installato (in `~\.local\bin`, aggiunto al PATH utente) e collegato all'abbonamento Max con `claude auth login` in una finestra senza `ANTHROPIC_API_KEY`. Prima prova reale fallita: per "nel progetto prova crea ciao.txt" il modello ha usato `scrivi_file` (file finito nella sandbox) e ha detto all'utente di averlo creato nel progetto. Corretto con: descrizioni degli strumenti che separano cartella di Jarvis e progetti, elenco dei progetti e regola esplicita aggiunti al prompt di sistema da `configura_claude_code()`, esito di `scrivi_file` che dice sempre dove ha salvato. Riprovato il 30/09/2026: il modello ha scelto `claude_code`, ha chiesto la conferma, "conferma" riconosciuta, Claude Code ha creato il file nel progetto e J.A.R.V.I.S. ha letto il riassunto a voce.
- Chiave API e Claude Code: l'utente usa Claude Code dal web (sessioni cloud, sempre con l'abbonamento Max) e dall'app desktop su un'altra macchina (Windows Server, abbonamento Max; l'app desktop non legge `ANTHROPIC_API_KEY`). Rischio solo se in futuro userà `claude` da terminale sul PC di Jarvis: lì, con la variabile impostata, può usare la chiave e il credito.
- Mai eseguito finora: tutto il lato Mac.
