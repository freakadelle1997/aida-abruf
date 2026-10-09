// Merkt eine Reise zum Entfernen vor. Der nächste stündliche Lauf nimmt sie aus der Reiseliste.
// Geschützt durch Cloudflare Access (nur eingeloggte Nutzer erreichen die Seite und diesen Endpunkt).
const RE = /^[A-Z]{2,3}\d{7,8}$/;

export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Ungültige Anfrage" }, 400);
  }
  const journey = (body.journey || "").trim();
  if (!RE.test(journey)) {
    return json({ error: "Unbekannte Reise." }, 400);
  }
  const entry = {
    action: "remove",
    journey,
    submitted_at: new Date().toISOString(),
    submitted_by: request.headers.get("Cf-Access-Authenticated-User-Email") || null,
  };
  await env.SUBMISSIONS.put("remove:" + journey, JSON.stringify(entry));
  return json({ ok: true, journey });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
