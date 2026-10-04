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
| Emmeti AQ-IoT | Pompa di calore: stanze, acqua calda, consumi separati per pompa di calore, acqua calda e resto della casa | Portale del produttore, senza API documentata: si usano in sola lettura le chiamate della sua app web |

Il ciclo di controllo legge i dati dell'impianto a intervalli regolari, calcola il surplus disponibile e, se conviene, chiede a Octopus la carica immediata e regola gli ampere dell'auto. Quando il surplus finisce annulla la carica immediata, e la pianificazione notturna di Octopus resta invariata.

## Stato del progetto

- [x] lettura dei dati in tempo reale di impianto, inverter e batteria da Solax
- [x] accesso a Octopus Energy Italia, lettura del veicolo e delle finestre di carica
- [x] avvio e annullamento della carica immediata tramite Octopus, provati su un'auto reale
- [x] ciclo di controllo che avvia, regola e ferma la carica in base al surplus
- [x] accesso a Tesla: lettura dello stato di carica e regolazione degli ampere, provata su un'auto reale
- [x] webapp con accesso riservato
- [x] installazione su server
- [x] comandi reali attivi
- [x] notifiche push e pannello dell'auto con storico delle ricariche
- [x] costi e risparmi stimati nello storico

## Le regole

Dalla webapp si sceglie tra tre modalità:

- **Carica subito**: carica alla corrente massima usando pannelli, batteria di casa e rete, senza guardare sole né orari. A carica completata torna alla modalità precedente.
- **Carica col sole**: la modalità che ottimizza, descritta qui sotto.
- **Automatica**: il sistema non interviene e l'auto carica di notte con Octopus. Al mattino passa da sola a "Carica col sole", e la sera "Carica col sole" torna in automatica.

Dalla webapp si impostano anche il livello di carica che Octopus deve raggiungere di notte e l'ora entro cui l'auto deve essere pronta.

La corrente massima è limitata a 12 A, un ampere sotto il limite del cavo, per restare stabili.

Con "Carica col sole", nella fascia diurna, l'auto collegata carica con una quota della produzione dei pannelli:

- all'auto va l'80% di quello che producono i pannelli: se danno 2 kW, l'auto ne riceve 1,6;
- il massimo è la corrente massima impostata;
- l'auto non accetta meno di 5 A (circa 1,1 kW): se la quota dei pannelli non ci arriva, il sistema non preleva di sua iniziativa dalla rete o dalla batteria di casa, ma lo segnala nella webapp e chiede il consenso;
- il consenso vale fino a fine giornata e si può revocare: con il consenso l'auto carica al minimo anche senza sole;
- una nuvola di passaggio non ferma la carica: senza consenso lo stop arriva dopo più letture consecutive insufficienti;
- la carica si ferma a fine giornata, a carica completata o a cavo scollegato, e la corrente torna al massimo, così la carica notturna di Octopus non resta rallentata.

Dopo la fascia diurna il sistema non carica: la batteria di casa resta alla casa, e l'auto si carica di notte con Octopus a prezzo scontato.

Durante le finestre di carica notturna di Octopus il sistema può tenere a riposo la batteria di casa, con la modalità remota "solo carica" dell'inverter Solax: così l'auto non la svuota e prende dalla rete a prezzo scontato. Il blocco dura quanto la finestra e l'inverter torna da solo al funzionamento normale. Si attiva e disattiva dalla webapp.

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
| `DAY_START`, `DAY_END` | 08:30, 19:00 | Fascia della carica diurna |
| `PRICE_KWH` | 0.229 | Costo in euro di un kWh in più preso dalla rete, tasse comprese |
| `FIXED_MONTHLY` | 30.47 | Quote fisse mensili della bolletta in euro, IVA compresa |
| `NIGHT_DISCOUNT_KWH` | 0.036 | Sconto in euro per kWh nelle ricariche notturne di Octopus |
| `LIVE` | non impostato | Con `true` i comandi vengono inviati davvero all'auto |

## Uso

Serve Python 3.11 o successivo. L'unica dipendenza è `cryptography`, usata per le notifiche push: senza, il sistema funziona ma le notifiche restano disattivate.

```bash
cp .env.example .env
# compila .env con le tue credenziali
pip install -r requirements.txt

python3 -m teslacharger.tesla register      # registra il dominio presso Tesla (una tantum)
python3 -m teslacharger.tesla auth-url      # link per autorizzare l'account Tesla
python3 -m teslacharger.tesla exchange CODICE

python3 -m teslacharger.app                 # ciclo di controllo e webapp su http://127.0.0.1:8787
python3 -m unittest discover -s tests       # test delle regole
```

Senza `LIVE=true` il sistema resta in modalità di prova: mostra cosa farebbe, senza inviare comandi.

I comandi all'auto vanno firmati: serve `tesla-http-proxy` del progetto [vehicle-command](https://github.com/teslamotors/vehicle-command) in ascolto in locale, con la chiave privata dell'applicazione, e la chiave va abbinata all'auto dall'app Tesla.

## Installazione su server

La cartella `deploy/` contiene i due servizi systemd (ciclo di controllo e firma dei comandi) e il modello di configurazione nginx. La webapp ascolta solo in locale e nginx la espone in HTTPS. L'accesso richiede la password impostata in `WEB_PASSWORD`, con una sessione che resta valida sul dispositivo.

## La webapp

Si installa sulla schermata Home del telefono e ha due sezioni: "Ricarica", con stato, modalità, consenso e carica notturna, e "Auto", con i dati dell'auto, lo storico delle ricariche diviso tra sole, giorno e notte, e una stima dei consumi di guida. Invia notifiche push all'avvio e allo stop della carica, quando serve il consenso e in caso di problemi. Su iPhone le notifiche richiedono che l'app sia aggiunta alla schermata Home.

Una descrizione completa, pensata per chi ne cura esperienza d'uso e interfaccia, è in [docs/descrizione-app.md](docs/descrizione-app.md).

L'interfaccia segue un progetto grafico in stile Material 3 Expressive, con i materiali della Tesla (argento, grafite, nero opaco e lucido). I file del progetto sono in `design/`: `python3 design/build_page.py` ricompone le pagine prendendo da lì colori, forme e animazioni.

Nella cartella `scripts/` restano gli script usati per esplorare le API (richiedono Node.js 20.6 o successivo).

## Credenziali

Le credenziali stanno solo nel file `.env`, escluso dal repository. Per Solax va creata un'applicazione nel portale sviluppatori, autorizzando i servizi di lettura (Data Monitoring e Information Access). Per Octopus si usano email e password dell'app.

## Avvertenze

Progetto personale, non affiliato a Tesla, Octopus Energy o Solax. Octopus sconsiglia di affidare a sistemi di terze parti il controllo della ricarica di un veicolo iscritto a Intelligent Octopus: chi riusa questo codice lo fa a proprio rischio e dovrebbe verificare le condizioni del proprio contratto.
