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

In modalità automatica, nella fascia diurna, l'auto collegata carica sempre, e più c'è sole più carica:

- la carica parte appena l'auto è collegata e non è già al limite impostato;
- la corrente non scende mai sotto la base (5 A, circa 1,1 kW), anche se il sole non basta: in quel caso la differenza arriva dalla batteria di casa o dalla rete;
- sopra la base, gli ampere seguono il surplus dei pannelli fino al massimo consentito dal cavo;
- finché la batteria di casa è sotto la soglia di precedenza, il surplus va a lei e l'auto resta alla base;
- la carica si ferma a fine giornata, a carica completata o a cavo scollegato, e la corrente torna al massimo, così la carica notturna di Octopus non resta rallentata.

Una carica immediata avviata a mano dall'app di Octopus non viene mai toccata. L'auto viene interrogata solo quando serve, perché le letture hanno un costo e la tengono sveglia.

I dati di SolaxCloud si aggiornano ogni 5 minuti, quindi la regolazione procede a passi di 5 minuti e la batteria di casa assorbe le variazioni più rapide.

Dalla webapp si sceglie la modalità: automatica, carica subito (massima potenza, anche dalla rete) o pausa.

Le soglie hanno valori predefiniti e si possono cambiare nel file `.env`:

| Variabile | Predefinito | Significato |
| --- | --- | --- |
| `MIN_AMPS` | 5 | Corrente di base, garantita di giorno |
| `BATTERY_ASSIST_W` | 300 | Potenza che la batteria di casa può cedere alla carica oltre la base |
| `SOC_START` | 80 | Carica della batteria di casa sotto cui l'auto resta alla base (%) |
| `MIN_SWITCH_MINUTES` | 15 | Tempo minimo tra un avvio e uno stop |
| `DAY_START`, `DAY_END` | 09:00, 18:00 | Fascia della carica diurna |
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
