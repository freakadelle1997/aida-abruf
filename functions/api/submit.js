// Nimmt eine eingereichte AIDA-Reise entgegen und legt sie im KV-Speicher ab.
// Geschützt durch Cloudflare Access (nur eingeloggte Nutzer erreichen die Seite und diesen Endpunkt).
const MAX_TRIPS = 8;   // mehr hinterlegte Reisen sprengen die GitHub-Freiminuten
const RE = /aida\.de\/(?:finden|buchen)\/([A-Z]{2,3}\d{7,8})\/([A-Z]+)/;

export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Ungültige Anfrage" }, 400);
  }
  const url = (body.url || "").trim();
  const m = url.match(RE);
  if (!m) {
    return json({ error: "Kein gültiger AIDA-Reiselink (aida.de/finden/...)." }, 400);
  }
  const journey = m[1];
  // Grenze: aktuell hinterlegte Reisen (tracked.json, beim Deploy erzeugt) + offene Einreichungen
  let tracked = [];
  try {
    const r = await env.ASSETS.fetch(new URL("/tracked.json", request.url));
    if (r.ok) tracked = await r.json();
  } catch {}
  const keys = (await env.SUBMISSIONS.list()).keys.map(k => k.name);
  const adds = keys.filter(k => !k.startsWith("remove:") && !tracked.includes(k));
  const removes = keys.filter(k => k.startsWith("remove:") && tracked.includes(k.slice(7)));
  const count = tracked.length + adds.length - removes.length;
  if (!tracked.includes(journey) && !adds.includes(journey) && count >= MAX_TRIPS) {
    return json({ error: `Es sind bereits ${MAX_TRIPS} Reisen hinterlegt – bitte erst eine entfernen.` }, 409);
  }
  const entry = {
    journey,
    url,
    label: (body.label || "").trim().slice(0, 120),
    focus: (body.focus || "IA").trim().toUpperCase().slice(0, 4),
    submitted_at: new Date().toISOString(),
    submitted_by: request.headers.get("Cf-Access-Authenticated-User-Email") || null,
  };
  await env.SUBMISSIONS.put(journey, JSON.stringify(entry));
  return json({ ok: true, journey });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
