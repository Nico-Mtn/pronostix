/* Pronostix — Cloudflare Worker : stockage des abonnements push (gratuit, KV).

   Un abonnement = UN enregistrement par navigateur, portant la liste des
   compétitions suivies (topics). C'est ce qui permet de s'abonner à la Ligue 1,
   à la Premier League, ou aux deux, sans dupliquer la souscription : à la
   révocation (410), une seule clé est à supprimer.

   Endpoints :
     POST /subscribe    → { subscription, topics:[…] }  ajoute des topics (fusion)
     POST /unsubscribe  → { endpoint, topics:[…] }      retire des topics
     GET  /topics?endpoint=…                            topics suivis par cet abonné
     GET  /list?key=…&topic=…                           abonnés d'un topic (protégé)
     POST /remove?key=…                                 retire un abonnement expiré (protégé)

   Binding KV attendu : SUBS · Secret attendu : LIST_SECRET
   Valeur stockée sous sub:<sha256(endpoint)> :
     { sub: <PushSubscription>, topics: […], maj: <ISO> }

   Auteur : Nico-Mtn (https://github.com/Nico-Mtn) */

const TOPICS_OK = ['ligue-1-france', 'premier-league-england', 'pronostics-cdm2026'];
const ORIGINE = 'https://nico-mtn.github.io';

export default {
  async fetch(req, env) {
    const cors = {
      'Access-Control-Allow-Origin': ORIGINE,
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type'
    };
    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
    const url = new URL(req.url);
    const json = (o, s) => new Response(JSON.stringify(o),
      { status: s || 200, headers: Object.assign({ 'Content-Type': 'application/json' }, cors) });

    // Liste blanche : on n'enregistre que des topics connus.
    const filtre = (t) => (Array.isArray(t) ? t : []).filter(x => TOPICS_OK.indexOf(x) >= 0);

    if (req.method === 'POST' && url.pathname === '/subscribe') {
      const body = await req.json().catch(() => null);
      // Rétrocompatible : l'ancien site postait directement l'objet PushSubscription.
      const sub = body && (body.subscription || (body.endpoint ? body : null));
      // Format historique (page Coupe du Monde) : la PushSubscription arrive seule,
      // sans liste de topics. On la rattache à la CdM plutôt que de la refuser.
      const brut = !!(body && !body.subscription && body.endpoint);
      const topics = brut && !(body.topics || []).length
        ? ['pronostics-cdm2026'] : filtre(body && body.topics);
      if (!sub || !sub.endpoint || !topics.length) return json({ erreur: 'requête invalide' }, 400);
      const cle = 'sub:' + await sha(sub.endpoint);
      const ancien = await env.SUBS.get(cle, 'json');
      const fusion = Array.from(new Set((ancien && ancien.topics || []).concat(topics)));
      await env.SUBS.put(cle, JSON.stringify({ sub, topics: fusion, maj: new Date().toISOString() }));
      return json({ topics: fusion });
    }

    if (req.method === 'POST' && url.pathname === '/unsubscribe') {
      const body = await req.json().catch(() => ({}));
      if (!body.endpoint) return json({ erreur: 'endpoint manquant' }, 400);
      const cle = 'sub:' + await sha(body.endpoint);
      const rec = await env.SUBS.get(cle, 'json');
      if (!rec) return json({ topics: [] });
      const retirer = filtre(body.topics);
      const reste = (rec.topics || []).filter(t => retirer.indexOf(t) < 0);
      if (reste.length) {
        await env.SUBS.put(cle, JSON.stringify({ sub: rec.sub, topics: reste, maj: new Date().toISOString() }));
      } else {
        await env.SUBS.delete(cle);   // plus aucun topic suivi : on ne garde rien
      }
      return json({ topics: reste });
    }

    if (req.method === 'GET' && url.pathname === '/topics') {
      const ep = url.searchParams.get('endpoint');
      if (!ep) return json({ topics: [] });
      const rec = await env.SUBS.get('sub:' + await sha(ep), 'json');
      return json({ topics: (rec && rec.topics) || [] });
    }

    if (req.method === 'GET' && url.pathname === '/list') {
      if (url.searchParams.get('key') !== env.LIST_SECRET) return new Response('forbidden', { status: 403 });
      const topic = url.searchParams.get('topic');
      const out = [];
      let curseur;
      // KV.list() plafonne à 1000 clés : on suit le curseur, sinon la liste
      // serait silencieusement tronquée dès que la base grandit.
      do {
        const page = await env.SUBS.list({ prefix: 'sub:', cursor: curseur });
        for (const k of page.keys) {
          const rec = await env.SUBS.get(k.name, 'json');
          if (!rec) continue;
          // Un enregistrement sans topics vient de l'ancien format : on le rattache
          // à la Coupe du Monde, seule compétition qui existait alors.
          const topics = rec.topics || ['pronostics-cdm2026'];
          const sub = rec.sub || rec;
          if (!topic || topics.indexOf(topic) >= 0) out.push(sub);
        }
        curseur = page.list_complete ? null : page.cursor;
      } while (curseur);
      return json(out);
    }

    if (req.method === 'POST' && url.pathname === '/remove') {
      if (url.searchParams.get('key') !== env.LIST_SECRET) return new Response('forbidden', { status: 403 });
      const body = await req.json().catch(() => ({}));
      if (body.endpoint) await env.SUBS.delete('sub:' + await sha(body.endpoint));
      return json({ ok: true });
    }

    return new Response('Pronostix push service', { headers: cors });
  },

  // Cron Triggers Cloudflare (fiables, contrairement au cron GitHub, qui sautait
  // régulièrement des exécutions). Déclenche les workflows GitHub via l'API.
  // Secret attendu : GH_TOKEN (PAT fine-grained, dépôt Nico-Mtn/pronostix,
  // permission Actions: Read and write).
  // Crons configurés, UN déclencheur par ligne dans le tableau de bord :
  //   "*/15 11-22 * * *" → annonce de journée, 1 h avant le premier coup d'envoi
  //   "0 7,8 * * *"      → résumé de la journée terminée, 9 h Paris (été / hiver)
  // Commentaires en // et non en /* */ : l'expression « */15 » refermait le bloc
  // de commentaire et cassait la syntaxe du fichier entier.
  async scheduled(event, env, ctx) {
    const REPO = 'Nico-Mtn/pronostix';
    const dispatch = async (wf) => {
      const r = await fetch(
        'https://api.github.com/repos/' + REPO + '/actions/workflows/' + wf + '/dispatches',
        {
          method: 'POST',
          headers: {
            'Authorization': 'Bearer ' + env.GH_TOKEN,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'pronostix-cron'
          },
          body: JSON.stringify({ ref: 'main' })
        }
      );
      // Sans ce log, un dispatch refusé (token expiré, dépôt renommé) passerait
      // totalement inaperçu — c'est exactement ce qui est arrivé après le renommage.
      if (!r.ok) console.log('dispatch ' + wf + ' → HTTP ' + r.status + ' ' + (await r.text()));
      return r;
    };
    ctx.waitUntil(dispatch('notify-journee.yml'));
  }
};

async function sha(s) {
  const b = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
}
