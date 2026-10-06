# 🔔 Notifications Pronostix — par championnat

Deux messages, envoyés **séparément** pour la Ligue 1 et pour la Premier League. On
s'abonne à l'une, à l'autre, ou aux deux.

| Message | Quand | Contenu |
|---|---|---|
| **Annonce de journée** | 1 h avant le premier coup d'envoi | Les affiches, les horaires, les pronos de Nono |
| **Bilan de journée** | Le lendemain à 9 h (Paris) | Exacts / bons / ratés de la journée, lien direct vers cette journée du calendrier |

> **Adresse du Worker** : `https://pronobot-push.nico-mtn.workers.dev` depuis le
> changement de sous-domaine Cloudflare d'octobre 2026. L'ancienne adresse
> (`…nicolasmartin-contact.workers.dev`) ne répond plus : le site, le gabarit de la
> Coupe du Monde et le secret `SUBS_URL` doivent tous pointer vers la nouvelle.

Tout le code est déjà dans le dépôt et déployé sur le site. **Il reste une seule
étape manuelle : mettre à jour le Worker Cloudflare.** Tant qu'elle n'est pas faite,
le bouton « Activer » des pages de championnat répond « Réessayer » — l'ancien Worker
ne connaît pas la notion de compétition.

---

## Pourquoi le Worker doit changer

L'ancien Worker rangeait tous les abonnés dans un seul sac, sans distinction de
compétition : impossible d'envoyer une notification Ligue 1 aux seuls abonnés Ligue 1.
Il pointait aussi encore sur `Nico-Mtn/pronostics-cdm2026`, l'ancien nom du dépôt —
ses appels à GitHub échouaient donc en silence depuis le renommage.

La nouvelle version stocke, pour chaque navigateur, **la liste des compétitions suivies** :

```
clé    : sub:<sha256(endpoint)>
valeur : { sub: <PushSubscription>, topics: ["ligue-1-france", …], maj: <ISO> }
```

Les abonnements existants (sans `topics`) sont automatiquement rattachés à la Coupe
du Monde : personne n'est perdu au passage.

---

## Étape 1 — Déployer le Worker

### Option A — depuis le dashboard Cloudflare (le plus simple)

C'est la méthode que tu utilises déjà (l'historique des versions indique
« Manually deployed · Dashboard »). Les bindings et les secrets sont déjà attachés
au Worker : il n'y a que le code à remplacer.

1. Ouvre le fichier `notify/worker.js` du dépôt, bouton **Raw**, puis copie tout
   (`Cmd+A`, `Cmd+C`).
2. Va sur **Cloudflare → Compute → Workers & Pages → pronobot-push → Edit code**.
3. Dans l'éditeur, `Cmd+A` puis `Cmd+V` pour tout remplacer.
4. Clique **Deploy**.

En cas de doute, l'onglet **Deployments** permet de revenir à la version précédente
en un clic.

### Option B — en ligne de commande

`wrangler.toml` contient un **id de namespace KV factice**
(`REMPLACER_PAR_TON_KV_ID`). Récupère le vrai id dans
**Cloudflare → Storage & databases → KV → pronobot-subs**, colle-le dans le fichier,
puis :

```bash
cd notify
wrangler login
wrangler deploy
```

⚠️ Sans le bon id, le déploiement créerait un namespace vide et **tous les
abonnements existants deviendraient invisibles**. L'option A évite ce piège.

---

## Étape 2 — Régler les deux crons

**Cloudflare → pronobot-push → Settings → Trigger Events → Cron Triggers.**

Retire les anciens (`0 */2 * * *` et `5 6 * * *`, qui visaient la Coupe du Monde) et
mets :

| Cron | Rôle |
|---|---|
| `*/15 11-22 * * *` | Cherche toutes les 15 min s'il y a une journée qui démarre dans 1 h |
| `0 7,8 * * *` | Déclenche le bilan à 9 h Paris (7 h UTC l'été, 8 h l'hiver) |

Le script vérifie lui-même l'heure de Paris : les deux passages ne produisent qu'un
seul envoi. Et `data/notify_state.json` mémorise ce qui est déjà parti — un cron qui
repasse n'envoie jamais deux fois le même message.

---

## Étape 3 — Vérifier que le Worker répond

Dans le navigateur, ouvre :

```
https://pronobot-push.nico-mtn.workers.dev/topics?endpoint=test
```

Réponse attendue : `{"topics":[]}`.
Si tu obtiens `Pronostix push service`, le nouveau code n'est pas déployé.

---

## Étape 4 — Tester l'envoi sans rien envoyer

**GitHub → Actions → « Notifications de journée » → Run workflow**, en cochant
**dry_run**.

Le job affiche le message qu'il aurait envoyé, sans notifier personne :

```
Ligue 1 : annonce J3 (coup d'envoi dans 58 min)
[DRY_RUN] ligue-1-france {"title":"🇫🇷 Ligue 1 — J3 dans 1 h", …}
```

Si rien ne part, c'est normal : hors de la fenêtre d'une heure avant le coup d'envoi
et hors de 9 h du matin, il n'y a rien à envoyer. Le log le dit explicitement.

---

## Étape 5 — S'abonner et recevoir

1. Ouvre `https://nico-mtn.github.io/pronostix/ligue-1-france/` sur ton téléphone.
2. Bouton **🔔 Activer**, puis autorise les notifications.
3. Le bouton passe à **✓ Activé**.
4. Recommence sur la page Premier League si tu veux les deux.

Pour tester un envoi réel tout de suite, ouvre `data/notify_state.json`, remets la
valeur concernée à `null`, commit — puis relance le workflow **sans** `dry_run`
pendant la fenêtre horaire.

---

## Secrets GitHub attendus

Déjà en place depuis la Coupe du Monde, rien à refaire :

| Secret | Rôle |
|---|---|
| `VAPID_PUBLIC` / `VAPID_PRIVATE` | Signature des notifications push |
| `SUBS_URL` | URL du Worker, **sans** `/subscribe` : `https://pronobot-push.nico-mtn.workers.dev` |
| `PUSH_LIST_SECRET` | Protège `/list` et `/remove` |

---

## En cas de problème

| Symptôme | Cause probable |
|---|---|
| Le bouton affiche « Réessayer » | Worker pas encore déployé (étape 1) |
| `/topics` renvoie `Pronostix push service` | Idem |
| Le workflow échoue sur « Secrets manquants » | Un secret GitHub absent ou mal nommé |
| Rien ne part alors qu'un match approche | Le message n'est envoyé qu'entre 45 et 75 min avant le coup d'envoi |
| Message reçu deux fois | `notify_state.json` n'a pas pu être committé — vérifier les droits du workflow |
| Aucun cron ne se déclenche | `GH_TOKEN` expiré, ou crons non enregistrés (étape 2) |

---

Auteur : Nico-Mtn — https://github.com/Nico-Mtn · Projet gratuit, sans publicité, sans paris.
