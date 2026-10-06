/* Pronostix — notifications de championnat, par journée et par compétition.

   Deux messages, envoyés séparément pour la Ligue 1 et pour la Premier League :

     ANNONCE — 1 h avant le premier coup d'envoi d'une journée : les affiches,
               les horaires et le pronostic de Nono.
     RÉSUMÉ  — le lendemain à 9 h (heure de Paris), quand une journée vient de
               se terminer : ce que Nono avait vu juste, et un lien direct vers
               cette journée du calendrier.

   Le message est composé ICI, pas dans le service worker : le push porte donc son
   contenu. C'est plus robuste (le SW n'a pas à deviner quelle compétition lire) et
   cela reste lisible sur les plateformes où le SW est bridé.

   Un état (data/notify_state.json) mémorise la dernière journée notifiée par
   compétition et par type : aucun doublon, même si le cron passe toutes les 15 min.

   Variables d'environnement : VAPID_PUBLIC, VAPID_PRIVATE, SUBS_URL, LIST_SECRET.
   Option : DRY_RUN=1 pour tout calculer et afficher sans rien envoyer.

   Auteur : Nico-Mtn (https://github.com/Nico-Mtn) */
import webpush from 'web-push';
import { readFileSync, writeFileSync } from 'node:fs';

const {
  VAPID_PUBLIC, VAPID_PRIVATE,
  VAPID_SUBJECT = 'mailto:nicolas.martin@simplebo.fr',
  SUBS_URL, LIST_SECRET, DRY_RUN
} = process.env;

const SEC = DRY_RUN === '1';
if (!SEC && (!VAPID_PUBLIC || !VAPID_PRIVATE || !SUBS_URL || !LIST_SECRET)) {
  console.error('Secrets manquants (VAPID_PUBLIC, VAPID_PRIVATE, SUBS_URL, LIST_SECRET).');
  process.exit(1);          // échec franc : un run vert sans envoi serait trompeur
}
if (!SEC) webpush.setVapidDetails(VAPID_SUBJECT, VAPID_PUBLIC, VAPID_PRIVATE);

const BASE = 'https://nico-mtn.github.io/pronostix';
const ETAT = 'data/notify_state.json';
const COMPETITIONS = [
  { topic: 'ligue-1-france', nom: 'Ligue 1', emoji: '🇫🇷' },
  { topic: 'premier-league-england', nom: 'Premier League', emoji: '🏴' }
];
const AVANT_MIN = 45, AVANT_MAX = 75;   // fenêtre d'envoi de l'annonce, en minutes
const HEURE_RESUME = 9;                 // heure de Paris pour le résumé
// Un résumé n'a de sens que le lendemain. Au-delà de ce délai après le dernier
// match de la journée, il est périmé : on le marque comme traité sans l'envoyer.
// Sans ce garde-fou, la première mise en service (ou une panne de quelques jours)
// rattrapait un vieux bilan, et les abonnés recevaient la J5 deux semaines après.
const RESUME_MAX_H = 36;
const DUREE_MATCH_H = 2;                // coup d'envoi → fin, marge comprise

const maintenant = new Date();

/* Heure de Paris. On passe par formatToParts plutôt que par un parsing de chaîne :
   le format court varie selon la locale et la version d'ICU (« 9 », « 09 », « 09 h »).
   Repli sur un décalage saisonnier si le fuseau n'est pas disponible. */
function heureDeParis(d) {
  try {
    const parts = new Intl.DateTimeFormat('en-GB',
      { timeZone: 'Europe/Paris', hour: '2-digit', hourCycle: 'h23' }).formatToParts(d);
    const h = Number((parts.find(p => p.type === 'hour') || {}).value);
    if (Number.isInteger(h)) return h;
  } catch { /* ICU incomplet : on retombe sur l'approximation ci-dessous */ }
  const mois = d.getUTCMonth() + 1;
  return (d.getUTCHours() + (mois > 3 && mois < 11 ? 2 : 1)) % 24;
}
const heureParis = heureDeParis(maintenant);

function lireEtat() {
  try { return JSON.parse(readFileSync(ETAT, 'utf8')); }
  catch { return { _meta: {}, etat: {} }; }
}
function ecrireEtat(e) {
  e._meta = {
    role: "Dernière journée notifiée, par compétition et par type d'envoi. Empêche "
        + "tout doublon : le cron passe toutes les 15 minutes, l'envoi n'a lieu qu'une fois.",
    maj: new Date().toISOString(), author: 'Nico-Mtn'
  };
  writeFileSync(ETAT, JSON.stringify(e, null, 2) + '\n');
}

/* Les matchs d'une journée, dans l'ordre chronologique. */
const parJournee = (data, j) => (data.matches || [])
  .filter(m => m.j === j)
  .sort((a, b) => String(a.sort).localeCompare(String(b.sort)));

/* Journée à venir = la première qui compte encore un match non joué. */
function journeeAVenir(data) {
  const js = [...new Set((data.matches || []).map(m => m.j).filter(Boolean))].sort((a, b) => a - b);
  return js.find(j => parJournee(data, j).some(m => !m.reel)) || null;
}

/* Dernière journée entièrement disputée. */
function journeeTerminee(data) {
  const js = [...new Set((data.matches || []).map(m => m.j).filter(Boolean))].sort((a, b) => b - a);
  return js.find(j => { const ms = parJournee(data, j); return ms.length && ms.every(m => m.reel); }) || null;
}

const nom = (data, cle) => ((data.noms || {})[cle] || {}).a || cle;

function messageAnnonce(data, comp, j) {
  const ms = parJournee(data, j);
  const lignes = ms.slice(0, 4).map(m => {
    const p = m.prono ? ` (prono ${m.prono[0]}-${m.prono[1]})` : '';
    return `${nom(data, m.home)}–${nom(data, m.away)} ${m.heure || ''}${p}`.trim();
  });
  const reste = ms.length > 4 ? ` +${ms.length - 4} autres` : '';
  return {
    title: `${comp.emoji} ${comp.nom} — J${j} dans 1 h`,
    body: lignes.join(' · ') + reste,
    tag: `px-${comp.topic}-annonce-${j}`,
    url: `${BASE}/${comp.topic}/#cal-J${j}`
  };
}

function messageResume(data, comp, j) {
  const ms = parJournee(data, j);
  const exact = ms.filter(m => m.statut === 'exact').length;
  const bon = ms.filter(m => m.statut === 'bon').length;
  const taux = ms.length ? Math.round((exact + bon) / ms.length * 100) : 0;
  const scores = ms.slice(0, 3)
    .map(m => `${nom(data, m.home)} ${m.reel[0]}-${m.reel[1]} ${nom(data, m.away)}`).join(' · ');
  return {
    title: `${comp.emoji} ${comp.nom} — bilan de la J${j}`,
    body: `Nono : ${exact} exact${exact > 1 ? 's' : ''}, ${bon} bon${bon > 1 ? 's' : ''} `
        + `sur ${ms.length} matchs (${taux} %). ${scores}`,
    tag: `px-${comp.topic}-resume-${j}`,
    url: `${BASE}/${comp.topic}/#cal-J${j}`
  };
}

async function abonnes(topic) {
  const base = SUBS_URL.replace(/\/$/, '');
  const r = await fetch(`${base}/list?key=${encodeURIComponent(LIST_SECRET)}&topic=${encodeURIComponent(topic)}`);
  if (!r.ok) throw new Error(`liste des abonnés (${base}) : HTTP ${r.status}`);
  return r.json();
}

async function envoyer(topic, message) {
  if (SEC) { console.log('[DRY_RUN]', topic, JSON.stringify(message)); return; }
  const base = SUBS_URL.replace(/\/$/, '');
  const subs = await abonnes(topic);
  const charge = JSON.stringify(message);
  let ok = 0, partis = 0;
  // Envoi par lots : une boucle strictement séquentielle ne tiendrait pas
  // le budget du job dès quelques centaines d'abonnés.
  for (let i = 0; i < subs.length; i += 20) {
    await Promise.all(subs.slice(i, i + 20).map(async (sub) => {
      try { await webpush.sendNotification(sub, charge); ok++; }
      catch (e) {
        if (e.statusCode === 404 || e.statusCode === 410) {
          partis++;
          await fetch(`${base}/remove?key=${encodeURIComponent(LIST_SECRET)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ endpoint: sub.endpoint })
          }).catch(() => {});
        } else console.error('Erreur push:', e.statusCode || e.message);
      }
    }));
  }
  console.log(`  → ${topic} : ${ok} envoyé(s), ${partis} abonnement(s) expiré(s) retiré(s)`);
}

// En mode à blanc, on vérifie quand même la liaison avec le Worker : c'est le
// seul moyen de valider SUBS_URL et LIST_SECRET sans envoyer de notification.
// La lecture de la liste ne modifie rien.
if (SEC) {
  if (!SUBS_URL || !LIST_SECRET) {
    console.log('[DRY_RUN] Worker non vérifié : SUBS_URL ou LIST_SECRET absent.');
  } else {
    for (const comp of COMPETITIONS) {
      try {
        const subs = await abonnes(comp.topic);
        console.log(`[DRY_RUN] Worker joignable — ${comp.nom} : ${subs.length} abonné(s)`);
      } catch (e) {
        console.error(`[DRY_RUN] Worker INJOIGNABLE — ${comp.nom} : ${e.message}`);
        process.exitCode = 1;   // run rouge : un secret erroné ne doit pas passer inaperçu
      }
    }
  }
}

const etat = lireEtat();
let modifie = false;

for (const comp of COMPETITIONS) {
  let data;
  try { data = JSON.parse(readFileSync(`${comp.topic}/data.json`, 'utf8')); }
  catch { console.log(`${comp.topic} : data.json introuvable, ignoré`); continue; }

  const vu = etat.etat[comp.topic] || (etat.etat[comp.topic] = { annonce: null, resume: null });

  // ─── Annonce : 1 h avant le premier coup d'envoi de la journée à venir ───
  const jA = journeeAVenir(data);
  if (jA && vu.annonce !== jA) {
    const premier = parJournee(data, jA).find(m => !m.reel && m.sort);
    if (premier) {
      const minutes = (new Date(premier.sort) - maintenant) / 60000;
      if (minutes >= AVANT_MIN && minutes <= AVANT_MAX) {
        const msg = messageAnnonce(data, comp, jA);
        console.log(`${comp.nom} : annonce J${jA} (coup d'envoi dans ${Math.round(minutes)} min)`);
        await envoyer(comp.topic, msg);
        vu.annonce = jA; modifie = true;
      } else {
        console.log(`${comp.nom} : J${jA} dans ${Math.round(minutes)} min — hors fenêtre d'annonce`);
      }
    }
  }

  // ─── Résumé : le lendemain à 9 h, sur la dernière journée terminée ───
  const jR = journeeTerminee(data);
  const finJ = jR ? Math.max(...parJournee(data, jR).map(m => +new Date(m.sort || 0))) : 0;
  const depuisH = finJ ? (maintenant - finJ) / 3600000 - DUREE_MATCH_H : 0;
  if (jR && vu.resume !== jR && depuisH > RESUME_MAX_H) {
    console.log(`${comp.nom} : résumé J${jR} périmé (journée terminée il y a ${Math.round(depuisH)} h) — ignoré`);
    vu.resume = jR; modifie = true;
  } else if (jR && vu.resume !== jR) {
    if (heureParis === HEURE_RESUME) {
      const msg = messageResume(data, comp, jR);
      console.log(`${comp.nom} : résumé J${jR}`);
      await envoyer(comp.topic, msg);
      vu.resume = jR; modifie = true;
    } else {
      console.log(`${comp.nom} : résumé J${jR} en attente de 9 h (il est ${heureParis} h à Paris)`);
    }
  }
}

if (modifie && !SEC) ecrireEtat(etat);
console.log(modifie ? (SEC ? 'Envois simulés (état non écrit).' : 'État mis à jour.')
                    : 'Rien à envoyer.');
