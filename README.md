# Research Digest Agent - Ricerca AI / modelli

Starter kit Python per generare un digest Markdown su un beat specifico. Il beat configurato e **Ricerca AI / modelli**: paper rilevanti, rilasci di modelli, benchmark, tecniche di training, sicurezza e miglioramenti di efficienza.

## Modalita di esecuzione

Demo offline, senza internet e senza chiavi API:

```bash
python main.py --demo
```

Agente reale:

```bash
python main.py
python main.py --config config.yaml
```

Il comando `python -m src.main` resta supportato per compatibilita.

## Setup

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Per la modalita reale crea `.env` partendo da `.env.example` e imposta almeno una chiave del provider scelto:

```env
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
```

Il file `.env` e escluso da Git.

### Interfaccia web dinamica

Avvia la GUI con:

```bash
python gui.py
```

Apri `http://127.0.0.1:8765`. La UI principale e una shell HTML/CSS/JavaScript
vanilla servita da `src/web/`: conserva canvas neurale, robot animato, sidebar,
card e impostazioni, ma carica tutti i contenuti da API e non usa `localStorage`
per dati di account. Non e richiesto un bundler: i moduli ES e gli asset sono
serviti direttamente dal server Python.

Il bootstrap legge sempre `/api/config`; il prefisso effettivo delle API,
timeout, ambiente e fuso orario sono configurabili nella sezione `web` di
`config.yaml` o con le variabili `RESEARCH_DIGEST_API_BASE_URL`,
`RESEARCH_DIGEST_REQUEST_TIMEOUT_MS` e `RESEARCH_DIGEST_ENV`. Un prefisso
relativo come `/api/v2` viene instradato dal server senza modificare il client.

Le API browser principali sono:

- `GET /api/me`, `POST /api/auth/login`, `POST /api/auth/register`, `POST /api/auth/logout`;
- `GET /api/digest/current`, `GET /api/digest`, `GET /api/digests` (ricerca e paginazione);
- `GET`/`POST /api/saved` per i salvataggi per account;
- `GET /api/settings` e `POST /api/settings/profile`, `/preferences`, `/suspension`;
- `POST /api/newsletter`, `GET /api/telegram/onboarding` e API push quando FCM e configurato.

Le risposte account-specific sono `private, no-store`, le mutazioni verificano
l'origine della richiesta e le sessioni restano in cookie `HttpOnly` con
`SameSite=Lax`.

### Newsletter via Gmail

La GUI legge automaticamente `.env` quando avvii:

```bash
python gui.py
```

Per mantenere funzionante la newsletter anche dopo aver esportato o copiato il progetto, conserva anche il file `.env` nella cartella esportata oppure ricrealo partendo da `.env.example`.

Configurazione Gmail:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM=tua-email@gmail.com
SMTP_USERNAME=tua-email@gmail.com
SMTP_PASSWORD=password-per-app-google
SMTP_USE_TLS=true
```

`SMTP_PASSWORD` deve essere una password per app Google, non la password normale dell'account. La pagina `/settings` invia subito il digest disponibile se SMTP è configurato; altrimenti salva il messaggio in `data/newsletter_outbox/`.

L'oggetto di ogni newsletter è **Le principali novità AI della settimana**. Il corpo indica sempre il periodo esatto di raccolta. L'invio pianificato parte il venerdì alle 09:00 Europe/Rome, subito dopo l'aggiornamento del digest della homepage, e raccoglie le notizie da lunedì a venerdì inclusi. L'invio immediato richiesto da un utente appena iscritto copre invece da lunedì fino al giorno dell'iscrizione.

### Newsletter via Telegram

La pagina `/settings` attiva il digest della settimana corrente anche su Telegram.

1. Crea un bot con `@BotFather` su Telegram.
2. Copia il token del bot nel file `.env`.
3. Copia anche lo username del bot, senza `@`, per generare il link nella pagina impostazioni.
4. Non è necessario scrivere manualmente al bot: il flusso `/start` è gestito dal pulsante del sito.

Nel file `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:token-del-bot
TELEGRAM_BOT_USERNAME=nome_del_tuo_bot
```

Poi avvia:

```bash
python gui.py
```

Nella pagina `/settings` clicca **Apri Telegram** e, nella chat del bot, premi **Start**. Il collegamento usa il protocollo nativo Telegram su mobile e desktop; se l'app o il client desktop non sono disponibili, passa automaticamente al link ufficiale Telegram Web. L'`href` resta HTTPS anche senza JavaScript.

Il link associato a un account contiene un intent casuale, monouso e valido per 15 minuti: non espone l'ID dell'account. Dopo **Start**, il server verifica che il comando arrivi da una chat privata, registra l'identità Telegram, collega la chat all'account, abilita il canale Telegram e invia subito il digest della settimana ISO corrente. Un nuovo `/start` reinvia il digest solo su richiesta esplicita dell'utente; timeout, limiti API e invii parziali restano in coda per un nuovo tentativo.

Telegram richiede comunque la pressione del proprio pulsante **Start** nel client: un sito web non può inviare `/start` automaticamente a nome dell'utente. Dopo quel gesto nativo non è necessario alcun comando aggiuntivo.

Di default il server esegue il polling del bot (`TELEGRAM_POLLING_ENABLED=true`). L'applicazione usa inoltre un lock locale per impedire a due processi dello stesso progetto di eseguire `getUpdates` insieme. In un deploy con più processi, disabilitalo nei processi web e avvia **un solo** worker dedicato con:

```bash
python -m src.telegram_worker
```

Non configurare un webhook Telegram insieme a questo worker, perché Telegram non consente `getUpdates` e webhook contemporaneamente. Il worker e il processo che genera il digest devono condividere una directory `data/` persistente: contiene iscrizioni, intent e stato di consegna, ed è volutamente esclusa da Git. Quando il digest viene generato, gli iscritti ricevono il riepilogo settimanale una sola volta per chat e settimana il venerdì alle 09:00 Europe/Rome; la giornata è definita una sola volta in `schedule.weekly_delivery_day` di `config.yaml`. Il digest pianificato raccoglie da lunedì a venerdì inclusi. Se manca il digest della settimana al primo Start, la generazione viene avviata automaticamente e ritentata in modo limitato; il primo messaggio copre da lunedì al giorno di avvio del bot.

Imposta `TELEGRAM_LOG_LEVEL=INFO` per registrare il percorso `/start`, il recupero del digest, il numero di voci, ogni richiesta e risposta `sendMessage`, gli ID dei messaggi Telegram e gli errori con stack trace sanitizzato. I log mascherano chat ID e token.

La GitHub Action inclusa è adatta alla generazione e al commit del digest, ma non condivide automaticamente questo stato privato con il server web. Per consegnare newsletter e Telegram in produzione, pianifica `python -m src.scheduled_run` sullo stesso host o volume persistente del worker, con SMTP e `TELEGRAM_BOT_TOKEN` disponibili nel suo ambiente.

### Firebase Cloud Messaging

L'integrazione FCM e pronta per il deploy ma disattivata di default. Non viene eseguito alcun deploy da questo progetto: quando pubblicherai il sito in HTTPS e abiliterai Firebase, la pagina `/settings` permettera al browser dell'utente di registrarsi alle notifiche push.

Il flusso gia predisposto e:

1. `/api/firebase/config` espone solo la configurazione pubblica Web Firebase.
2. `/firebase-messaging-sw.js` serve il service worker per ricevere notifiche in background.
3. Il pulsante "Attiva notifiche push" in `/settings` chiede il permesso al browser e salva il token FCM in `data/firebase/fcm_tokens.json`, associato all'account corrente.
4. Dopo una generazione digest riuscita, `python main.py` invia una push agli utenti registrati se Firebase è attivo e le notifiche non sono sospese.

Nel file `config.yaml` la sezione `notifications.firebase` contiene:

- `enabled: false`, per evitare invii accidentali;
- `credentials_path`, percorso locale del JSON del service account Firebase Admin SDK;
- `token_store_path`, percorso locale dove vengono salvati i token FCM degli utenti;
- `default_topic`, topic predefinito per notifiche aggregate;
- `web`, placeholder della configurazione Web Firebase e della chiave VAPID pubblica.

Nel file `.env` puoi preparare gli stessi valori partendo da `.env.example`:

```env
FIREBASE_ENABLED=false
FIREBASE_PROJECT_ID=your-firebase-project-id
FIREBASE_CREDENTIALS_PATH=data/firebase/service-account.json
FIREBASE_TOKEN_STORE_PATH=data/firebase/fcm_tokens.json
FIREBASE_DEFAULT_TOPIC=research-digest
FIREBASE_WEB_API_KEY=your-web-api-key
FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
FIREBASE_STORAGE_BUCKET=your-project.appspot.com
FIREBASE_MESSAGING_SENDER_ID=123456789012
FIREBASE_APP_ID=1:123456789012:web:abcdef123456
FIREBASE_MEASUREMENT_ID=G-XXXXXXXXXX
FIREBASE_VAPID_KEY=your-web-push-vapid-public-key
```

Il file del service account e l'archivio locale dei token sono ignorati da Git tramite `.gitignore`. Quando passerai all'attivazione:

- crea un progetto Firebase;
- aggiungi una Web app e copia i valori pubblici nella sezione `FIREBASE_WEB_*`;
- genera una Web Push certificate/VAPID key e copiala in `FIREBASE_VAPID_KEY`;
- scarica il JSON del service account Firebase Admin SDK in `data/firebase/service-account.json`;
- imposta `FIREBASE_ENABLED=true`;
- assicurati che il sito sia servito in HTTPS, tranne durante i test su `localhost`.

### Sospensione notifiche

La pagina `/settings` include un modulo "Sospensione notifiche" con calendario a intervallo. Le impostazioni della UI vengono salvate per account in `data/user_notification_suspensions.json`, restano valide dopo logout o riavvio e usano esplicitamente il fuso `web.timezone` (UTC per default). Durante l'intervallo configurato non vengono inviati newsletter, Telegram o push per quell'account; alla data di fine l'invio riprende automaticamente senza recuperare le notifiche saltate. Il precedente controllo globale in `data/notification_suspension.json` resta disponibile solo per i flussi amministrativi/legacy, non per le impostazioni web utente.

## Configurazione

`config.yaml` contiene beat, fonti, allowlist dei domini, modello, budget, destinazione Markdown e database SQLite. Il progetto usa LiteLLM: `cost_controls.model` e il modello primario, `fallback_models` contiene i fallback.

Le fonti operative sono caricate da `data/source_catalog.yaml` e filtrate con:

- `trust_score`;
- `source_catalog.authoritative_domains`;
- `allow_unlisted_domains: false`;
- controllo URL pubblici HTTP(S).

I campi `beat_slug`, `max_voci`, `max_turns`, `model` e `fonti` sono presenti anche per rendere la configurazione leggibile secondo le istruzioni del progetto.

## Garanzie anti-allucinazione

- Il modello puo usare solo gli URL candidati raccolti nella run.
- Ogni voce deve essere verificata con `fetch_url`.
- Ogni voce deve includere una `evidence` letterale presente nella pagina letta.
- Il codice sovrascrive titolo, fonte e URL con i dati raccolti, non con quelli inventati dal modello.
- Lo schema Pydantic vieta campi extra e valida lunghezza, date e relevance score.
- Gli URL consegnati vengono salvati in SQLite e non sono riproposti nelle run successive.

## Output

Il canale principale e Markdown locale tramite `src/tools/delivery.py`.

- Demo: directory `out/`.
- Run reale: directory configurata in `delivery.primary.path`, oggi `data/digests/`.
- Fallimenti di consegna: `delivery.failed_path`.

Ogni voce contiene titolo, fonte, URL, data, sintesi, perche conta, categoria e rilevanza.

## Test

```bash
python -m pytest -q
```

I test coprono schema, fetch, deduplica, grounding, fallback modello, consegna Markdown e integrazione con server HTTP finto.

## Schedulazione

La homepage viene aggiornata dal lunedì al venerdì alle **09:00 Europe/Rome**. Nella stessa run del venerdì vengono inviate newsletter e digest Telegram, dopo che il file del digest è stato scritto. Le ricevute per destinatario e settimana impediscono duplicati in caso di ritentativo.

La GitHub Action `.github/workflows/digest.yml` si attiva alle 07:00 e alle 08:00 UTC nei feriali e `src.scheduled_run` esegue l'agente solo quando è davvero le 09:00 a Roma: questo conserva l'orario sia in CEST sia in CET. Un template copiabile è anche in `examples/github-action-research-digest.yml`.

Per l'invio reale dei destinatari del sito, usa il job sullo stesso host persistente. Su questo PC lo script è `scripts/run-scheduled-digest.ps1` e deve essere registrato in Utilità di pianificazione alle 09:00, dal lunedì al venerdì.

Per cron:

```cron
0 9 * * 1-5 cd /percorso/research-digest-agent && .venv/bin/python -m src.scheduled_run
```

Evitare esecuzioni sovrapposte sullo stesso database SQLite.

## Stima costi

Configurazione corrente:

- modello primario: `gemini/gemini-3.5-flash`;
- fallback: `gemini/gemini-3.1-flash-lite`, `gemini/gemini-flash-latest`;
- massimo 12 iterazioni agente;
- massimo 10 voci consegnate;
- massimo 3 letture approfondite `fetch_url`;
- tetto run: 120.000 token e 0,50 USD.

Con schedulazione feriale, il tetto teorico massimo e circa 11 USD/mese, ma il costo reale dovrebbe restare piu basso perche la run si ferma appena consegna il digest. Dopo una run reale, verificare la dashboard del provider e aggiornare questa stima con il costo effettivo.

## File principali

- `main.py`: entrypoint richiesto dalle istruzioni.
- `src/main.py`: CLI, demo offline e avvio agente reale.
- `src/agent.py`: loop LiteLLM con tool calling, grounding e budget.
- `src/prompts.py`: criteri editoriali del beat.
- `src/schemas.py`: schema Pydantic del digest.
- `src/tools/fetch.py`: RSS, scraping controllato e verifica URL.
- `src/tools/dedup.py`: deduplica persistente SQLite.
- `src/tools/delivery.py`: rendering e consegna Markdown.
