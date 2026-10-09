// Listet die noch nicht verarbeiteten Einreichungen (für die Anzeige in der Übersicht).
export async function onRequestGet({ env }) {
  const list = await env.SUBMISSIONS.list();
  const items = [];
  for (const k of list.keys) {
    const v = await env.SUBMISSIONS.get(k.name);
    if (v) items.push(JSON.parse(v));
  }
  return new Response(JSON.stringify({ items }), {
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
