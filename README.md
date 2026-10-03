# TeslaCharger

Sistema personale per ricaricare una Tesla nel modo più conveniente possibile, combinando un impianto fotovoltaico Solax con accumulo e la tariffa Intelligent Octopus di Octopus Energy Italia.

## Il problema

Intelligent Octopus pianifica la ricarica dell'auto nelle ore notturne e applica uno sconto sull'energia caricata in quelle finestre. Funziona bene di notte, ma di giorno ignora il fotovoltaico: se c'è sole e l'auto è in garage, per caricarla bisogna aprire l'app di Octopus e avviare a mano la carica immediata, che parte a piena potenza senza tenere conto di quanta energia stanno producendo i pannelli.

## L'obiettivo

Un servizio sempre acceso che decide da solo quando e quanto caricare:

- di giorno, quando l'auto è collegata e c'è surplus solare, avvia la carica e ne regola la potenza seguendo la produzione dei pannelli;
- protegge la batteria di casa, che ha la precedenza e non deve scaricarsi nell'auto;
- la sera lascia il controllo a Octopus, che completa la carica di notte a prezzo scontato;
- espone una webapp a uso personale, pensata per il telefono, con lo stato in tempo reale e la scelta della modalità (solo sole, sole e notte, carica subito, non intervenire).

## Come funziona

Il sistema mette in comunicazione tre servizi.

| Servizio | A cosa serve | API |
| --- | --- | --- |
| SolaxCloud | Produzione dei pannelli, scambio con la rete, stato della batteria di casa | [Solax Developer API](https://developer.solaxcloud.com/home), OAuth2 client credentials |
| Octopus Energy Italia | Stato del veicolo in Intelligent Octopus, finestre pianificate, carica immediata (boost) | [API GraphQL Kraken](https://developer.oeit-kraken.energy/) |
| Tesla | Livello di carica dell'auto, regolazione degli ampere | [Tesla Fleet API](https://developer.tesla.com/docs/fleet-api), con comandi firmati tramite `tesla-http-proxy` |

Il ciclo di controllo legge i dati dell'impianto a intervalli regolari, calcola il surplus disponibile e, se conviene, chiede a Octopus la carica immediata e regola gli ampere dell'auto. Quando il surplus finisce annulla la carica immediata, e la pianificazione notturna di Octopus resta invariata.

## Stato del progetto

- [x] lettura dei dati in tempo reale di impianto, inverter e batteria da Solax
- [x] accesso a Octopus Energy Italia, lettura del veicolo e delle finestre di carica
- [x] avvio e annullamento della carica immediata tramite Octopus, provati su un'auto reale
- [x] ciclo di controllo che avvia, regola e ferma la carica in base al surplus
- [x] accesso a Tesla: lettura dello stato di carica e regolazione degli ampere, provata su un'auto reale
- [x] webapp con accesso riservato
- [x] installazione su server, in modalità di prova
- [ ] passaggio ai comandi reali dopo un periodo di osservazione

## Le regole

Dalla webapp si sceglie tra tre modalità:

- **Carica subito**: carica alla corrente massima usando pannelli, batteria di casa e rete, senza guardare sole né orari. A carica completata torna alla modalità precedente.
- **Carica col sole**: la modalità che ottimizza, descritta qui sotto.
- **Automatica**: il sistema non interviene e l'auto carica solo di notte con Octopus. Dalla webapp si imposta il livello di carica che Octopus deve raggiungere.

La corrente massima è limitata a 12 A, un ampere sotto il limite del cavo, per restare stabili.

Con "Carica col sole", nella fascia diurna, l'auto collegata carica con una quota della produzione dei pannelli:

- all'auto va l'80% di quello che producono i pannelli: se danno 2 kW, l'auto ne riceve 1,6;
- il massimo è la corrente massima impostata;
- l'auto non accetta meno di 5 A (circa 1,1 kW): se la quota dei pannelli non ci arriva, il sistema non preleva di sua iniziativa dalla rete o dalla batteria di casa, ma lo segnala nella webapp e chiede il consenso;
- il consenso vale fino a fine giornata e si può revocare: con il consenso l'auto carica al minimo anche senza sole;
- una nuvola di passaggio non ferma la carica: senza consenso lo stop arriva dopo più letture consecutive insufficienti;
- la carica si ferma a fine giornata, a carica completata o a cavo scollegato, e la corrente torna al massimo, così la carica notturna di Octopus non resta rallentata.

Dopo la fascia diurna il sistema non carica: la batteria di casa resta alla casa, e l'auto si carica di notte con Octopus a prezzo scontato.

Una carica immediata avviata a mano dall'app di Octopus non viene mai toccata. L'auto viene interrogata solo quando serve, perché le letture hanno un costo e la tengono sveglia.

I dati di SolaxCloud si aggiornano ogni 5 minuti, quindi la regolazione procede a passi di 5 minuti e la batteria di casa assorbe le variazioni più rapide.

Le soglie hanno valori predefiniti e si possono cambiare nel file `.env`:

| Variabile | Predefinito | Significato |
| --- | --- | --- |
| `MIN_AMPS` | 5 | Corrente minima accettata dall'auto |
| `MAX_AMPS` | 12 | Corrente massima di carica |
| `DEFICIT_SAMPLES` | 2 | Letture consecutive senza sole prima dello stop, se manca il consenso |
| `PV_SHARE` | 80 | Quota della produzione dei pannelli destinata all'auto (%) |
| `MIN_SWITCH_MINUTES` | 15 | Tempo minimo tra un avvio e uno stop |
| `DAY_START`, `DAY_END` | 09:00, 19:00 | Fascia della carica diurna |
| `LIVE` | non impostato | Con `true` i comandi vengono inviati davvero all'auto |

## Uso

Serve Python 3.11 o successivo. Non ci sono dipendenze da installare.

```bash
cp .env.example .env
# compila .env con le tue credenziali

python3 -m teslacharger.tesla register      # registra il dominio presso Tesla (una tantum)
python3 -m teslacharger.tesla auth-url      # link per autorizzare l'account Tesla
python3 -m teslacharger.tesla exchange CODICE

python3 -m teslacharger.app                 # ciclo di controllo e webapp su http://127.0.0.1:8787
python3 -m unittest discover -s tests       # test delle regole
```

Senza `LIVE=true` il sistema resta in modalità di prova: mostra cosa farebbe, senza inviare comandi.

I comandi all'auto vanno firmati: serve `tesla-http-proxy` del progetto [vehicle-command](https://github.com/teslamotors/vehicle-command) in ascolto in locale, con la chiave privata dell'applicazione, e la chiave va abbinata all'auto dall'app Tesla.

## Installazione su server

La cartella `deploy/` contiene i due servizi systemd (ciclo di controllo e firma dei comandi) e il modello di configurazione nginx. La webapp ascolta solo in locale: nginx la espone in HTTPS e la protegge con una password, lasciando pubblica solo la chiave che Tesla deve poter leggere.

Nella cartella `scripts/` restano gli script usati per esplorare le API (richiedono Node.js 20.6 o successivo).

## Credenziali

Le credenziali stanno solo nel file `.env`, escluso dal repository. Per Solax va creata un'applicazione nel portale sviluppatori, autorizzando i servizi di lettura (Data Monitoring e Information Access). Per Octopus si usano email e password dell'app.

## Avvertenze

Progetto personale, non affiliato a Tesla, Octopus Energy o Solax. Octopus sconsiglia di affidare a sistemi di terze parti il controllo della ricarica di un veicolo iscritto a Intelligent Octopus: chi riusa questo codice lo fa a proprio rischio e dovrebbe verificare le condizioni del proprio contratto.
