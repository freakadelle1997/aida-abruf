// Fortschritt des laufenden Abgleichs (schreibt der GitHub-Lauf nach KV "status:lauf").
export async function onRequestGet({ env }) {
  const v = await env.SUBMISSIONS.get("status:lauf");
  return new Response(v || "{}", {
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}
