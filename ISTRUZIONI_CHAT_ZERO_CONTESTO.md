# Istruzioni per una chat con 0 contesto

Questo file serve a dare a una nuova chat tutte le informazioni necessarie per capire, completare e verificare il progetto presente in questa directory.

## Obiettivo del progetto

Il progetto e uno starter kit Python per un **Research Digest Agent**: un agente che controlla fonti pubbliche, di solito feed RSS o Atom, su un tema specifico detto `beat`, seleziona le novita piu rilevanti e genera un digest Markdown strutturato.

Il progetto deve funzionare in due modalita:

- **Demo offline**, con `python main.py --demo`: usa il feed locale di esempio in `sample-output/sample-feed.xml`, non richiede internet e non richiede chiavi API.
- **Agente reale**, con `python main.py`: usa OpenAI Agents SDK, richiede `OPENAI_API_KEY` nel file `.env`, consulta le fonti configurate in `config.yaml`, deduplica le voci gia viste e produce un digest Markdown.

La consegna finale deve dimostrare che:

- il digest ha output strutturato e validato con Pydantic;
- l'agente non inventa contenuti, titoli, fonti o URL;
- le voci gia riportate non vengono riproposte nelle esecuzioni successive;
- gli errori sulle fonti non bloccano l'intera esecuzione;
- esiste un limite alle iterazioni/costi dell'agente;
- il progetto puo essere schedulato o eseguito periodicamente;
- i test automatici passano.

## Struttura della directory

```text
agent-starter-python/
+-- main.py
+-- config.yaml
+-- requirements.txt
+-- pytest.ini
+-- .env.example
+-- README.md
+-- data/
+-- examples/
|   +-- github-action-research-digest.yml
+-- sample-output/
|   +-- sample-feed.xml
|   +-- digest-esempio.md
+-- src/
|   +-- __init__.py
|   +-- agent.py
|   +-- config.py
|   +-- prompts.py
|   +-- schemas.py
|   +-- state.py
|   +-- tools/
|       +-- __init__.py
|       +-- dedup.py
|       +-- deliver.py
|       +-- fetch.py
+-- tests/
    +-- test_dedup.py
    +-- test_fetch.py
    +-- test_schemas.py
```

## Significato dei file principali

### `main.py`

E il punto di ingresso.

Responsabilita:

- carica `.env` con `load_dotenv()`;
- legge la configurazione con `load_config`;
- gestisce `--demo`;
- in demo esegue una pipeline deterministica senza LLM;
- in modalita agente costruisce l'agente con `build_agent`;
- limita le iterazioni con `max_turns`;
- applica un controllo di grounding sugli URL;
- tronca i tag a massimo 3;
- marca le voci come gia viste nel database SQLite;
- scrive il digest Markdown con `deliver_markdown`.

Comandi importanti:

```bash
python main.py --demo
python main.py
python main.py --config config.yaml
```

### `config.yaml`

Contiene la configurazione del beat.

Campi importanti:

- `beat`: tema presidiato dal digest;
- `beat_slug`: slug/tag breve del tema;
- `max_voci`: massimo numero di voci nel digest;
- `max_turns`: massimo numero di iterazioni dell'agente;
- `model`: modello usato in modalita agente;
- `db_path`: database SQLite per deduplica;
- `out_dir`: directory dei digest generati;
- `fonti`: elenco delle fonti RSS/Atom o file locali.

Per completare il progetto, adattare almeno:

```yaml
beat: "Tema scelto dal gruppo"
beat_slug: "tema-scelto"
fonti:
  - nome: "Nome fonte reale"
    url: "https://example.com/feed.xml"
    max: 10
```

Lasciare una fonte locale e utile per la demo offline, ma per il progetto finale servono fonti reali coerenti con il beat.

### `src/agent.py`

Costruisce l'agente con OpenAI Agents SDK.

Punti chiave:

- definisce lo strumento `cerca_novita`;
- `cerca_novita` chiama `fetch_candidates` e `filtra_nuove`;
- l'agente usa le istruzioni in `src/prompts.py`;
- l'output e vincolato allo schema `Digest`;
- il modello viene letto da `config.yaml`.

Non far inventare all'agente strumenti non presenti. Se si aggiungono nuovi strumenti, mantenerli deterministici e testabili.

### `src/prompts.py`

Contiene le istruzioni editoriali dell'agente.

Da personalizzare per il beat:

- cosa e rilevante;
- che tono usare;
- quali fonti preferire;
- quali elementi scartare;
- perche le notizie contano per KVA o per il contesto del progetto.

Regola fondamentale da mantenere:

- usare solo le voci restituite da `cerca_novita`;
- non inventare titoli, URL, fonti o contenuti;
- se non ci sono voci rilevanti, restituire un digest vuoto.

### `src/schemas.py`

Definisce gli schemi Pydantic:

- `DigestItem`;
- `Digest`.

Campi di ogni voce:

- `titolo`;
- `fonte`;
- `url`;
- `data`;
- `sintesi`;
- `perche_conta`;
- `tag`.

Non aggiungere campi casuali senza aggiornare test, prompt, output e documentazione.

### `src/config.py`

Carica e valida `config.yaml`.

Comportamenti importanti:

- `beat` e `fonti` sono obbligatori;
- i percorsi locali vengono risolti rispetto alla directory del file config;
- imposta default per `out_dir`, `db_path`, `max_voci`, `max_turns`.

### `src/state.py`

Gestisce la memoria dei duplicati con SQLite.

Funzioni importanti:

- `is_seen(url)`;
- `mark_seen(url, beat)`;
- `count()`;
- `close()`.

Il dedup e globale per URL. Non schedulare due esecuzioni sovrapposte sullo stesso database.

### `src/tools/fetch.py`

Legge fonti RSS/Atom o file locali con `feedparser`.

Produce oggetti `Candidate` con:

- `titolo`;
- `url`;
- `fonte`;
- `data`;
- `estratto`.

Gestisce errori sulle fonti stampando un avviso e continuando con le altre fonti.

### `src/tools/dedup.py`

Rimuove:

- voci senza URL;
- duplicati nella stessa esecuzione;
- URL gia presenti nel database SQLite.

### `src/tools/deliver.py`

Scrive il digest Markdown in `out_dir`.

Il file generato ha nome:

```text
digest-{beat-normalizzato}.md
```

Esempio:

```text
out/digest-ecosistema-agenti-mcp.md
```

## Setup ambiente

Usare Python 3.10 o superiore.

Su Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Su macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Per la modalita agente reale:

```bash
cp .env.example .env
```

Poi inserire nel file `.env`:

```env
OPENAI_API_KEY=...
```

Non committare o condividere il file `.env`.

## Procedura consigliata per completare il progetto

1. Leggere `README.md`.
2. Eseguire i test iniziali:

```bash
python -m pytest -q
```

3. Eseguire la demo offline:

```bash
python main.py --demo
```

4. Controllare il digest generato nella directory `out/`.
5. Scegliere il beat finale del gruppo.
6. Aggiornare `config.yaml` con `beat`, `beat_slug`, `max_voci`, `max_turns`, `model` e fonti reali.
7. Aggiornare `src/prompts.py` con criteri editoriali specifici del beat.
8. Se si usa la modalita agente, creare `.env` con `OPENAI_API_KEY`.
9. Eseguire:

```bash
python main.py
```

10. Verificare che il digest generato contenga solo URL presenti nelle fonti.
11. Eseguire nuovamente `python main.py` e verificare che le stesse voci non vengano riproposte.
12. Eseguire ancora i test:

```bash
python -m pytest -q
```

13. Preparare la schedulazione con cron o GitHub Actions.
14. Documentare costo stimato, modello scelto, frequenza di esecuzione e canale di consegna.

## Personalizzazione del beat

Scegliere un tema abbastanza specifico da produrre un digest utile.

Esempi buoni:

- "Regolazione europea sull'intelligenza artificiale";
- "Sicurezza software supply chain";
- "Agenti AI e protocolli MCP";
- "Strumenti open source per data journalism";
- "Climate tech in Europa".

Esempi troppo vaghi:

- "Tecnologia";
- "AI";
- "News";
- "Economia".

Aggiornare `config.yaml`:

```yaml
beat: "Agenti AI e protocolli MCP"
beat_slug: "agenti-ai-mcp"
max_voci: 5
max_turns: 4
model: "gpt-4.1-mini"
db_path: "data/seen.sqlite3"
out_dir: "out"

fonti:
  - nome: "OpenAI Blog"
    url: "https://openai.com/blog/rss.xml"
    max: 10
  - nome: "Anthropic News"
    url: "https://www.anthropic.com/news/rss.xml"
    max: 10
```

Verificare sempre che gli URL dei feed siano realmente leggibili. Se una fonte non espone RSS/Atom, sostituirla con una fonte che lo fa oppure aggiungere un nuovo tool dedicato, mantenendo test e gestione errori.

## Personalizzazione delle istruzioni editoriali

Modificare `src/prompts.py` mantenendo lo schema generale.

Le istruzioni dovrebbero dire:

- quale pubblico legge il digest;
- cosa considerare rilevante;
- cosa ignorare;
- che stile usare;
- come scrivere la sintesi;
- come scrivere `perche_conta`;
- quali tag usare;
- che ogni voce deve essere basata su URL reali restituiti dal tool.

Esempio di criteri:

```text
Seleziona notizie con impatto pratico su prodotto, ricerca, policy o adozione.
Scarta comunicati puramente promozionali, tutorial troppo generici e duplicati.
Scrivi in italiano, con tono asciutto, concreto e non pubblicitario.
```

## Grounding e anti-allucinazione

Il progetto contiene due livelli di protezione:

1. Nel prompt, l'agente deve usare solo le voci restituite da `cerca_novita`.
2. In `main.py`, dopo la risposta dell'agente, vengono mantenute solo le voci il cui URL e presente tra i candidati raccolti da `fetch_candidates`.

Non rimuovere questo controllo.

Se si modifica `fetch_candidates`, assicurarsi che gli URL siano normalizzati in modo coerente, altrimenti il grounding potrebbe scartare voci valide.

## Deduplica

La deduplica funziona cosi:

- nella stessa esecuzione, `filtra_nuove` elimina URL ripetuti;
- tra esecuzioni diverse, `SeenStore` salva gli URL in SQLite;
- dopo la consegna, `main.py` marca ogni voce del digest come vista.

Per testare manualmente:

```bash
python main.py
python main.py
```

La seconda esecuzione non dovrebbe riproporre le stesse voci, salvo cambiamenti nelle fonti o reset del database.

Per resettare la memoria durante sviluppo, eliminare il file indicato da `db_path`, per esempio:

```powershell
Remove-Item .\data\seen.sqlite3
```

Usare questa operazione solo consapevolmente, perche cancella la memoria delle voci gia viste.

## Test automatici

I test esistenti coprono:

- schema Pydantic del digest;
- lettura di un feed locale;
- deduplica persistente e nella stessa run.

Comando:

```bash
python -m pytest -q
```

Se si aggiungono funzionalita, aggiungere test coerenti:

- nuovo tool di fetch: testare input valido, input vuoto, errore fonte;
- nuovo canale di consegna: testare creazione output e formato;
- modifica schema: aggiornare `tests/test_schemas.py`;
- modifica deduplica: aggiornare `tests/test_dedup.py`;
- modifica parsing feed: aggiornare `tests/test_fetch.py`.

## Schedulazione

Il progetto deve poter girare periodicamente.

Opzione cron su Linux/macOS:

```cron
0 8 * * 1 cd /percorso/agent-starter-python && .venv/bin/python main.py
```

Questo esempio esegue l'agente ogni lunedi alle 08:00.

Opzione GitHub Actions:

- usare il template in `examples/github-action-research-digest.yml`;
- copiarlo in `.github/workflows/`;
- configurare il secret `OPENAI_API_KEY`;
- verificare che l'action installi le dipendenze e lanci `python main.py`;
- decidere come conservare o pubblicare il digest generato.

## Consegna del digest

Il canale gia implementato e Markdown locale tramite `src/tools/deliver.py`.

Per una consegna piu completa si puo aggiungere:

- invio email;
- pubblicazione su Slack/Discord;
- commit automatico del digest in repository;
- upload come artifact in GitHub Actions;
- salvataggio in una cartella condivisa.

Se si aggiunge un canale, non eliminare il Markdown locale: e utile per debug, test e valutazione.

## Stima dei costi

Il progetto limita i costi con:

- `max_turns` in `config.yaml`;
- `max_voci`;
- modello configurabile, per esempio `gpt-4.1-mini`;
- fetch deterministico prima della chiamata al modello.

Per completare la parte di stima:

1. Annotare modello usato.
2. Annotare frequenza di esecuzione, per esempio una volta a settimana.
3. Annotare numero massimo di turni.
4. Eseguire una run reale e controllare l'uso nella dashboard del provider.
5. Scrivere nel README o in una nota finale una stima mensile.

Esempio di nota:

```text
Il digest gira una volta a settimana con modello gpt-4.1-mini, max_turns=4 e massimo 5 voci.
La spesa viene tenuta bassa limitando fonti, voci e iterazioni. Dopo la prima esecuzione reale,
verificare il costo effettivo nella dashboard OpenAI e riportare una stima mensile.
```

## Criteri di accettazione finale

Il progetto puo considerarsi completato quando:

- `python -m pytest -q` passa;
- `python main.py --demo` genera un digest in `out/`;
- `config.yaml` contiene un beat reale e fonti coerenti;
- `src/prompts.py` contiene criteri editoriali specifici;
- `python main.py` funziona con `.env` configurato;
- il digest finale e in Markdown e contiene titolo, fonte, link, data, sintesi, perche conta e tag;
- ogni link del digest proviene dalle fonti raccolte;
- una seconda esecuzione non ripropone le stesse voci;
- gli errori di una fonte non bloccano le altre;
- `max_turns` e impostato;
- esiste una strategia di schedulazione;
- esiste una breve stima dei costi.

## Prompt pronto da dare a una nuova chat

Usa questo prompt se devi consegnare il progetto a una chat senza contesto:

```text
Sei in una directory Python chiamata agent-starter-python. Devi completare un Research Digest Agent.

Prima leggi README.md, config.yaml, main.py, src/agent.py, src/prompts.py, src/schemas.py, src/config.py, src/state.py, src/tools/fetch.py, src/tools/dedup.py, src/tools/deliver.py e i test in tests/.

Il progetto deve generare un digest Markdown da fonti RSS/Atom su un beat scelto. Deve funzionare in demo offline con `python main.py --demo` e in modalita agente con `python main.py` usando OPENAI_API_KEY in `.env`.

Compiti:
1. Verifica che i test passino con `python -m pytest -q`.
2. Esegui la demo offline con `python main.py --demo`.
3. Personalizza `config.yaml` con un beat reale, beat_slug, max_voci, max_turns, model e fonti RSS/Atom reali.
4. Personalizza `src/prompts.py` con criteri editoriali specifici del beat.
5. Mantieni il grounding: l'agente deve usare solo URL restituiti da `cerca_novita`; non deve inventare fonti o link.
6. Mantieni la deduplica SQLite tramite `SeenStore`.
7. Verifica la modalita agente con `python main.py` se la chiave API e disponibile.
8. Verifica che il digest sia scritto in `out/` e contenga titolo, fonte, link, data, sintesi, perche conta e tag.
9. Documenta schedulazione e stima dei costi.
10. Alla fine riesegui `python -m pytest -q` e riassumi cosa e stato completato.

Non rimuovere demo offline, test, Pydantic schema, controllo grounding o deduplica.
Non committare `.env`.
```

## Note operative per la chat

- Preferire modifiche piccole e verificabili.
- Usare i pattern gia presenti nel codice.
- Non cambiare schema, formato output o comportamento di deduplica senza aggiornare test e documentazione.
- Se una fonte reale non funziona, sostituirla con un feed valido invece di forzare parsing fragile.
- Se manca la chiave API, completare e verificare almeno demo offline e test.
- Se si lavora su Windows, usare PowerShell e percorsi coerenti.
