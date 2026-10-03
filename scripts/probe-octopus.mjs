// Verifica dell'accesso a Octopus Energy Italia (sola lettura): account,
// dispositivi Intelligent Octopus e finestre di carica pianificate.
// Uso: node --env-file=.env scripts/probe-octopus.mjs

const ENDPOINT = "https://api.oeit-kraken.energy/v1/graphql/";
const { OCTOPUS_EMAIL, OCTOPUS_PASSWORD } = process.env;
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

function show(title, data) {
  console.log(`\n=== ${title} ===\n${JSON.stringify(data, null, 2)}`);
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
console.log("Login ok");

const viewer = await gql(`query { viewer { accounts { number } } }`);
const accounts = viewer.data?.viewer?.accounts ?? [];
show("Account", accounts);

for (const { number } of accounts) {
  const devices = await gql(
    `query ($accountNumber: String!) {
      devices(accountNumber: $accountNumber) {
        id
        name
        deviceType
        provider
        integrationDeviceId
        status { current currentState isSuspended }
        preferences { mode targetType unit schedules { dayOfWeek max min time } }
        alerts { message publishedAt }
        ... on SmartFlexVehicle { vehicleVariant { model batterySize } }
      }
    }`,
    { accountNumber: number },
  );
  show(`Dispositivi ${number}`, devices);

  for (const device of devices.data?.devices ?? []) {
    show(
      `Finestre pianificate ${device.name}`,
      await gql(
        `query ($deviceId: String!) {
          flexPlannedDispatches(deviceId: $deviceId) { start end type energyAddedKwh }
        }`,
        { deviceId: device.id },
      ),
    );
  }
}
