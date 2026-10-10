# TeslaCharger

Sistema personale per ricaricare una Tesla nel modo più conveniente possibile, combinando un impianto fotovoltaico Solax con accumulo e la tariffa Intelligent Octopus di Octopus Energy Italia. Con il tempo è diventato anche il cruscotto della casa: impianto, pompa di calore Emmeti, bollette e previsione del sole.

## Il problema

Intelligent Octopus pianifica la ricarica dell'auto nelle ore notturne e applica uno sconto sull'energia caricata in quelle finestre. Funziona bene di notte, ma di giorno ignora il fotovoltaico: se c'è sole e l'auto è in garage, per caricarla bisogna aprire l'app di Octopus e avviare a mano la carica immediata, che parte a piena potenza senza tenere conto di quanta energia stanno producendo i pannelli. E di notte carica sempre allo stesso livello, anche quando il giorno dopo l'auto resterà a casa col sole o non ha bisogno di energia.

## Cosa fa

Un servizio sempre acceso che decide quando e quanto caricare:

- di giorno, quando l'auto è collegata, la carica con una quota della produzione dei pannelli e ne regola la potenza seguendo il sole;
- non preleva di sua iniziativa da rete o batteria di casa: se il sole non basta chiede il consenso;
- la sera chiede come comportarsi per la notte: nessuna carica, carica ridotta perché domani l'auto resta a casa, oppure carica piena con Octopus a prezzo scontato;
- durante la carica notturna può tenere a riposo la batteria di casa, così l'auto prende dalla rete scontata;
- espone una webapp per il telefono con lo stato in tempo reale, le scelte, lo storico e le notifiche push.

## Come funziona

Il sistema mette in comunicazione questi servizi.

| Servizio | A cosa serve | API |
| --- | --- | --- |
| SolaxCloud | Produzione dei pannelli, scambio con la rete, stato della batteria di casa, comando di riposo della batteria | [Solax Developer API](https://developer.solaxcloud.com/home), OAuth2 client credentials |
| Octopus Energy Italia | Stato del veicolo in Intelligent Octopus, livello e ora della carica notturna, finestre pianificate, carica immediata (boost), bollette | [API GraphQL Kraken](https://developer.oeit-kraken.energy/) |
| Tesla | Livello di carica dell'auto, regolazione degli ampere, storico delle ricariche | [Tesla Fleet API](https://developer.tesla.com/docs/fleet-api), con comandi firmati tramite `tesla-http-proxy` |
| Emmeti AQ-IoT | Pompa di calore: stanze, acqua calda, consumi separati per pompa di calore, acqua calda e resto della casa | Portale del produttore, senza API documentata: si usano in sola lettura le chiamate della sua app web |
| Open-Meteo | Previsione del meteo e della radiazione solare, tarata sulla produzione reale dell'impianto | [API pubblica](https://open-meteo.com/), senza chiave |

Il ciclo di controllo legge i dati dell'impianto ogni due minuti e mezzo, decide cosa fare e, se serve, chiede a Octopus la carica immediata e regola gli ampere dell'auto. Le regole stanno in `teslacharger/policy.py`, funzioni pure coperte dai test; `teslacharger/controller.py` le applica, parla con i servizi e tiene lo stato.

## Le regole

### Modalità

Dalla webapp si sceglie tra tre modalità:

- **Carica subito**: carica alla corrente massima usando pannelli, batteria di casa e rete, senza guardare sole né orari. A carica completata torna alla modalità precedente.
- **Carica col sole**: la modalità che ottimizza, descritta qui sotto.
- **Automatica**: il sistema non interviene durante il giorno e l'auto carica di notte con Octopus. Al mattino passa da sola a "Carica col sole", e la sera "Carica col sole" torna in automatica.

### Carica col sole

Nella fascia diurna, con l'auto collegata:

- all'auto va l'80% di quello che producono i pannelli: se danno 2 kW, l'auto ne riceve 1,6;
- il massimo è la corrente massima impostata (12 A, un ampere sotto il limite del cavo);
- l'auto non accetta meno di 5 A (circa 1,1 kW): se la quota dei pannelli non ci arriva, il sistema non preleva da rete o batteria di casa ma mostra nell'app la richiesta di consenso. Non arriva come notifica: con l'auto collegata la carica parte da sola appena il sole basta;
- quando il sole arriva a bastare per l'auto, tre casi: con l'auto collegata e «Carica col sole» attiva parte la carica e arriva la notifica di avvio; con l'auto collegata ma «Nessuna carica» scelta la sera prima arriva l'avviso che c'è sole e si decide dall'app; con l'auto scollegata arriva l'invito a collegarla, solo nelle ore in cui di solito è a casa (`HOME_PLAN`). Un avviso al giorno;
- il consenso vale fino a fine giornata e si può revocare: con il consenso l'auto carica al minimo anche senza sole;
- una nuvola di passaggio non ferma la carica: senza consenso lo stop arriva dopo più letture consecutive insufficienti;
- appena collegata l'auto, il sistema aspetta qualche minuto prima di avviare la carica, perché nei primi istanti Octopus prende in carico l'auto e annulla una carica immediata appena richiesta; se Octopus non conferma l'avvio, il sistema riprova al ciclo successivo;
- la carica si ferma a fine giornata, a carica completata o a cavo scollegato, e la corrente torna al massimo, così la carica notturna di Octopus non resta rallentata.

### La domanda della sera

Quando l'auto è collegata dopo le 18, il sistema chiede con una notifica come comportarsi per la notte:

- **Nessuna carica**: il livello chiesto a Octopus scende al 10%, sotto quello dell'auto, e di notte non parte nulla; il giorno dopo nemmeno la carica col sole parte da sola, quando il sole basta arriva un avviso;
- **Domani a casa**: carica fino al livello del piano "Domani a casa" (50% se non modificato), lasciando spazio al sole del giorno dopo;
- **Automatico**: carica fino al livello del piano "Automatico".

I due piani hanno ciascuno il proprio livello e la propria ora di fine carica, che si regolano dalla webapp. Il sistema suggerisce una delle due risposte guardando un piano settimanale delle ore in cui l'auto è di solito a casa (`HOME_PLAN`) e la previsione del sole. Finché non c'è risposta la carica resta sospesa; alle 22, senza risposta, viene applicato il suggerimento. La domanda resta aperta fino all'inizio della fascia diurna successiva, e quando l'auto riparte dopo una notte a livello ridotto torna in vigore il piano "Automatico".

### La carica notturna

Di notte la carica è di Octopus, nelle finestre a prezzo scontato. Durante quelle finestre il sistema può tenere a riposo la batteria di casa con la modalità remota "solo carica" dell'inverter Solax: il blocco dura quanto la finestra e l'inverter torna da solo al funzionamento normale. Si attiva e disattiva dalla webapp.

Octopus fissa il piano della notte poco dopo il collegamento, con il livello di quel momento, e se il livello scende dopo non sempre lo ricalcola. Quando è stato scelto "Nessuna carica" ma Octopus ha comunque una carica in programma, il sistema ripete il comando e avvisa subito con una notifica: in quel caso la carica si ferma con certezza solo dall'app Tesla o da quella di Octopus.

Quando l'auto è già sopra il livello chiesto, Octopus non la tiene sotto controllo e la Tesla carica per conto suo fino al proprio limite, a prezzo pieno e spesso dalla batteria di casa. Octopus non lo segnala: il sistema se ne accorge leggendo l'auto qualche minuto dopo il collegamento e ogni volta che il consumo di casa supera 1,5 kW (se resta alto, al massimo una volta l'ora, per lasciarla dormire). Se l'auto carica fuori da una finestra di Octopus e oltre il livello di stanotte, il sistema le manda il comando di stop e avvisa quando l'auto lo conferma. Dopo due stop nello stesso collegamento la lascia fare, perché potrebbe essere stato l'utente a riavviarla; per caricare comunque si sceglie "Carica subito".

### Altro

Una carica immediata avviata a mano dall'app di Octopus non viene mai toccata. L'auto viene interrogata solo quando serve, perché le letture hanno un costo e la tengono sveglia. Le notifiche di avvio e stop partono solo quando Octopus conferma che la manovra è avvenuta.

I dati di SolaxCloud si aggiornano ogni 5 minuti, quindi la regolazione procede a passi di 5 minuti e la batteria di casa assorbe le variazioni più rapide.

## Configurazione

Credenziali e dati personali stanno solo nel file `.env`, escluso dal repository: si parte da `.env.example`. Per Solax va creata un'applicazione nel portale sviluppatori, autorizzando i servizi di lettura (Data Monitoring e Information Access); il riposo della batteria richiede anche il permesso di controllo dell'inverter. Per Octopus ed Emmeti si usano le credenziali delle rispettive app.

Le soglie hanno valori predefiniti e si possono cambiare nello stesso file:

| Variabile | Predefinito | Significato |
| --- | --- | --- |
| `MIN_AMPS` | 5 | Corrente minima accettata dall'auto |
| `MAX_AMPS` | 12 | Corrente massima di carica |
| `PV_SHARE` | 80 | Quota della produzione dei pannelli destinata all'auto (%) |
| `DEFICIT_SAMPLES` | 2 | Letture consecutive senza sole prima dello stop, se manca il consenso |
| `MIN_SWITCH_MINUTES` | 15 | Tempo minimo tra un avvio e uno stop |
| `DAY_START`, `DAY_END` | 08:30, 19:00 | Fascia della carica diurna |
| `POLL_SECONDS` | 150 | Intervallo tra due cicli |
| `CAR_RETRY_MINUTES` | 30 | Attesa prima di risvegliare di nuovo l'auto o ricontrollare il cavo |
| `EVENING_ASK_HOUR` | 18 | Ora da cui parte la domanda della sera |
| `EVENING_DEFAULT_HOUR` | 22 | Ora in cui, senza risposta, si applica il suggerimento |
| `HOME_DAY_TARGET` | 50 | Livello di partenza del piano "Domani a casa" |
| `HOME_DAY_MIN_KWH` | 5 | Energia solare prevista per l'auto oltre la quale si suggerisce "Domani a casa" |
| `HOME_PLAN` | vuoto | Ore in cui di solito l'auto è a casa, per giorno (lunedì = 0), per esempio `0:9-18;2:14-19` |
| `ROOM_NAMES` | vuoto | Nomi delle stanze per indirizzo del termostato, per esempio `11:Salotto,12:Camera` |
| `PRICE_KWH` | 0.229 | Costo in euro di un kWh in più preso dalla rete, tasse comprese |
| `FIXED_MONTHLY` | 30.47 | Quote fisse mensili della bolletta in euro, IVA compresa |
| `NIGHT_DISCOUNT_KWH` | 0.036 | Sconto in euro per kWh nelle ricariche notturne di Octopus |
| `LIVE` | non impostato | Con `true` i comandi vengono inviati davvero all'auto, a Octopus e all'inverter |

## Uso

Serve Python 3.11 o successivo. L'unica dipendenza è `cryptography`, usata per le notifiche push: senza, il sistema funziona ma le notifiche restano disattivate.

```bash
cp .env.example .env
# compila .env con le tue credenziali
pip install -r requirements.txt

python3 -m teslacharger.tesla register      # registra il dominio presso Tesla (una tantum)
python3 -m teslacharger.tesla auth-url      # link per autorizzare l'account Tesla
python3 -m teslacharger.tesla exchange CODICE
python3 -m teslacharger.tesla charge-status # livello e stato della carica (charge-stop e charge-start la comandano)

python3 -m teslacharger.app                 # ciclo di controllo e webapp su http://127.0.0.1:8787
python3 -m unittest discover -s tests       # test delle regole
```

Senza `LIVE=true` il sistema resta in modalità di prova: mostra cosa farebbe, senza inviare comandi.

I comandi all'auto vanno firmati: serve `tesla-http-proxy` del progetto [vehicle-command](https://github.com/teslamotors/vehicle-command) in ascolto in locale, con la chiave privata dell'applicazione, e la chiave va abbinata all'auto dall'app Tesla.

Stato e storico stanno nella cartella indicata da `TESLACHARGER_DATA` (predefinita `data/`): `state.json` con le scelte correnti e `history.db`, un database SQLite con impianto, cariche, collegamenti, risposte alla domanda della sera ed eventi.

## Installazione su server

La cartella `deploy/` contiene i due servizi systemd (ciclo di controllo e firma dei comandi) e il modello di configurazione nginx. La webapp ascolta solo in locale e nginx la espone in HTTPS. L'accesso richiede la password impostata in `WEB_PASSWORD`, con una sessione che resta valida sul dispositivo.

## La webapp

Si installa sulla schermata Home del telefono. La veste grafica è quella del progetto in `design/Main.dc.html` (Material 3 Expressive, materiali Tesla): barra con il titolo della sezione e un indice delle sue parti, barra di navigazione in basso, intestazioni di gruppo con un colore per tipo (stato, controlli, archivio) e colori funzionali per sole, batteria di casa, Octopus e clima. Ha quattro sezioni, ciascuna divisa in tre parti:

- **Ricarica**: stato, domanda della sera, consenso, previsione del sole, modalità, carica notturna con i due piani, notifiche e ultime manovre;
- **Casa**: impianto fotovoltaico e batteria in tempo reale, energia del giorno, storico di 14 giorni, bollette Octopus con stima dei mesi non ancora fatturati;
- **Clima**: pompa di calore, stanze, acqua calda e consumi mensili, in sola lettura;
- **Auto**: dati dell'auto, indicatori della batteria, ricariche divise tra sole, giorno e notte, ricariche ai Supercharger, consumi di guida.

Su iPhone le notifiche richiedono che l'app sia aggiunta alla schermata Home. Una descrizione completa, pensata per chi ne cura esperienza d'uso e interfaccia, è in [docs/descrizione-app.md](docs/descrizione-app.md).

L'interfaccia segue un progetto grafico in stile Material 3 Expressive, con i materiali della Tesla (argento, grafite, nero opaco e lucido). I file del progetto sono in `design/`: struttura e logica delle pagine sono in `design/page.body.html` e `design/login.body.html`, e `python3 design/build_page.py` rigenera `teslacharger/page.html` e la pagina di accesso prendendo dal progetto colori, forme e animazioni. Le pagine generate non vanno modificate a mano.

## Analisi

La cartella `analysis/` contiene due script usati per valutare l'impianto, non necessari al funzionamento:

- `python3 -m analysis.fetch_history INIZIO FINE` scarica da Solax lo storico a 5 minuti in `data/solax_history.db`;
- `python3 -m analysis.battery_sizing` rifà i conti dell'anno con batterie di casa di capacità diversa.

Nella cartella `scripts/` restano gli script usati per esplorare le API (richiedono Node.js 20.6 o successivo).

## Limiti noti

- Octopus non ricalcola sempre il piano della notte quando il livello scende dopo il collegamento: con "Nessuna carica" il sistema avvisa, ma la carica va fermata a mano.
- Una carica partita dall'auto da sola viene vista solo leggendo l'auto, quindi lo stop può arrivare con qualche minuto di ritardo.
- Il portale Emmeti non ha un'API ufficiale: se cambia la sua app web, la sezione "Clima" può smettere di funzionare.
- Solax tiene valido un solo token per applicazione: uno script lanciato in parallelo con le stesse credenziali annulla quello del servizio, che si rinnova da solo al ciclo successivo.

## Avvertenze

Progetto personale, non affiliato a Tesla, Octopus Energy, Solax o Emmeti. Octopus sconsiglia di affidare a sistemi di terze parti il controllo della ricarica di un veicolo iscritto a Intelligent Octopus: chi riusa questo codice lo fa a proprio rischio e dovrebbe verificare le condizioni del proprio contratto.
