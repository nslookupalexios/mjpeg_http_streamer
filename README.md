# MJPEG Frame Streaming Server

## Descrizione generale

Questo progetto implementa una **web-application di streaming video MJPEG** basata su HTTP (`multipart/x-mixed-replace`), progettata per **trasmettere in tempo quasi real-time** una sequenza di immagini JPEG generate da un sistema esterno e depositate in una directory del filesystem.

Il sistema è stato concepito con l’obiettivo primario di:

* **minimizzare la latenza end-to-end**,
* **scartare automaticamente i frame obsoleti**,
* mantenere **complessità architetturale ridotta**,
* consentire **facile integrazione con sistemi target embedded o vision-based**.

Lo stream espone **sempre e solo il frame più recente disponibile**, senza alcun buffering FIFO lato server.

---

## Architettura del sistema

### Schema logico

```
+------------------+        filesystem        +-----------------------+
| Frame Producer   |  --->  directory JPG  -> | MJPEG Streaming Server|
| (target system   |                          |  - watchdog           |
|  o simulator)    |                          |  - latest-frame cache |
+------------------+                          |  - HTTP MJPEG stream  |
                                              +-----------+-----------+
                                                          |
                                                          | HTTP
                                                          v
                                              +-----------------------+
                                              | Web Client (Browser)  |
                                              | <img src="/stream">   |
                                              +-----------------------+
```

### Componenti principali

1. **Frame Producer**

   * Sistema esterno (reale o simulato) che genera file `.jpg` in una directory.
   * Non esistono assunzioni sul naming dei file, salvo l’estensione.

2. **Directory Watcher**

   * Implementato tramite `watchdog` (inotify su Linux).
   * Intercetta creazione, modifica o rename di file JPEG.
   * Valida e decodifica le immagini prima di renderle disponibili allo stream.

3. **Latest Frame Cache**

   * Un singolo slot in memoria contenente:

     * l’ultimo frame JPEG valido,
     * un contatore di sequenza monotono.
   * Garantisce che **i frame vecchi vengano automaticamente scartati**.

4. **HTTP MJPEG Stream**

   * Endpoint `/stream`.
   * Trasmissione tramite `multipart/x-mixed-replace`.
   * Rate limit configurabile (fps).
   * Ogni client riceve il frame più recente disponibile al momento dell’invio.

---

## Proprietà funzionali chiave

* **No buffering FIFO**: il sistema non accumula frame.
* **Drop automatico**: se il producer è più veloce del client, i frame intermedi vengono persi intenzionalmente.
* **Latenza ridotta**: tipicamente limitata a:

  * tempo di scrittura del frame,
  * watcher delay,
  * periodo di refresh dello stream.
* **Stateless per-client**: nessuna coda per singolo client.
* **Robustezza a file incompleti**: tentativi di decodifica difensivi.

---

## Requisiti di sistema

### Software

* Python ≥ 3.9
* FFmpeg (solo per lo script di simulazione)
* Browser moderno (Chrome, Firefox, Chromium)

### Librerie Python

```bash
pip install fastapi uvicorn watchdog pillow
```

---

## Configurazione

La configurazione è **hard-coded in cima al file** `mjpeg_server.py`.

```python
# =============================================================================
# Configuration
# =============================================================================
FRAMES_DIR_ABS: str = "/absolute/path/to/frames"
TARGET_FPS: float = 20.0
```

### Vincoli obbligatori

* `FRAMES_DIR_ABS`:

  * può avere **nome arbitrario**;
  * può essere **qualsiasi path assoluto o relativo**;
  * **deve esistere già** al momento dell’avvio;
  * se non esiste o non è una directory → **il programma termina immediatamente**.

---

## Avvio del server

```bash
uvicorn mjpeg_server:app --host 0.0.0.0 --port 8000
```

Se la directory non esiste:

```
ERROR: FRAMES_DIR_ABS does not exist or is not a directory
```

e il processo termina.

---

## Endpoint disponibili

| Endpoint  | Metodo | Descrizione                           |
| --------- | ------ | ------------------------------------- |
| `/`       | GET    | Pagina HTML minimale con viewer MJPEG |
| `/stream` | GET    | Stream MJPEG multipart                |

### Visualizzazione rapida

Aprire nel browser:

```
http://<host>:8000/
```

---

## Simulazione del Frame Producer

La simulazione della generazione dei frame viene effettuata tramite **script Bash basato su FFmpeg**, che estrae una sequenza di immagini JPEG da un file MP4.


### Utilizzo

```bash
./mp4_to_jpg_frames.sh input.mp4 /path/to/frames 20
```

Lo script:

* genera una sequenza di file `frame_XXXXXX.jpg`;
* crea la directory di output se non esiste;
* è adatto a simulare un **producer offline o batch**.

> Nota: lo script non usa `-re`, quindi l’estrazione non è real-time.
> Per una simulazione temporale realistica, è possibile aggiungere `-re` al comando `ffmpeg`.

## Considerazioni di progetto

### Perché MJPEG e non HLS/WebRTC

* Nessun signaling
* Nessuna dipendenza da codec hardware
* Debug immediato
* Ideale per:

  * prototipi,
  * sistemi di supervisione,
  * tool di debug remoto,
  * ambienti LAN / laboratorio

### Limitazioni note

* Banda proporzionale al numero di client
* Nessuna compressione temporale inter-frame
* Non adatto a deployment Internet su larga scala

---

## Buone pratiche lato producer

Per massima affidabilità:

* scrivere su file temporaneo (`.tmp`);
* `fsync`;
* rename atomico in `.jpg`.

Questo evita completamente la lettura di file parziali.

---

## Estensioni possibili

* Supporto PNG → JPEG automatico
* Timestamp nel filename e ordering semantico
* Autodelete dei frame più vecchi
* Rate adattivo
* Autenticazione endpoint
* Proxy reverse (Nginx)

---

## Licenza

Il codice è fornito **as-is**, senza garanzie implicite, ed è pensato per **uso di ricerca, sviluppo e prototipazione**.