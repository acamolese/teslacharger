// Stato, avvio e annullamento della carica immediata (boost) di Intelligent Octopus.
// ATTENZIONE: "boost" e "cancel" agiscono davvero sulla ricarica dell'auto.
// Uso: node --env-file=.env scripts/octopus-boost.mjs status|boost|cancel

const ENDPOINT = "https://api.oeit-kraken.energy/v1/graphql/";
const { OCTOPUS_EMAIL, OCTOPUS_PASSWORD } = process.env;
const action = process.argv[2] ?? "status";
if (!["status", "boost", "cancel"].includes(action)) {
  console.error("Azione non valida. Usa: status, boost oppure cancel");
  process.exit(1);
}
if (!OCTOPUS_EMAIL || !OCTOPUS_PASSWORD) {
  console.error("Compila OCTOPUS_EMAIL e OCTOPUS_PASSWORD nel file .env");
  process.exit(1);
}

let token = null;

async function gql(query, variables = {}) {
  const res = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: token } : {}),
    },
    body: JSON.stringify({ query, variables }),
  });
  return res.json();
}

const login = await gql(
  `mutation ($email: String!, $password: String!) {
    obtainKrakenToken(input: { email: $email, password: $password }) { token }
  }`,
  { email: OCTOPUS_EMAIL, password: OCTOPUS_PASSWORD },
);
token = login.data?.obtainKrakenToken?.token;
if (!token) {
  console.error("Login non riuscito:", JSON.stringify(login.errors ?? login));
  process.exit(1);
}

const viewer = await gql(`query { viewer { accounts { number } } }`);
const accountNumber = viewer.data?.viewer?.accounts?.[0]?.number;

async function readState() {
  const res = await gql(
    `query ($accountNumber: String!) {
      devices(accountNumber: $accountNumber) {
        id
        name
        deviceType
        status { current currentState isSuspended }
      }
    }`,
    { accountNumber },
  );
  const device = (res.data?.devices ?? []).find((d) => d.deviceType === "ELECTRIC_VEHICLES");
  if (!device) throw new Error(`Nessun veicolo trovato: ${JSON.stringify(res)}`);
  const dispatches = await gql(
    `query ($deviceId: String!) {
      flexPlannedDispatches(deviceId: $deviceId) { start end type energyAddedKwh }
    }`,
    { deviceId: device.id },
  );
  return { device, dispatches: dispatches.data?.flexPlannedDispatches ?? dispatches.errors };
}

function print(label, { device, dispatches }) {
  console.log(
    `[${new Date().toLocaleTimeString("it-IT")}] ${label}: ${device.name} stato=${device.status.currentState} sospeso=${device.status.isSuspended}`,
  );
  console.log("  finestre:", JSON.stringify(dispatches));
}

const before = await readState();
print("prima", before);

if (action !== "status") {
  const res = await gql(
    `mutation ($input: UpdateBoostChargeInput!) {
      updateBoostCharge(input: $input) { id }
    }`,
    { input: { deviceId: before.device.id, action: action === "boost" ? "BOOST" : "CANCEL" } },
  );
  console.log("risposta:", JSON.stringify(res));
  print("subito dopo", await readState());
}
