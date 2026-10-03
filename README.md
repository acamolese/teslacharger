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
| Tesla | Livello di carica dell'auto, regolazione degli ampere | Tesla Fleet API (da integrare) |

Il ciclo di controllo legge i dati dell'impianto a intervalli regolari, calcola il surplus disponibile e, se conviene, chiede a Octopus la carica immediata e regola gli ampere dell'auto. Quando il surplus finisce annulla la carica immediata, e la pianificazione notturna di Octopus resta invariata.

## Stato del progetto

Il progetto è all'inizio. Al momento contiene gli script di verifica degli accessi, entrambi in sola lettura:

- [x] lettura dei dati in tempo reale di impianto, inverter e batteria da Solax
- [x] accesso a Octopus Energy Italia, lettura del veicolo e delle finestre di carica
- [ ] avvio e annullamento della carica immediata tramite Octopus
- [ ] integrazione con Tesla per la regolazione degli ampere
- [ ] ciclo di controllo con le regole su surplus e batteria di casa
- [ ] webapp con accesso riservato
- [ ] installazione su server

## Uso

Serve Node.js 20.6 o successivo. Non ci sono dipendenze da installare.

```bash
cp .env.example .env
# compila .env con le tue credenziali

node --env-file=.env scripts/probe-solax.mjs
node --env-file=.env scripts/probe-octopus.mjs
```

`probe-solax.mjs` elenca impianti e dispositivi dell'account Solax e ne stampa i dati in tempo reale. `probe-octopus.mjs` stampa i dispositivi registrati in Intelligent Octopus, il loro stato e le finestre di carica pianificate.

## Credenziali

Le credenziali stanno solo nel file `.env`, escluso dal repository. Per Solax va creata un'applicazione nel portale sviluppatori, autorizzando i servizi di lettura (Data Monitoring e Information Access). Per Octopus si usano email e password dell'app.

## Avvertenze

Progetto personale, non affiliato a Tesla, Octopus Energy o Solax. Octopus sconsiglia di affidare a sistemi di terze parti il controllo della ricarica di un veicolo iscritto a Intelligent Octopus: chi riusa questo codice lo fa a proprio rischio e dovrebbe verificare le condizioni del proprio contratto.
