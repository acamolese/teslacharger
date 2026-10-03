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
- [x] prima versione del ciclo di controllo: accende e spegne la carica immediata in base al surplus
- [x] accesso a Tesla: lettura dello stato di carica e regolazione degli ampere, provata su un'auto reale
- [ ] regolazione degli ampere dentro il ciclo di controllo
- [ ] webapp con accesso riservato
- [ ] installazione su server

La prima versione non regola la potenza: l'auto carica alla potenza impostata (circa 2,8 kW nel caso di partenza) e il sistema decide solo quando avviare e quando fermare.

## Le regole

La carica solare parte quando, nella fascia diurna, la batteria di casa è sopra la soglia di avvio e il surplus copre la potenza dell'auto, al netto di un piccolo aiuto concesso alla batteria di casa. Si ferma quando il deficit dura per più letture consecutive, quando la batteria di casa scende sotto la soglia di stop o a fine giornata. Tra due manovre passa sempre un tempo minimo.

Una carica immediata avviata a mano dall'app di Octopus non viene mai toccata.

I dati di SolaxCloud si aggiornano ogni 5 minuti, quindi la regolazione procede a passi di 5 minuti e la batteria di casa assorbe le variazioni più rapide.

Le soglie hanno valori predefiniti e si possono cambiare nel file `.env`:

| Variabile | Predefinito | Significato |
| --- | --- | --- |
| `CAR_POWER_W` | 2800 | Potenza assorbita dall'auto in carica |
| `BATTERY_ASSIST_W` | 500 | Potenza che la batteria di casa può cedere alla carica |
| `SOC_START` | 80 | Carica minima della batteria di casa per avviare (%) |
| `SOC_STOP` | 50 | Carica della batteria di casa sotto cui si ferma (%) |
| `DEFICIT_SAMPLES` | 2 | Letture consecutive in deficit prima dello stop |
| `MIN_SWITCH_MINUTES` | 15 | Tempo minimo tra due manovre |
| `DAY_START`, `DAY_END` | 09:00, 18:00 | Fascia in cui la carica solare è consentita |

## Uso

Serve Python 3.11 o successivo. Non ci sono dipendenze da installare.

```bash
cp .env.example .env
# compila .env con le tue credenziali

python3 -m teslacharger.controller --once   # una valutazione, senza agire
python3 -m teslacharger.controller          # ciclo continuo, senza agire
python3 -m teslacharger.controller --live   # ciclo continuo che comanda davvero l'auto

python3 -m unittest discover -s tests       # test delle regole
```

Senza `--live` il sistema scrive solo cosa farebbe, senza inviare comandi.

Nella cartella `scripts/` ci sono gli script usati per esplorare le API (richiedono Node.js 20.6 o successivo): `probe-solax.mjs` e `probe-octopus.mjs` sono in sola lettura, `octopus-boost.mjs` avvia e annulla la carica immediata.

## Credenziali

Le credenziali stanno solo nel file `.env`, escluso dal repository. Per Solax va creata un'applicazione nel portale sviluppatori, autorizzando i servizi di lettura (Data Monitoring e Information Access). Per Octopus si usano email e password dell'app.

## Avvertenze

Progetto personale, non affiliato a Tesla, Octopus Energy o Solax. Octopus sconsiglia di affidare a sistemi di terze parti il controllo della ricarica di un veicolo iscritto a Intelligent Octopus: chi riusa questo codice lo fa a proprio rischio e dovrebbe verificare le condizioni del proprio contratto.
