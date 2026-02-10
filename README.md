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

## Installazione automatica come servizio systemd

Il progetto include uno **script di installazione automatica** (`install_service.sh`) che configura il server MJPEG come **servizio systemd**, garantendo:

* avvio automatico al boot,
* gestione centralizzata tramite `systemctl`,
* logging via `journalctl`,
* isolamento dell'ambiente Python (virtualenv).

### Esecuzione dell'installazione

```bash
./install_service.sh
```

> **IMPORTANTE**: Lo script **deve essere eseguito come utente normale**, NON come root.
> Se necessario, lo script richiederà automaticamente i privilegi sudo per le operazioni che lo richiedono.

### Cosa fa lo script

1. **Installazione dipendenze di sistema** (via `apt`)

   * `python3`, `python3-venv`, `python3-pip`
   * `fonts-dejavu-core` (per il rendering del placeholder "NO FRAME")
   * `libjpeg-turbo8`, `zlib1g`, `libfreetype6` (dipendenze runtime di Pillow)

2. **Creazione del virtualenv**

   * Crea `.venv` nella directory del progetto
   * Installa le dipendenze Python (`fastapi`, `uvicorn[standard]`, `pillow`, `watchdog`)

3. **Generazione del file di configurazione**

   * Copia `config.env.base` → `config.env`
   * Popola automaticamente le seguenti variabili:
     * `INVOKING_USER`: l'utente che ha eseguito lo script
     * `MJPEG_HTTP_STREAMER_DIR`: path assoluto del repository
     * `PROJECT_DIR`, `VENV_DIR`, `PYTHON_BIN`, `UVICORN_BIN`: path di sistema
     * `FRAMES_DIR_ABS`: default a `<project>/images` se non impostato
   * Crea la directory `images/` se non esiste

4. **Installazione del servizio systemd**

   * Crea `/etc/systemd/system/mjpeg-server.service`
   * Configurato per:
     * Caricare le variabili d'ambiente da `config.env` (via `EnvironmentFile`)
     * Eseguire come utente non privilegiato
     * Restart automatico in caso di failure
     * Sandboxing (`NoNewPrivileges`, `PrivateTmp`)

5. **Abilitazione e avvio del servizio**

   * `systemctl enable mjpeg-server.service`
   * `systemctl start mjpeg-server.service`
   * Mostra lo stato finale e i log recenti

### Personalizzazione della configurazione

Dopo l'installazione, è possibile modificare `config.env` per personalizzare:

* `HOST`: indirizzo di bind (default: `0.0.0.0`)
* `PORT`: porta TCP (default: `8000`)
* `TARGET_FPS`: frame rate dello stream (default: `20.0`)
* `MAX_FRAME_AGE_S`: età massima dei frame su disco prima della rimozione automatica (default: `10.0`)
* `FRAMES_DIR_ABS`: directory contenente i frame JPEG

Dopo la modifica, riavviare il servizio:

```bash
sudo systemctl restart mjpeg-server.service
```

### Gestione del servizio

```bash
# Stato del servizio
sudo systemctl status mjpeg-server.service

# Visualizzare i log
sudo journalctl -u mjpeg-server.service -f

# Fermare il servizio
sudo systemctl stop mjpeg-server.service

# Riavviare il servizio
sudo systemctl restart mjpeg-server.service

# Disabilitare l'avvio automatico
sudo systemctl disable mjpeg-server.service
```

---

## Disinstallazione del servizio

Per rimuovere completamente il servizio systemd:

```bash
./uninstall_services.sh
```

### Cosa fa lo script

1. **Arresto del servizio** (`systemctl stop`)
2. **Disabilitazione dell'avvio automatico** (`systemctl disable`)
3. **Reset dello stato di failure** (`systemctl reset-failed`)
4. **Rimozione del file unit** (`/etc/systemd/system/mjpeg-server.service`)
5. **Reload della configurazione systemd** (`systemctl daemon-reload`)

### Rimozione completa degli artefatti locali

Di default, lo script **non rimuove** i file creati nella directory del progetto (`.venv`, `config.env`, `images/`).

Per eliminare anche questi artefatti:

```bash
PURGE_LOCAL_ARTIFACTS=1 ./uninstall_services.sh
```

Questo comando rimuove:

* `.venv/` (virtualenv)
* `config.env` (configurazione generata)
* `images/` (directory frame, solo se dentro il progetto)

---

## Configurazione manuale (senza systemd)

Se si preferisce non utilizzare systemd, è possibile configurare manualmente il sistema.

### Setup virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install fastapi "uvicorn[standard]" pillow watchdog
```

### Configurazione

#### Variabili d'ambiente supportate

Il server legge le seguenti variabili d'ambiente all'avvio:

| Variabile         | Default            | Descrizione                                     |
| ----------------- | ------------------ | ----------------------------------------------- |
| `FRAMES_DIR_ABS`  | `/tmp/mjpeg_frames`| Directory assoluta contenente i frame JPEG      |
| `TARGET_FPS`      | `20.0`             | Frame rate target dello stream                  |
| `MAX_FRAME_AGE_S` | `10.0`             | Età massima (secondi) dei frame prima della pulizia |

#### Esempio con variabili d'ambiente

```bash
export FRAMES_DIR_ABS="/home/user/my_frames"
export TARGET_FPS="30.0"
export MAX_FRAME_AGE_S="5.0"

# Avvio manuale
.venv/bin/uvicorn mjpeg_server:app --host 0.0.0.0 --port 8000
```

#### Modifica diretta del codice

In alternativa, le costanti possono essere modificate direttamente in [mjpeg_server.py](mjpeg_server.py#L19-L21):

```python
# =============================================================================
# Configuration
# =============================================================================
FRAMES_DIR_ABS: str = os.getenv("FRAMES_DIR_ABS", "/tmp/mjpeg_frames")
TARGET_FPS: float = float(os.getenv("TARGET_FPS", "20.0"))
MAX_FRAME_AGE_S: float = float(os.getenv("MAX_FRAME_AGE_S", "10.0"))
```

### Vincoli obbligatori

* `FRAMES_DIR_ABS`:

  * può avere **nome arbitrario**;
  * può essere **qualsiasi path assoluto o relativo**;
  * la directory viene **creata automaticamente** se non esiste (dal server o dallo script di installazione);
  * se il path esiste ma **non è una directory** → il programma termina immediatamente.

---

## Avvio del server

### Con systemd (raccomandato)

Dopo aver eseguito `install_service.sh`:

```bash
sudo systemctl start mjpeg-server.service
```

Il servizio sarà disponibile su `http://localhost:8000`.

### Manualmente

```bash
# Attivare il virtualenv
source .venv/bin/activate

# Avviare uvicorn
uvicorn mjpeg_server:app --host 0.0.0.0 --port 8000
```

### Diagnostica errori

Se la directory dei frame non esiste o non è valida:

```
ERROR: FRAMES_DIR_ABS does not exist or is not a directory
```

In questo caso:

1. Verificare il valore di `FRAMES_DIR_ABS` in `config.env` (se si usa systemd)
2. Verificare i permessi della directory
3. Controllare i log: `sudo journalctl -u mjpeg-server.service -n 100`

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

## Licenza

Il codice è fornito **as-is**, senza garanzie implicite, ed è pensato per **uso di ricerca, sviluppo e prototipazione**.