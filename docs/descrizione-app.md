# TeslaCharger: descrizione dell'applicazione

Questo documento descrive cosa fa l'applicazione, chi la usa, quali informazioni mostra e quali azioni permette. È pensato per chi deve progettarne l'esperienza d'uso e l'interfaccia. Descrive lo stato attuale, senza indicazioni su come ridisegnarla.

## In breve

TeslaCharger è una webapp personale che gestisce la ricarica domestica di una Tesla Model 3. Ha un solo utente, il proprietario dell'auto, che la usa quasi sempre dal telefono, installata sulla schermata Home dell'iPhone come un'app.

L'applicazione ha due parti. La prima è un servizio sempre acceso su un server, che ogni due minuti e mezzo legge lo stato della casa e dell'auto e decide da solo se caricare e con quanta potenza. La seconda è la webapp, cioè la finestra con cui l'utente vede cosa sta succedendo, sceglie come deve comportarsi il sistema e riceve le notifiche.

L'utente non deve seguire il sistema passo passo: nella maggior parte dei giorni non apre l'app. La apre per controllare, per cambiare modalità, per rispondere a una richiesta di consenso o perché ha ricevuto una notifica.

## Il contesto

La casa ha un impianto fotovoltaico con una batteria di accumulo. Il fornitore di energia è Octopus Energy, con un'offerta che pianifica la ricarica dell'auto di notte e applica uno sconto del 30% sull'energia caricata in quelle ore. L'auto si ricarica da una presa di casa con il caricatore mobile, quindi a potenza contenuta.

Le fonti di energia per l'auto sono quindi quattro, con convenienza diversa:

| Fonte | Quando | Convenienza |
| --- | --- | --- |
| Pannelli solari | Di giorno, se c'è sole | Gratuita |
| Rete, di notte con Octopus | Nelle ore pianificate da Octopus | Scontata del 30% |
| Batteria di casa | Sempre, finché è carica | È energia che servirebbe alla casa la sera |
| Rete, di giorno | Sempre | Prezzo pieno |

Lo scopo dell'applicazione è usare il più possibile le prime due e ricorrere alle altre solo quando l'utente lo decide.

## I tre sistemi collegati

L'applicazione non produce dati propri: li legge da tre servizi e invia comandi a due di essi.

- **Impianto fotovoltaico (Solax)**: fornisce la potenza prodotta dai pannelli, lo stato della batteria di casa e lo scambio con la rete. I dati si aggiornano ogni 5 minuti, quindi quello che l'utente vede può essere vecchio di qualche minuto.
- **Octopus Energy**: dice se l'auto è collegata alla presa, se sta caricando e quali ore di carica notturna ha pianificato. Riceve i comandi di avvio e stop della carica immediata e le preferenze sulla carica notturna.
- **Tesla**: fornisce i dati dell'auto (batteria, autonomia, chilometri e altri) e riceve il comando che regola la corrente di carica. L'auto, quando è ferma da un po', va in standby e non risponde: in quel caso i dati mostrati sono quelli dell'ultima lettura, con il loro orario.

## Le modalità

Il comportamento del sistema dipende dalla modalità selezionata. Le modalità sono tre, se ne può avere attiva una sola alla volta e l'utente le sceglie con tre pulsanti.

### Carica subito

Carica l'auto immediatamente alla corrente massima, usando tutto quello che c'è: pannelli, batteria di casa e rete. Non tiene conto del sole né dell'orario. È la modalità per quando serve l'auto carica al più presto. A carica completata il sistema torna da solo alla modalità che era attiva prima.

### Carica col sole

È la modalità che ottimizza. Vale nella fascia diurna, dalle 8:30 alle 19:00, quando l'auto è collegata.

All'auto viene destinato l'80% della potenza prodotta in quel momento dai pannelli: se i pannelli producono 2 kW, l'auto ne riceve 1,6. La potenza viene ricalcolata a ogni nuovo dato dei pannelli, quindi la carica sale e scende seguendo il sole.

Ci sono due limiti. L'auto non accetta meno di circa 1,1 kW, e non riceve mai più di circa 2,8 kW (12 ampere). Se la quota dei pannelli non arriva al minimo, il sistema non preleva di sua iniziativa dalla rete o dalla batteria di casa: lo dichiara all'utente e chiede il suo consenso (vedi più avanti). Una nuvola di passaggio non ferma la carica: lo stop arriva solo se il sole resta insufficiente per una decina di minuti.

Alle 19:00 il sistema ferma la carica che ha avviato e torna alla modalità automatica.

### Automatica

Il sistema non interviene sulla carica. L'auto si ricarica di notte, nelle ore scelte da Octopus, a prezzo scontato. È la modalità della notte.

Al mattino, alle 8:30, la modalità automatica passa da sola a "Carica col sole". Se durante il giorno l'utente sceglie esplicitamente "Automatica", la scelta resta valida fino a sera e il sistema quel giorno non carica col sole.

### Il ciclo di una giornata tipo

Senza interventi dell'utente la giornata si svolge così: di notte l'auto carica con Octopus fino al livello desiderato. Alle 8:30 il sistema passa a "Carica col sole" e, se l'auto è collegata e c'è sole, carica seguendo i pannelli. Alle 19:00 torna in "Automatica". Se l'auto non è a casa durante il giorno non succede nulla.

## La richiesta di consenso

Quando è attiva "Carica col sole", l'auto è collegata e il sole non basta a raggiungere la potenza minima, il sistema non carica. Mostra invece una richiesta: l'utente può autorizzare a caricare lo stesso alla potenza minima, prendendo l'energia dalla batteria di casa o dalla rete.

Il consenso vale fino alle 19:00 dello stesso giorno e può essere revocato in qualsiasi momento. Con il consenso attivo, l'auto carica al minimo quando il sole non basta e segue i pannelli quando il sole c'è. La richiesta arriva anche come notifica, una sola volta al giorno.

## La carica notturna con Octopus

La carica notturna è sempre attiva, qualunque sia la modalità. L'utente imposta due preferenze, che vengono trasmesse a Octopus:

- **il livello da raggiungere**, in percentuale di batteria, da 50% a 100% a passi di 5;
- **l'ora entro cui l'auto deve essere pronta**, tra le 4:00 e le 11:00 a passi di mezz'ora. L'intervallo è imposto da Octopus.

Octopus sceglie in autonomia in quali ore della notte caricare per rispettare livello e orario. Le preferenze valgono per tutti i giorni della settimana.

## Cosa mostra la webapp

La webapp ha una schermata di accesso e tre sezioni, "Ricarica", "Casa" e "Auto", tra cui si passa con tre pulsanti in alto.

### Accesso

Alla prima apertura viene chiesta una password. Una volta entrati, il dispositivo resta collegato per un anno, senza dover reinserire la password. Nella sezione "Auto" c'è il comando per uscire.

### Sezione "Ricarica"

È la sezione principale. Dall'alto verso il basso contiene:

1. **Stato attuale**: una frase che riassume cosa sta succedendo ("Auto in carica", "Nessuna carica in corso", "Avvio della carica", "Carica fermata", "Regolazione della potenza", "Risveglio dell'auto") e sotto la motivazione, per esempio "pannelli a 3200 W, all'auto fino all'80%", "auto non collegata", "carica notturna affidata a Octopus". In alto compare l'ora dell'ultimo aggiornamento.
2. **Richiesta di consenso**: compare solo quando serve, con il pulsante per autorizzare rete e batteria per la giornata. Quando il consenso è attivo, al suo posto compare l'indicazione che è valido fino alle 19:00, con il pulsante per revocarlo.
3. **Quattro dati della casa e dell'auto**:
   - pannelli: potenza prodotta e quota destinabile all'auto;
   - batteria di casa: percentuale di carica e se si sta caricando, scaricando o è ferma;
   - consumi di casa: potenza assorbita in quel momento e scambio con la rete;
   - auto: percentuale di batteria e stato del cavo, con l'ora della lettura.
4. **Modalità**: i tre pulsanti, con quello attivo evidenziato e una riga che spiega cosa fa la modalità selezionata.
5. **Carica notturna con Octopus**: i due selettori per livello e orario, con una riga di conferma dopo ogni modifica.
6. **Notifiche**: lo stato delle notifiche sul dispositivo, il pulsante per attivarle e quello per inviare una notifica di prova.
7. **Ultime manovre**: l'elenco cronologico delle azioni del sistema e dell'utente, con data e ora (avvii, stop, regolazioni, cambi di modalità, consensi, modifiche alla carica notturna).

Due avvisi possono comparire in cima alla sezione: uno quando l'ultimo ciclo del sistema non è riuscito, con il motivo, e uno quando il sistema è in modalità di prova, cioè mostra cosa farebbe senza inviare comandi all'auto.

### Sezione "Casa"

Raccoglie tutto ciò che l'impianto fotovoltaico racconta di sé. È di sola consultazione.

1. **Oggi**: energia prodotta dai pannelli, consumata (casa e auto insieme), presa dalla rete e ceduta alla rete, più due percentuali: l'autosufficienza (quanta parte dei consumi è stata coperta senza comprare energia) e l'autoconsumo (quanta parte della produzione è stata usata in casa).
2. **Pannelli e inverter adesso**: potenza, tensione e corrente di ciascuna delle due stringhe di pannelli, temperatura e potenza dell'inverter, tensione e frequenza della rete.
3. **Batteria di casa**: carica ed energia disponibile, potenza in ingresso o uscita, stato di salute, numero di cicli, temperatura, e la resa (energia restituita rispetto a quella immagazzinata).
4. **Produzione dei pannelli, ultimi 14 giorni**: un grafico con una colonna per giorno. Selezionando un giorno compaiono consumi, scambi con la rete e autosufficienza.
5. **Da quando esiste l'impianto**: i totali di energia prodotta, comprata, ceduta e passata dalla batteria.
6. **Ultimi avvisi dell'inverter**: gli ultimi eventi anomali registrati, con data e durata.

I dati si aggiornano ogni 5 minuti.

### Sezione "Auto"

Raccoglie le informazioni sull'auto e lo storico delle ricariche.

1. **Intestazione**: nome dell'auto, data e ora dell'ultima lettura, pulsante per aggiornare i dati. Se l'auto è in standby il pulsante la sveglia e attende che risponda, cosa che può richiedere fino a un minuto.
2. **Dati dell'auto**: batteria e limite di carica impostato, autonomia stimata in chilometri, chilometri totali, temperatura interna ed esterna, versione del software e stato di chiusura.
3. **Batteria e ricarica**: indicatori calcolati dalle letture nel tempo. Capacità stimata della batteria, autonomia che l'auto si attribuisce al 100%, efficienza della ricarica (energia arrivata in batteria rispetto a quella presa dalla presa), tensione della presa sotto carico, consumo da ferma, batteria utilizzabile, numero di cariche consecutive al 100%, corrente offerta dal cavo. Ogni indicatore compare quando ci sono abbastanza dati: prima, indica quanti ne mancano.
4. **Stato dell'auto**: un elenco di voci con il loro valore, per esempio sportello di ricarica, carica programmata, climatizzatore, modalità Sentinella, dashcam, porte e finestrini, aggiornamenti software.
5. **Ricariche a casa, ultimi 14 giorni**: un grafico con una colonna per giorno. Ogni colonna è divisa in tre parti: energia dal sole, energia caricata di giorno da rete o batteria, energia caricata di notte con Octopus. Sotto, un riepilogo del mese in corso: kWh totali, quota dal sole in percentuale, kWh notturni e kWh diurni non solari. La quota di sole è una stima.
6. **Consumi di guida**: una stima dei kWh ogni 100 km, calcolata confrontando chilometri percorsi e batteria consumata. Compare dopo che sono stati registrati almeno 50 km. Prima, viene indicato che la stima non è ancora disponibile.
7. **Sotto il cofano**: dati tecnici nei codici interni di Tesla (motore, computer di guida, colore, cerchi e simili).
8. **Esci**: chiude la sessione.

Lo storico parte dal giorno di attivazione del sistema: non esistono dati precedenti.

## Le notifiche

Le notifiche arrivano sul telefono anche ad app chiusa. Su iPhone funzionano solo se l'app è stata aggiunta alla schermata Home e aperta da lì: se l'utente la apre dal browser, al posto del pulsante di attivazione trova la spiegazione di come aggiungerla.

Le notifiche inviate sono quattro:

| Notifica | Quando arriva |
| --- | --- |
| Carica avviata | Il sistema ha avviato una carica, con la corrente e il motivo |
| Carica fermata | Il sistema ha fermato una sua carica, con il motivo |
| Sole insufficiente per l'auto | Serve il consenso per caricare da rete o batteria. Una volta al giorno |
| TeslaCharger ha un problema | Tre cicli consecutivi non sono riusciti |

Toccando una notifica si apre l'app.

## Cosa può fare l'utente

In tutto le azioni disponibili sono poche:

- scegliere una delle tre modalità;
- autorizzare o revocare, per la giornata, la carica da rete o batteria;
- impostare livello e orario della carica notturna;
- attivare le notifiche sul dispositivo e inviarne una di prova;
- aggiornare i dati dell'auto;
- accedere e uscire.

Tutto il resto è deciso dal sistema.

## Comportamenti da conoscere

- **I dati non sono in diretta.** L'impianto si aggiorna ogni 5 minuti e l'auto viene letta solo quando serve, perché ogni lettura ha un costo e la tiene sveglia. Ogni dato dell'auto ha quindi un orario di lettura, che può essere di ore prima.
- **Le azioni non hanno effetto immediato.** Dopo un cambio di modalità il sistema rivaluta subito la situazione, ma se l'auto è in standby deve prima svegliarla: tra il tocco e l'avvio reale della carica possono passare alcuni minuti.
- **Le cariche avviate a mano non vengono toccate.** Se l'utente avvia una carica dall'app di Octopus, il sistema la riconosce come non sua e non la regola né la ferma.
- **Il sistema sa se l'auto è a casa.** Se il cavo non è collegato non fa nulla e non chiede nulla.
- **La pagina si aggiorna da sola** ogni 30 secondi e ogni volta che l'app torna in primo piano.
- **Tema chiaro e scuro**: l'interfaccia segue l'impostazione del dispositivo.

## Glossario

- **Carica immediata (boost)**: la funzione di Octopus che fa partire subito la carica, fuori dalle ore pianificate. È lo strumento con cui il sistema avvia le cariche diurne.
- **Fascia diurna**: dalle 8:30 alle 19:00, l'intervallo in cui può avvenire la carica col sole.
- **Quota dei pannelli**: la parte della produzione solare destinata all'auto, pari all'80%.
- **Corrente minima e massima**: 5 e 12 ampere, cioè circa 1,1 e 2,8 kW.
- **Consenso**: l'autorizzazione dell'utente a caricare da rete o batteria di casa quando il sole non basta, valida fino a sera.
- **Standby**: lo stato in cui l'auto, ferma da un po', non risponde alle letture.
