// Verifica dell'accesso alla Solax Developer API: elenca impianti e dispositivi
// e stampa i dati in tempo reale.
// Uso: node --env-file=.env scripts/probe-solax.mjs

const BASE = "https://openapi-eu.solaxcloud.com";
const { SOLAX_CLIENT_ID, SOLAX_CLIENT_SECRET } = process.env;
if (!SOLAX_CLIENT_ID || !SOLAX_CLIENT_SECRET) {
  console.error("Compila SOLAX_CLIENT_ID e SOLAX_CLIENT_SECRET nel file .env");
  process.exit(1);
}

async function getToken() {
  const res = await fetch(`${BASE}/openapi/auth/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: SOLAX_CLIENT_ID,
      client_secret: SOLAX_CLIENT_SECRET,
      grant_type: "client_credentials",
    }),
  });
  const data = await res.json();
  if (data.code !== 0 || !data.result?.access_token) {
    throw new Error(`Token non ottenuto: ${JSON.stringify(data)}`);
  }
  console.log("Token ok, scope:", data.result.scope, "scadenza (s):", data.result.expires_in);
  return data.result.access_token;
}

const token = await getToken();

async function get(path, params) {
  const url = new URL(BASE + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, { headers: { Authorization: `bearer ${token}` } });
  return res.json();
}

function show(title, data) {
  console.log(`\n=== ${title} ===\n${JSON.stringify(data, null, 2)}`);
}

// businessType: 1 residenziale, 4 commerciale
for (const businessType of [1, 4]) {
  const plants = await get("/openapi/v2/plant/page_plant_info", { businessType, pageNo: 1 });
  show(`Impianti (businessType ${businessType})`, plants);
  const records = plants.result?.records ?? plants.result?.list ?? plants.result ?? [];
  if (!Array.isArray(records) || records.length === 0) continue;

  for (const plant of records) {
    const plantId = plant.plantId ?? plant.id;
    show(
      `Tempo reale impianto ${plantId}`,
      await get("/openapi/v2/plant/realtime_data", { plantId, businessType }),
    );
    // deviceType: 1 inverter, 2 batteria, 3 contatore, 4 wallbox
    for (const deviceType of [1, 2, 3, 4]) {
      const devices = await get("/openapi/v2/device/page_device_info", {
        businessType,
        deviceType,
        pageNo: 1,
        plantId,
      });
      show(`Dispositivi tipo ${deviceType}`, devices);
      const list = devices.result?.records ?? devices.result?.list ?? devices.result ?? [];
      const sns = Array.isArray(list) ? list.map((d) => d.deviceSn ?? d.sn).filter(Boolean) : [];
      if (sns.length === 0) continue;
      show(
        `Tempo reale dispositivi tipo ${deviceType}`,
        await get("/openapi/v2/device/realtime_data", {
          snList: sns.slice(0, 10).join(","),
          deviceType,
          businessType,
        }),
      );
    }
  }
}
