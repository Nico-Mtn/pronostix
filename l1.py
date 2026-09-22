# -*- coding: utf-8 -*-
# ============================================================================
#  Pronostix — Championnats nationaux (template multi-compétitions)
#  Auteur / Author : Nico-Mtn — https://github.com/Nico-Mtn
#  Projet gratuit, sans publicité, sans paris.
#  Réutilisation libre : un CRÉDIT au créateur (Nico-Mtn) serait grandement
#  apprécié. / If you reuse this model or code, a credit to the creator
#  (Nico-Mtn) would be greatly appreciated.
# ============================================================================
"""
Génère UNE PAGE PAR CHAMPIONNAT (Ligue 1, Premier League, …) : fiches clubs,
calendrier, pronostics, classement (réel + projeté), dynamique et buteurs,
à partir de football-data.org. Toutes les compétitions listées dans LEAGUES
sont incluses dans le plan GRATUIT.

Reprend le fonctionnement éprouvé sur la Coupe du Monde 2026 :
  • moteur de prono Elo + Dixon-Coles (mêmes principes, adaptés au championnat) ;
  • PRONO FIGÉ 24 h avant le coup d'envoi (data/<prefix>_pronos.json) : le prono affiché
    la veille est EXACTEMENT celui qui sera noté — aucune dérive ;
  • notation exact / bon / raté et indice de fiabilité ;
  • mode « Réel » (résultats officiels) et « Prono » (projections de Nono).

Sorties (par championnat) : <slug>/index.html, <slug>/data.json,
          <slug>/content.sig (déploiement conditionnel),
          data/<prefix>_pronos.json (pronos figés, committés).

Usage : python3 l1.py   (variable d'env FOOTBALLDATA_KEY)
"""
import os, sys, json, math, hashlib, datetime, unicodedata, urllib.request, urllib.error, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://api.football-data.org/v4"
KEY = os.environ.get("FOOTBALLDATA_KEY", "")
FREEZE_LEAD_H = 24           # gel du prono 24 h avant le coup d'envoi

# ─── Championnats suivis ─────────────────────────────────────────────────────
# Ce fichier est un TEMPLATE : ajouter une entrée ci-dessous suffit à créer une
# nouvelle page complète (clubs, live feed, calendrier, classements, buteurs).
# Champ « code » = identifiant football-data ; tous ceux listés ici sont en plan GRATUIT.
# Champ « prefix » = préfixe des fichiers de données (data/<prefix>_pronos.json, etc.).
# Champ « flag » = code pays flagcdn. Le drapeau est servi en IMAGE et non en emoji :
# celui de l'Angleterre est une séquence de balises Unicode que la majorité des
# systèmes n'affiche pas, et remplace par un drapeau noir générique.
# Champ « couleur » = teinte de l'onglet actif du championnat, pour qu'on sache d'un
# coup d'oeil où l'on se trouve : bleu marine pour la Ligue 1, rouge pour la Premier League.
# « yt » : résumé vidéo OFFICIEL de chaque journée. Plutôt qu'une clé d'API Google
# (compte à créer, quota à surveiller), on ouvre la recherche de la chaîne officielle
# filtrée sur la journée : gratuit, sans quota, sans dépendance. Les deux gabarits
# ci-dessous ont été vérifiés sur les chaînes réelles, ils tombent sur la bonne vidéo.
#   {ord} = 1ère, 2ème…   {j} = numéro brut   {sc} = 26/27   {sl} = 2026/27
LEAGUES = [
    {"slug": "ligue-1-france", "prefix": "l1", "code": "FL1", "nom": "Ligue 1",
     "flag": "fr", "couleur": "#173a7a", "saison": 2026, "libelle": "2026-2027",
     "yt_base": "https://www.youtube.com/channel/UCQsH5XtIc9hONE1BQjucM0g/search",
     "yt_q": "Résumé {ord} journée Ligue 1 {sc}"},
    {"slug": "premier-league-england", "prefix": "pl", "code": "PL", "nom": "Premier League",
     "flag": "gb-eng", "couleur": "#c8102e", "saison": 2026, "libelle": "2026-2027",
     "yt_base": "https://www.youtube.com/@premierleague/search",
     "yt_q": "Matchweek {j} {sl} Premier League Highlights"},
]
LG = LEAGUES[0]              # championnat courant (réassigné par set_league)
OUTDIR = os.path.join(ROOT, LG["slug"])
COMP = LG["code"]
SEASON = LG["saison"]

def set_league(lg):
    """Bascule le moteur sur un championnat : chemins, code API et saison de référence."""
    global LG, OUTDIR, COMP, SEASON, PREV_SEASON, ELO_START
    LG = lg
    OUTDIR = os.path.join(ROOT, lg["slug"])
    COMP = lg["code"]
    SEASON = lg["saison"]
    PREV_SEASON = SEASON - 1
    ELO_START = {}           # recalculé pour chaque championnat

def data_path(kind):
    """Chemin d'un fichier de données propre au championnat courant."""
    return os.path.join(ROOT, "data", f"{LG['prefix']}_{kind}.json")

def flag_img(code, taille=20):
    """Drapeau servi en image (rendu identique sur tous les systèmes)."""
    return (f'<img class="flg" src="https://flagcdn.com/w40/{code}.png" '
            f'width="{taille}" height="{int(taille * 0.75)}" alt="" loading="lazy">')

def videos_journees(lg, journees):
    """URL du résumé vidéo officiel, pour chaque journée.

    On ne prétend pas pointer LA vidéo : on ouvre la chaîne officielle du
    championnat avec la recherche déjà remplie sur la journée demandée. C'est
    honnête, gratuit, et ça résiste au fait qu'une vidéo soit publiée en retard,
    renommée, ou retirée — trois cas fréquents avec les droits audiovisuels."""
    base, gabarit = lg.get("yt_base"), lg.get("yt_q")
    if not (base and gabarit):
        return {}
    a, b = lg["libelle"].split("-")            # « 2026-2027 »
    court, long = f"{a[2:]}/{b[2:]}", f"{a}/{b[2:]}"
    out = {}
    for j in journees:
        ordinal = "1ère" if j == 1 else f"{j}ème"
        q = gabarit.format(ord=ordinal, j=j, sc=court, sl=long)
        out[str(j)] = base + "?query=" + urllib.parse.quote(q)
    return out

def nav_html(current_slug):
    """Sélecteur de compétition, en icônes seules.

    Un drapeau dit « Premier League » plus vite qu'un libellé, et la barre du haut
    retrouve la place nécessaire au sélecteur de mode. Le nom reste porté par
    `title` et `aria-label` : rien n'est perdu pour un lecteur d'écran.
    """
    items = ['<a href="../" title="Accueil" aria-label="Accueil"><span class="ic">🏠</span></a>']
    for lg in LEAGUES:
        on = lg["slug"] == current_slug
        cls = ' class="on"' if on else ""
        # La teinte n'est appliquée qu'à l'onglet actif : les autres restent neutres.
        sty = f' style="background:{lg["couleur"]};border-color:{lg["couleur"]}"' if on else ""
        href = "./" if on else f"../{lg['slug']}/"
        aria = ' aria-current="page"' if on else ""
        items.append(f'<a href="{href}"{cls}{sty} title="{lg["nom"]}" '
                     f'aria-label="{lg["nom"]}"{aria}>{flag_img(lg["flag"], 24)}</a>')
    return "\n   ".join(items)

# ─── Référentiel des clubs ───────────────────────────────────────────────────
# data/clubs_ref.json apporte ce que l'API ne fournit pas : nom d'affichage long,
# abréviation pour les écrans étroits, capacité du stade et titres de champion.
# Indexé sur le shortName football-data, qui sert d'identifiant partout dans l'app.
_CLUBS = None
def clubs_ref():
    global _CLUBS
    if _CLUBS is None:
        try:
            with open(os.path.join(ROOT, "data", "clubs_ref.json"), encoding="utf-8") as f:
                _CLUBS = json.load(f).get("clubs") or {}
        except Exception:
            _CLUBS = {}
    return _CLUBS

# ─── Elo de départ des clubs ─────────────────────────────────────────────────
# Il n'est PAS saisi de mémoire : il est calculé (voir compute_elo_start) à partir
#   1. des résultats RÉELS de la saison précédente (walk-forward sur tous les matchs),
#      puis régressés vers la moyenne — une équipe ne repart jamais à 100 % de son
#      niveau de fin de saison (mercato, préparation, usure) ;
#   2. d'un ajustement qualitatif de PRÉ-SAISON (data/l1_mercato.json) : départs de
#      cadres, forte rotation d'effectif, changement d'entraîneur… Un groupe très
#      remanié met du temps à trouver son système de jeu.
# Le résultat est FIGÉ dans data/l1_elo_start.json (reproductible + économise le quota API).
ELO_START = {}                 # rempli au runtime
ELO_DEFAULT = 1500.0
PREV_SEASON = SEASON - 1       # saison de référence (2025-2026)
REGRESSION = 0.25              # part de retour vers la moyenne entre deux saisons
PROMU_ELO = 1420.0             # niveau de départ d'un promu (absent de la saison N-1)
MERCATO_CAP = 60.0             # borne de l'ajustement mercato (en points Elo)
HOME_ADV = 65.0        # avantage du terrain en points Elo (championnat)
MU_L1 = 2.72           # total de buts moyen par match (grands championnats européens)
DC_RHO = -0.13         # correction Dixon-Coles sur les petits scores
# Marge accordée au NUL dans la règle de décision. Sans elle, on annonce l'issue
# la plus probable — or le nul, même à 28 % en moyenne, n'est presque jamais LE
# maximum : le modèle ne pronostiquait donc quasiment jamais de match nul, alors
# qu'un match sur quatre en est un. Mesuré sur 6 saisons réelles (2 058 matchs) :
#   sans marge      → 0 % de pronos nuls, fiabilité 49,7 %
#   marge de 0.107  → 24,7 % en Ligue 1 et 23,7 % en Premier League, pour 23,8 %
#                     et 24,5 % de nuls réellement observés ; fiabilité 46,1 %
# On perd ~3 points de fiabilité, on gagne un pronostic crédible. Mettre 0 pour
# revenir à l'ancien comportement.
NUL_MARGE = 0.107

# Part d'imprévu dans le choix du SCORE (l'issue V/N/D, elle, ne change pas).
# Le modèle calcule une distribution complète de scores ; jusqu'ici on n'affichait
# que son sommet. Or le sommet d'un Poisson dont la moyenne tourne autour de 1,5
# vaut toujours 1 ou 2 : on annonçait donc 2-1 une fois sur deux et JAMAIS un match
# à quatre buts, alors qu'un tiers des rencontres en compte quatre ou plus.
# On tire désormais le score DANS la distribution, en l'aplatissant par p^IMPREVU
# pour épaissir la queue. Mesuré sur 6 saisons réelles (2 058 matchs) :
#            scores distincts   part de 2-1   matchs à 4 buts et +   buts/match   exacts
#   sommet             7           54,5 %              0 %              2,58       9,3 %
#   IMPREVU=0.70      45           11,4 %           40,1 %              3,19       6,5 %
#   réel              47            8,4 %           34,0 %              2,92         —
# La réussite (bonne issue V/N/D) reste identique à 46,2 % : seule la précision du
# score exact est échangée contre du réalisme. Mettre 1.0 pour un tirage fidèle à
# la distribution, ou None pour revenir au sommet.
IMPREVU = 0.70

# Identifiant d'équipe : shortName OFFICIEL de l'API (« Paris SG », « Marseille »…),
# puis nom complet, puis trigramme. Aucun découpage maison (qui produisait des
# libellés fautifs du type « de Marseille »). Le NOM AFFICHÉ, lui, vient du
# référentiel clubs_ref.json — l'identifiant, jamais, pour ne pas invalider les
# pronos figés ni l'Elo de départ, qui sont indexés dessus.
def disp(team):
    t = team or {}
    for k in ("shortName", "name"):
        v = (t.get(k) or "").strip()
        if v:
            return v if len(v) <= 24 else (t.get("tla") or v[:24])
    return t.get("tla") or "?"

# ─── Accès API ───────────────────────────────────────────────────────────────
def api_get(path):
    if not KEY:
        print("[INFO] FOOTBALLDATA_KEY absente → mode hors-ligne", file=sys.stderr)
        return None
    req = urllib.request.Request(API + path, headers={"X-Auth-Token": KEY})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] API {path} : {e}", file=sys.stderr)
        return None

def cached_get(kind, path, valide):
    """Appel API mis en cache sur disque. Les données quasi statiques (fiche des
    clubs, classement de la saison passée) ne sont demandées qu'une fois : le
    quota du plan gratuit est limité, et ces valeurs ne bougent plus."""
    p = data_path(kind)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("saison") == SEASON and valide(d.get("data")):
            return d["data"]
    except Exception:
        pass
    data = api_get(path)
    if valide(data):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"_meta": {"role": f"Cache de {path} — données stables, "
                                             "rafraîchies seulement si le fichier est supprimé.",
                                     "author": "Nico-Mtn"},
                           "saison": SEASON, "data": data}, f, ensure_ascii=False)
        except Exception as e:
            print(f"[WARN] écriture cache {kind} : {e}", file=sys.stderr)
        return data
    return None

def fetch_all():
    """Renvoie (matches, standings, scorers) depuis football-data, ou du cache local."""
    m = api_get(f"/competitions/{COMP}/matches?season={SEASON}")
    s = api_get(f"/competitions/{COMP}/standings?season={SEASON}")
    b = api_get(f"/competitions/{COMP}/scorers?season={SEASON}&limit=20")
    cache = data_path("cache")
    if m and m.get("matches"):
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({"matches": m, "standings": s, "scorers": b,
                           "_saved": datetime.datetime.now(datetime.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ")}, f, ensure_ascii=False)
        except Exception:
            pass
        return m, s, b
    # repli : dernier cache committé (le site reste debout si l'API tousse)
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                d = json.load(f)
            print("[INFO] API indisponible → repli sur le cache local", file=sys.stderr)
            return d.get("matches"), d.get("standings"), d.get("scorers")
        except Exception:
            pass
    return None, None, None

def fetch_teams():
    """Fiche des clubs (année de création, stade, couleurs) — mise en cache."""
    d = cached_get("teams", f"/competitions/{COMP}/teams?season={SEASON}",
                   lambda x: bool(x and x.get("teams")))
    out = {}
    for t in (d or {}).get("teams", []):
        out[disp(t)] = {"fonde": t.get("founded"), "venue": t.get("venue"),
                        "crest": t.get("crest"), "tla": t.get("tla"),
                        "site": t.get("website")}
    return out

def fetch_prev_table():
    """Classement FINAL de la saison précédente — mis en cache (il ne bougera plus)."""
    d = cached_get("prev_table", f"/competitions/{COMP}/standings?season={PREV_SEASON}",
                   lambda x: bool(x and x.get("standings")))
    out = {}
    for blk in (d or {}).get("standings", []):
        if blk.get("type") != "TOTAL":
            continue
        n = len(blk.get("table") or [])
        for r in blk.get("table", []):
            out[disp(r.get("team") or {})] = {"pos": r.get("position"), "pts": r.get("points"),
                                              "sur": n}
        break
    return out

# ─── Modèle : Elo live + Dixon-Coles ─────────────────────────────────────────
def team_elo(elo, name):
    return elo.get(name, ELO_START.get(name, ELO_DEFAULT))

def elo_update(elo, h, a, sh, sa):
    """Applique le résultat d'un match aux ratings (Elo pondéré par l'écart de buts)."""
    eh = elo.setdefault(h, ELO_START.get(h, ELO_DEFAULT))
    ea = elo.setdefault(a, ELO_START.get(a, ELO_DEFAULT))
    exp_h = 1.0 / (1.0 + 10 ** (-((eh + HOME_ADV) - ea) / 400.0))
    res_h = 1.0 if sh > sa else (0.5 if sh == sa else 0.0)
    k = 20.0 * (1.0 + min(abs(sh - sa), 4) * 0.12)
    elo[h] = eh + k * (res_h - exp_h)
    elo[a] = ea + k * ((1.0 - res_h) - (1.0 - exp_h))

def walk_forward(rows):
    """Parcours CHRONOLOGIQUE : chaque match joué est pronostiqué avec l'Elo tel qu'il
    était AVANT ce match (aucune fuite d'information depuis le futur), puis les ratings
    sont mis à jour. Renvoie ({match_id: prono}, elo_final).
    Indispensable quand on démarre en cours de saison : les journées déjà disputées sont
    notées honnêtement, sans que le modèle « connaisse » leur résultat."""
    preds, elo = {}, {}
    for r in rows:
        preds[str(r["id"])] = predict(elo, r["home"], r["away"], r["id"])
        if r["played"]:
            elo_update(elo, r["home"], r["away"], r["sh"], r["sa"])
    return preds, elo

def load_mercato():
    """Ajustements qualitatifs de PRÉ-SAISON, saisis à la main (jugement humain).
    Format data/<prefix>_mercato.json :
        {"ajustements": {"Olympique Lyon": {"delta": -35, "note": "départ de 3 cadres"}}}
    delta en points Elo, borné à ±MERCATO_CAP. Négatif = effectif déstabilisé (beaucoup
    de départs/arrivées, vente de joueurs cadres, nouveau staff → temps d'adaptation) ;
    positif = recrutement qui renforce nettement un groupe déjà en place."""
    p = data_path("mercato")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("ajustements", {}) or {}
    except Exception:
        return {}

def prev_season_elo():
    """Elo de FIN de saison précédente, calculé en walk-forward sur les matchs réels
    (chaque rencontre met à jour les ratings, dans l'ordre chronologique)."""
    raw = api_get(f"/competitions/{COMP}/matches?season={PREV_SEASON}")
    if not raw or not raw.get("matches"):
        return {}
    ms = []
    for m in raw["matches"]:
        ft = ((m.get("score") or {}).get("fullTime") or {})
        if ft.get("home") is None: continue
        h, a = disp(m.get("homeTeam")), disp(m.get("awayTeam"))
        if not h or not a: continue
        ms.append((m.get("utcDate") or "", h, a, ft["home"], ft["away"]))
    ms.sort(key=lambda x: x[0])
    elo = {}
    for _, h, a, sh, sa in ms:
        elo.setdefault(h, ELO_DEFAULT); elo.setdefault(a, ELO_DEFAULT)
        elo_update(elo, h, a, sh, sa)
    return elo

def compute_elo_start(teams_now):
    """Elo de départ de la saison en cours :
       Elo fin N-1 → régression vers la moyenne → ajustement mercato → promus à part.
    Calculé UNE fois puis figé dans data/l1_elo_start.json (reproductible, et le quota
    API n'est pas consommé à chaque run)."""
    path = data_path("elo_start")
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("saison") == SEASON and set(teams_now) <= set((saved.get("elo") or {}).keys()):
            return {k: float(v) for k, v in saved["elo"].items()}
    except Exception:
        pass

    prev = prev_season_elo()
    mercato = load_mercato()
    if prev:
        avg = sum(prev.values()) / len(prev)
        base = {t: avg + (e - avg) * (1.0 - REGRESSION) for t, e in prev.items()}
    else:
        base, avg = {}, ELO_DEFAULT
    elo, notes = {}, {}
    for t in (teams_now or list(base.keys())):
        if t in base:
            v = base[t]; src = "saison précédente (régressée)"
        else:
            v = PROMU_ELO; src = "promu (aucun match en N-1)"
        adj = mercato.get(t) or {}
        d = max(-MERCATO_CAP, min(MERCATO_CAP, float(adj.get("delta", 0) or 0)))
        elo[t] = round(v + d, 1)
        notes[t] = {"base": round(v, 1), "mercato": d, "source": src,
                    "note": adj.get("note", "")}
    if elo:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_meta": {
                    "role": "Elo de DÉPART de la saison : résultats réels de la saison "
                            "précédente (walk-forward) régressés vers la moyenne, + ajustement "
                            "qualitatif de pré-saison (data/<prefix>_mercato.json). Figé pour reproductibilité.",
                    "regression": REGRESSION, "promu_elo": PROMU_ELO,
                    "calcule_le": datetime.datetime.now(datetime.timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "author": "Nico-Mtn"},
                    "saison": SEASON, "elo": elo, "detail": notes}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] écriture {path} : {e}", file=sys.stderr)
    return elo

def _pois(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)

def _tau(x, y, lh, la):
    if x == 0 and y == 0: return 1.0 - lh * la * DC_RHO
    if x == 0 and y == 1: return 1.0 + lh * DC_RHO
    if x == 1 and y == 0: return 1.0 + la * DC_RHO
    if x == 1 and y == 1: return 1.0 - DC_RHO
    return 1.0

def _alea(graine):
    """Tirage DÉTERMINISTE dans [0,1), par empreinte FNV-1a 32 bits.

    Indispensable : le site se reconstruit toutes les 30 minutes et le prono d'un
    match donné ne doit jamais bouger d'un build à l'autre. `hash()` de Python est
    salé à chaque exécution, et `random` dépend d'un état global : ni l'un ni
    l'autre ne convient. FNV-1a est court, sans dépendance, et se réimplémente à
    l'identique en JavaScript, ce qui permet de rejouer un backtest hors de Python."""
    h = 2166136261
    for octet in str(graine).encode("utf-8"):
        h ^= octet
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 100000) / 100000.0

def _choisir_score(cands, graine):
    """Choisit un score parmi les candidats {(x,y): proba} de l'issue retenue.

    Sans graine (ou IMPREVU à None) on garde le sommet de la distribution, qui
    maximise les scores exacts mais ne sort jamais des 1-1 / 2-1. Avec graine, on
    tire dans la distribution aplatie : les gros scores redeviennent possibles,
    à la fréquence que le modèle leur attribue vraiment."""
    if graine is None or IMPREVU is None:
        return max(cands.items(), key=lambda kv: kv[1])[0]
    # Tri explicite : l'ordre d'itération doit être le même à chaque exécution.
    poids = [(k, v ** IMPREVU) for k, v in sorted(cands.items())]
    total = sum(p for _, p in poids) or 1.0
    seuil = _alea(graine) * total
    cumul = 0.0
    for k, p in poids:
        cumul += p
        if seuil <= cumul:
            return k
    return poids[-1][0]

def predict(elo, home, away, graine=None):
    """Prono d'un match de championnat : proba V/N/D + score plausible.

    `graine` (l'identifiant du match) rend le tirage du score reproductible."""
    eh = team_elo(elo, home) + HOME_ADV
    ea = team_elo(elo, away)
    sup = max(-1.9, min(1.9, (eh - ea) / 230.0))
    lam_h = max(0.30, min(3.20, (MU_L1 + sup) / 2.0))
    lam_a = max(0.30, min(3.20, (MU_L1 - sup) / 2.0))
    grid = {}
    for x in range(7):
        for y in range(7):
            grid[(x, y)] = _pois(x, lam_h) * _pois(y, lam_a) * _tau(x, y, lam_h, lam_a)
    tot = sum(grid.values()) or 1.0
    for k in grid: grid[k] /= tot
    pv = sum(p for (x, y), p in grid.items() if x > y)
    pn = sum(p for (x, y), p in grid.items() if x == y)
    pd = sum(p for (x, y), p in grid.items() if x < y)
    # En championnat le NUL est une issue à part entière (contrairement aux phases
    # finales) : on le retient dès qu'il talonne la meilleure issue, à NUL_MARGE près.
    meilleure = max(pv, pn, pd)
    if pn >= meilleure - NUL_MARGE:
        issue = ("N", pn)
    else:
        issue = ("V", pv) if pv >= pd else ("D", pd)
    if issue[0] == "V":   cands = {k: v for k, v in grid.items() if k[0] > k[1]}
    elif issue[0] == "N": cands = {k: v for k, v in grid.items() if k[0] == k[1]}
    else:                 cands = {k: v for k, v in grid.items() if k[0] < k[1]}
    (sx, sy) = _choisir_score(cands, graine)
    # 2e SCÉNARIO : l'issue alternative la plus probable, avec son score le plus plausible.
    # Affiché quand le match est incertain — l'utilisateur voit ce que le modèle hésite à trancher.
    autres = sorted([t for t in (("V", pv), ("N", pn), ("D", pd)) if t[0] != issue[0]],
                    key=lambda t: -t[1])
    second = None
    if autres:
        k2, p2 = autres[0]
        if k2 == "V":   c2 = {k: v for k, v in grid.items() if k[0] > k[1]}
        elif k2 == "N": c2 = {k: v for k, v in grid.items() if k[0] == k[1]}
        else:           c2 = {k: v for k, v in grid.items() if k[0] < k[1]}
        if c2:
            # Graine distincte : sans cela le 2e scénario tomberait sur le même
            # rang de la distribution que le premier, et les deux se ressembleraient.
            (bx, by) = _choisir_score(c2, None if graine is None else f"{graine}-2")
            second = {"sh": bx, "sa": by, "issue": k2, "conf": int(round(p2 * 100))}
    return {"sh": sx, "sa": sy, "issue": issue[0], "conf": int(round(issue[1] * 100)),
            "second": second,
            "proba": {"v": int(round(pv * 100)), "n": int(round(pn * 100)), "d": int(round(pd * 100))},
            "eh": int(round(eh - HOME_ADV)), "ea": int(round(ea))}

def dynamique(rows):
    """Dynamique de chaque équipe sur ses 5 derniers matchs joués : points pris,
    différence de buts et série (V/N/D). Sert l'onglet « Dynamique » et la fiche club."""
    hist = {}
    for r in rows:
        if not r["played"]:
            continue
        for team, pour, contre in ((r["home"], r["sh"], r["sa"]), (r["away"], r["sa"], r["sh"])):
            res = "V" if pour > contre else ("N" if pour == contre else "D")
            hist.setdefault(team, []).append({"res": res, "pour": pour, "contre": contre})
    out = []
    for team, h in hist.items():
        d = h[-5:]
        pts = sum(3 if x["res"] == "V" else (1 if x["res"] == "N" else 0) for x in d)
        diff = sum(x["pour"] - x["contre"] for x in d)
        # Indice lisible : points pris rapportés au maximum possible, recentré sur 0
        idx = round((pts / (3 * len(d)) - 0.5) * 4, 2) if d else 0
        out.append({"team": team, "serie": [x["res"] for x in d], "pts": pts,
                    "sur": 3 * len(d), "diff": diff, "idx": idx, "joues": len(h)})
    out.sort(key=lambda x: (-x["idx"], -x["diff"]))
    return out

def bilans(rows):
    """Bilan à domicile et à l'extérieur de chaque équipe (matchs joués uniquement)."""
    b = {}
    def slot(t, ou):
        return b.setdefault(t, {"dom": {"j": 0, "v": 0, "n": 0, "p": 0},
                                "ext": {"j": 0, "v": 0, "n": 0, "p": 0}})[ou]
    for r in rows:
        if not r["played"]:
            continue
        h, a = slot(r["home"], "dom"), slot(r["away"], "ext")
        h["j"] += 1; a["j"] += 1
        if r["sh"] > r["sa"]:   h["v"] += 1; a["p"] += 1
        elif r["sh"] < r["sa"]: h["p"] += 1; a["v"] += 1
        else:                   h["n"] += 1; a["n"] += 1
    return b

def saison_stats(rows, feed, crests):
    """Statistiques de la saison en cours, calculées sur les seuls matchs joués.

    Volontairement recalculées depuis les rencontres plutôt que reprises du
    classement de l'API : le classement peut accuser un retard de quelques
    heures sur les derniers résultats, ces chiffres ne le doivent pas.
    """
    joues = [r for r in rows if r["played"]]
    if not joues:
        return None
    buts = sum(r["sh"] + r["sa"] for r in joues)
    # Meilleure journée : seules les journées intégralement disputées sont
    # comparables. Une journée à moitié jouée fausserait le ratio.
    par_j, total_j = {}, {}
    for r in rows:
        if not r["j"]:
            continue
        total_j[r["j"]] = total_j.get(r["j"], 0) + 1
        if r["played"]:
            e = par_j.setdefault(r["j"], {"j": r["j"], "matchs": 0, "buts": 0})
            e["matchs"] += 1; e["buts"] += r["sh"] + r["sa"]
    completes = [e for j, e in par_j.items() if e["matchs"] == total_j.get(j)]
    meilleure = None
    if completes:
        for e in completes:
            e["ratio"] = round(e["buts"] / e["matchs"], 2)
        meilleure = max(completes, key=lambda e: (e["ratio"], e["buts"]))
    # Attaque et défense sur l'ensemble des rencontres jouées.
    bp, bc = {}, {}
    for r in joues:
        bp[r["home"]] = bp.get(r["home"], 0) + r["sh"]
        bp[r["away"]] = bp.get(r["away"], 0) + r["sa"]
        bc[r["home"]] = bc.get(r["home"], 0) + r["sa"]
        bc[r["away"]] = bc.get(r["away"], 0) + r["sh"]
    def extreme(d, best):
        if not d:
            return None
        v = best(d.values())
        eq = sorted(k for k, x in d.items() if x == v)
        return {"v": v, "teams": [{"team": k, "crest": crests.get(k)} for k in eq]}
    # Match le plus prolifique : à égalité de buts, le plus récent l'emporte.
    top = max(joues, key=lambda r: (r["sh"] + r["sa"],
                                    r["dt"] or datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)))
    lig = next((f for f in feed if f["id"] == top["id"]), None)
    return {
        "matchs": len(joues), "buts": buts,
        "bpm": round(buts / len(joues), 2),
        "journee": meilleure,
        "attaque": extreme(bp, max), "defense": extreme(bc, min),
        "carton": {"home": top["home"], "away": top["away"],
                   "ch": top["ch"], "ca": top["ca"],
                   "sh": top["sh"], "sa": top["sa"], "buts": top["sh"] + top["sa"],
                   "j": top["j"], "date": (lig or {}).get("date", "")},
    }

# ─── Pronos FIGÉS (24 h avant le coup d'envoi) ───────────────────────────────
def load_frozen():
    p = data_path("pronos")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("pronos", {})
    except Exception:
        return {}

def save_frozen(pronos):
    p = data_path("pronos")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"_meta": {
            "role": "Pronostics FIGÉS 24 h avant le coup d'envoi "
                    "(affichage = notation, aucune dérive). Écrit par l1.py.",
            "author": "Nico-Mtn",
            "credit": "Auteur : Nico-Mtn (https://github.com/Nico-Mtn). Réutilisation libre, crédit apprécié."},
            "pronos": pronos}, f, ensure_ascii=False, indent=2)

def grade(pred, sh, sa):
    """exact = score pile ; bon = bonne issue (V/N/D) ; sinon raté."""
    if pred is None or sh is None: return None
    if pred["sh"] == sh and pred["sa"] == sa: return "exact"
    real = "V" if sh > sa else ("N" if sh == sa else "D")
    return "bon" if pred["issue"] == real else "rate"

# ─── Construction du payload ─────────────────────────────────────────────────
MOIS = {1:"janv.",2:"févr.",3:"mars",4:"avr.",5:"mai",6:"juin",
        7:"juil.",8:"août",9:"sept.",10:"oct.",11:"nov.",12:"déc."}

def paris(dt_utc):
    """UTC → heure de Paris (approximation été/hiver suffisante pour l'affichage)."""
    off = 2 if 3 < dt_utc.month < 11 else 1
    return dt_utc + datetime.timedelta(hours=off)

def build(matches_raw, standings_raw, scorers_raw):
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for m in (matches_raw or {}).get("matches", []):
        ht, at = m.get("homeTeam") or {}, m.get("awayTeam") or {}
        if not (ht.get("name") or ht.get("shortName")) or not (at.get("name") or at.get("shortName")):
            continue
        hn, an = disp(ht), disp(at)      # identifiant unique d'équipe, partout le même
        try:
            dt = datetime.datetime.fromisoformat((m.get("utcDate") or "").replace("Z", "+00:00"))
        except Exception:
            dt = None
        ft = ((m.get("score") or {}).get("fullTime") or {})
        # L'API renvoie parfois un HORODATAGE à la place du statut (« 2026-08-28
        # 17:45:00Z » au lieu de « FINISHED ») : neuf matchs joués n'avaient donc
        # jamais été comptés, ce qui figeait la journée courante et le classement.
        # On ne se fie plus au seul statut : un match est joué s'il a un score
        # complet, qu'il n'est plus en cours, et que son coup d'envoi est passé.
        statut_api = (m.get("status") or "").strip().upper()
        en_cours = statut_api in ("IN_PLAY", "PAUSED", "LIVE")
        a_score = ft.get("home") is not None and ft.get("away") is not None
        played = a_score and not en_cours and (dt is None or dt <= now)
        rows.append({
            "id": m.get("id"), "j": m.get("matchday"), "dt": dt,
            "home": hn, "away": an,
            "ch": ht.get("crest"), "ca": at.get("crest"),
            "sh": ft.get("home") if played else None,
            "sa": ft.get("away") if played else None,
            "played": played,
            "live": en_cours,
            "direct": [ft.get("home"), ft.get("away")] if (en_cours and a_score) else None,
        })
    rows.sort(key=lambda r: (r["dt"] or datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)))
    global ELO_START
    teams = sorted({r["home"] for r in rows} | {r["away"] for r in rows})
    ELO_START = compute_elo_start(teams)   # saison N-1 régressée + mercato (figé)
    walk, elo = walk_forward(rows)   # pronos sans fuite pour le passé + Elo à jour

    frozen = load_frozen(); out_frozen = dict(frozen)
    feed, stats = [], {"joue": 0, "exact": 0, "bon": 0, "rate": 0, "total": len(rows)}
    for r in rows:
        key = str(r["id"])
        fr = frozen.get(key)
        pred = None
        if fr and fr.get("home") == r["home"] and fr.get("away") == r["away"]:
            pred = {k: fr[k] for k in ("sh", "sa", "issue", "conf", "proba", "second") if k in fr}
        elif r["played"]:
            pred = walk[key]                                   # prono « d'avant match » (honnête)
        elif r["dt"] and (r["dt"] - datetime.timedelta(hours=FREEZE_LEAD_H)) <= now < r["dt"]:
            pred = predict(elo, r["home"], r["away"], r["id"])  # dans la fenêtre → on FIGE
            rec = dict(pred); rec["home"] = r["home"]; rec["away"] = r["away"]
            rec["frozen_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            out_frozen[key] = rec
        else:
            pred = predict(elo, r["home"], r["away"], r["id"])  # projection (non figée)
        st = grade(pred, r["sh"], r["sa"]) if r["played"] else None
        if st:
            stats["joue"] += 1; stats[st] += 1
        d = paris(r["dt"]) if r["dt"] else None
        feed.append({
            "id": r["id"], "j": r["j"],
            "date": (f"{d.day} {MOIS[d.month]}" if d else ""),
            "heure": (d.strftime("%H:%M") if d else ""),
            "iso": (d.strftime("%Y-%m-%d") if d else ""),
            "sort": (r["dt"].strftime("%Y-%m-%dT%H:%M:%SZ") if r["dt"] else ""),
            "home": r["home"], "away": r["away"], "ch": r["ch"], "ca": r["ca"],
            "prono": [pred["sh"], pred["sa"]] if pred else None,
            "issue": pred.get("issue") if pred else None,
            "conf": pred.get("conf") if pred else None,
            "proba": pred.get("proba") if pred else None,
            "reel": [r["sh"], r["sa"]] if r["played"] else None,
            "second": (pred.get("second") if pred else None),
            "live": r["live"], "direct": r["direct"],
            "statut": st, "fige": key in out_frozen,
        })
    save_frozen(out_frozen)

    # Classement RÉEL (API) ---------------------------------------------------
    table = []
    for blk in (standings_raw or {}).get("standings", []):
        if blk.get("type") != "TOTAL": continue
        for t in blk.get("table", []):
            tm = t.get("team") or {}
            table.append({"pos": t.get("position"), "team": disp(tm),
                          "crest": tm.get("crest"), "j": t.get("playedGames"), "pts": t.get("points"),
                          "g": t.get("won"), "n": t.get("draw"), "p": t.get("lost"),
                          "bp": t.get("goalsFor"), "bc": t.get("goalsAgainst"),
                          "diff": t.get("goalDifference")})
        break

    # Classement PROJETÉ fin de saison (réel acquis + pronos des matchs à venir)
    proj = {}
    def slot(name, crest):
        return proj.setdefault(name, {"team": name, "crest": crest, "pts": 0, "j": 0,
                                      "g": 0, "n": 0, "p": 0, "bp": 0, "bc": 0})
    for r, f in zip(rows, feed):
        sh, sa = (r["sh"], r["sa"]) if r["played"] else (
            (f["prono"][0], f["prono"][1]) if f["prono"] else (None, None))
        if sh is None: continue
        H = slot(f["home"], f["ch"]); A = slot(f["away"], f["ca"])
        H["j"] += 1; A["j"] += 1; H["bp"] += sh; H["bc"] += sa; A["bp"] += sa; A["bc"] += sh
        if sh > sa:   H["pts"] += 3; H["g"] += 1; A["p"] += 1
        elif sh < sa: A["pts"] += 3; A["g"] += 1; H["p"] += 1
        else:         H["pts"] += 1; A["pts"] += 1; H["n"] += 1; A["n"] += 1
    projected = sorted(proj.values(), key=lambda t: (-t["pts"], -(t["bp"] - t["bc"]), -t["bp"]))
    for i, t in enumerate(projected, 1):
        t["pos"] = i; t["diff"] = t["bp"] - t["bc"]

    scorers = []
    for s in (scorers_raw or {}).get("scorers", []):
        p = s.get("player") or {}; t = s.get("team") or {}
        scorers.append({"player": p.get("name"), "team": disp(t),
                        "crest": t.get("crest"), "goals": s.get("goals") or 0,
                        "assists": s.get("assists") or 0})

    # Fiches clubs : API (année de création, stade) + référentiel (capacité, titres,
    # nom d'affichage) + calculs maison (bilan domicile / extérieur).
    api_teams, prev_tbl, ref = fetch_teams(), fetch_prev_table(), clubs_ref()
    bil = bilans(rows)
    crests = {}
    for r in rows:
        crests.setdefault(r["home"], r["ch"]); crests.setdefault(r["away"], r["ca"])
    pos_now = {t["team"]: t for t in table}
    clubs, noms = [], {}
    for key in teams:
        rf = ref.get(key) or {}
        at = api_teams.get(key) or {}
        nom = rf.get("nom") or key
        noms[key] = {"n": nom, "a": rf.get("abbr") or nom}
        cur = pos_now.get(key)
        # Classement de la saison précédente. Un club absent du classement N-1 de
        # l'élite est un PROMU : on va chercher son rang en division inférieure dans
        # le référentiel (Ligue 2 n'est pas couverte par le plan gratuit de l'API).
        pv = prev_tbl.get(key)
        if pv:
            prev = {"pos": pv["pos"], "pts": pv["pts"], "sur": pv["sur"], "promu": False,
                    "division": LG["nom"], "saison": f"{PREV_SEASON}-{PREV_SEASON + 1}"}
        else:
            n1 = rf.get("n1") or {}
            prev = ({"pos": n1["pos"], "pts": n1.get("pts"), "sur": n1.get("sur"),
                     "promu": True, "division": n1.get("division"),
                     "saison": n1.get("saison") or f"{PREV_SEASON}-{PREV_SEASON + 1}"}
                    if n1.get("pos") else None)
        clubs.append({
            "team": key, "nom": nom, "abbr": rf.get("abbr") or nom,
            "crest": crests.get(key) or at.get("crest"),
            "fonde": at.get("fonde"),
            "stade": rf.get("stade") or at.get("venue"),
            "capacite": rf.get("capacite"),
            "titres": rf.get("titres"),
            "prev": prev,
            "dom": (bil.get(key) or {}).get("dom") or {"j": 0, "v": 0, "n": 0, "p": 0},
            "ext": (bil.get(key) or {}).get("ext") or {"j": 0, "v": 0, "n": 0, "p": 0},
            "pos": (cur or {}).get("pos"), "pts": (cur or {}).get("pts"),
        })
    # Ordre alphabétique : on cherche un club par son nom, pas par son rang.
    # Les accents sont neutralisés pour que « Étoile » se range bien entre E et F.
    def alpha(c):
        return unicodedata.normalize("NFKD", c["nom"].lower()).encode("ascii", "ignore")
    clubs.sort(key=alpha)

    jours = sorted({f["j"] for f in feed if f["j"]})
    cur = None
    for j in jours:
        if any(f["j"] == j and not f["reel"] for f in feed): cur = j; break
    return {
        "maj": paris(now).strftime("%d/%m/%Y à %H:%M") + " (Paris)",
        "today": now.strftime("%Y-%m-%d"),
        "saison": LG["libelle"], "nom": LG["nom"], "slug": LG["slug"], "journee": cur, "journees": jours,
        "stats": stats, "matches": feed, "table": table, "dyn": dynamique(rows),
        "sstats": saison_stats(rows, feed, crests),
        "videos": videos_journees(LG, jours),
        "projected": projected, "scorers": scorers, "clubs": clubs, "noms": noms,
        "credit": "Auteur : Nico-Mtn (https://github.com/Nico-Mtn). Projet gratuit, sans pub, sans paris.",
    }

# ─── Rendu HTML (page autonome, codes visuels Pronostix) ─────────────────────
PAGE = """<!DOCTYPE html><html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pronostix — __NOM__ __SAISON__</title>
<meta name="description" content="Pronostics IA gratuits de __NOM__ __SAISON__ : fiches clubs, calendrier, classement, buteurs. Sans publicité, sans paris. Par Nico-Mtn.">
<link rel="preconnect" href="https://flagcdn.com">
<link rel="icon" type="image/png" href="../icon-192.png">
<link rel="apple-touch-icon" href="../icon-192.png">
<meta property="og:title" content="Pronostix — __NOM__ __SAISON__">
<meta property="og:description" content="Pronostics IA gratuits, sans publicité et sans paris.">
<meta property="og:image" content="../logo.png">
<meta name="theme-color" content="#2246c7">
<script>
/* Thème appliqué AVANT le premier rendu : évite le flash blanc en mode sombre.
   « auto » suit le système ; le choix explicite est conservé d'une visite à l'autre. */
(function(){try{
 var p=localStorage.getItem("px-theme")||"auto";
 var d=p==="dark"||(p==="auto"&&window.matchMedia("(prefers-color-scheme:dark)").matches);
 document.documentElement.dataset.theme=d?"dark":"light";
 document.documentElement.dataset.pref=p;
}catch(e){document.documentElement.dataset.theme="light";}})();
</script>
<style>
:root{--bg:#f4f6fb;--fg:#1b2333;--card:#fff;--bd:#e6e9f2;--line:#eef0f6;--soft:#eef1f8;
--mut:#5a6478;--acc:#2246c7;--gold:#e8a20c;--ok:#15803d;--ko:#dc2626;--sh:rgba(34,70,199,.10);
/* --lien = accent utilisé sur du TEXTE. En thème sombre, le bleu de marque n'offre
   pas un contraste suffisant sur fond foncé : on le remplace par une teinte claire. */
--lien:#2246c7;--surok:#fff;--surgold:#3a2a00}
html[data-theme="dark"]{--bg:#0f1420;--fg:#e8ecf5;--card:#161d2e;--bd:#242d42;--line:#242d42;
--soft:#242d42;--mut:#94a0b8;--sh:rgba(0,0,0,.35);--lien:#d8e3ff;--ok:#22c55e;--surok:#06280f}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:0 14px 40px}
header{background:var(--card);border-bottom:1px solid var(--bd);padding:12px 0 10px;position:sticky;top:0;z-index:9}
.hrow{max-width:820px;margin:0 auto;padding:0 14px;display:flex;align-items:center;gap:12px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.brand img{width:38px;height:38px;border-radius:10px;object-fit:cover;flex:none}
h1{margin:0;font-size:19px;font-weight:900}
.tag{color:var(--lien);font-weight:700;font-size:12px}
.sub{font-size:11px;opacity:.6;text-transform:uppercase;letter-spacing:.05em}
/* Fiabilité et compteurs sont des indicateurs de PRONO : ils disparaissent en
   mode Réel, où la page ne parle que de résultats officiels. */
.pct{margin-left:auto;text-align:center;flex:none}
.pct .b{font-size:22px;font-weight:900;color:var(--gold);line-height:1}
.pct .l{font-size:10px;opacity:.6;text-transform:uppercase}
[hidden]{display:none !important}
.theme{flex:none;display:flex;gap:2px;background:var(--soft);border-radius:99px;padding:3px}
.theme button{border:0;background:transparent;color:var(--mut);width:28px;height:26px;border-radius:99px;
cursor:pointer;font-size:13px;line-height:1;padding:0}
.theme button.on{background:var(--card);color:var(--lien);box-shadow:0 1px 3px var(--sh)}
.scorebar{max-width:820px;margin:10px auto 0;padding:0 14px;display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.sc{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:9px 6px;text-align:center}
.sc b{display:block;font-size:17px;font-weight:900}.sc span{font-size:10px;opacity:.6;text-transform:uppercase}
.sc.ok b{color:var(--ok)}.sc.ko b{color:var(--ko)}
.maj{max-width:820px;margin:8px auto 0;padding:0 14px;text-align:center;font-size:11px;opacity:.6}
/* Barre du haut : la compétition à gauche (icônes seules), le mode à droite.
   Les deux décisions les plus structurantes de la page tiennent sur une ligne. */
.toprow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:16px 0 10px}
.compnav{display:flex;gap:6px;flex:none;background:var(--card);border:1px solid var(--bd);
border-radius:14px;padding:4px}
.compnav a{width:42px;height:38px;display:flex;align-items:center;justify-content:center;
border-radius:10px;text-decoration:none;background:transparent;color:var(--mut);
border:1px solid transparent;transition:transform .12s ease}
.compnav a:hover{transform:translateY(-1px);background:var(--soft)}
.compnav a.on{color:#fff;box-shadow:0 2px 8px var(--sh)}
.compnav a.on:hover{background:inherit}
.compnav .ic{font-size:17px;line-height:1}
.compnav .flg{width:24px;height:18px}
.flg{border-radius:3px;object-fit:cover;flex:none;box-shadow:0 0 0 1px rgba(0,0,0,.10)}
/* Sélecteur de mode : interrupteur segmenté avec pastille glissante. C'est le
   geste principal de la page, il doit se voir et se comprendre d'un coup d'œil. */
.modebar{position:relative;display:flex;flex:1 1 250px;background:var(--card);
border:1px solid var(--bd);border-radius:14px;padding:4px}
.modebar .glide{position:absolute;top:4px;left:4px;width:calc(50% - 4px);
height:calc(100% - 8px);border-radius:10px;background:var(--ok);
transition:transform .22s cubic-bezier(.4,0,.2,1),background .22s ease}
.modebar[data-m="prono"] .glide{transform:translateX(100%);background:var(--gold)}
.modebar button{position:relative;z-index:1;flex:1;padding:9px 6px;border:0;border-radius:10px;
background:transparent;font:inherit;font-weight:800;font-size:14px;color:var(--mut);cursor:pointer;
display:flex;align-items:center;justify-content:center;gap:7px;white-space:nowrap;
transition:color .18s ease}
.modebar button .e{font-size:16px;line-height:1}
.modebar button.on[data-m="reel"]{color:var(--surok)}
.modebar button.on[data-m="prono"]{color:var(--surgold)}
@media(prefers-reduced-motion:reduce){.modebar .glide{transition:none}}
.note{text-align:center;font-size:12px;opacity:.7;margin:6px 0 10px}
/* Cloche : l'abonnement ne mérite pas un bandeau permanent en travers de la page.
   Une icône dans l'en-tête, et tout le réglage dans une popup ouverte à la demande. */
.bell{flex:none;position:relative;width:34px;height:32px;border-radius:10px;border:1px solid var(--bd);
background:var(--soft);color:var(--mut);cursor:pointer;font-size:15px;line-height:1;padding:0}
.bell:hover{color:var(--lien);border-color:var(--lien)}
.bell[data-on="1"]::after{content:"";position:absolute;top:5px;right:5px;width:7px;height:7px;
border-radius:50%;background:var(--ok);box-shadow:0 0 0 2px var(--card)}
/* Popup de préférences : même feuille modale que la fiche club, pour ne pas
   introduire un deuxième vocabulaire visuel. */
.npref{padding:2px 0 4px}
.npref .intro{font-size:13px;opacity:.72;line-height:1.55;margin:0 0 14px}
.nrow{display:flex;align-items:center;gap:11px;padding:13px 0}
.nrow+.nrow{border-top:1px solid var(--line)}
.nrow .flg{width:26px;height:20px}
.nrow .nm{flex:1;min-width:0;font-weight:800;font-size:14px}
.nrow .nm span{display:block;font-weight:600;font-size:11px;opacity:.6;margin-top:2px}
/* Interrupteur : cible tactile confortable, état lisible sans couleur seule. */
.sw{flex:none;width:50px;height:29px;border-radius:99px;border:0;cursor:pointer;padding:0;
background:rgba(127,127,127,.32);position:relative;transition:background .2s ease}
.sw::before{content:"";position:absolute;top:3px;left:3px;width:23px;height:23px;border-radius:50%;
background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.35);transition:transform .2s ease}
.sw[aria-checked="true"]{background:var(--ok)}
.sw[aria-checked="true"]::before{transform:translateX(21px)}
.sw[disabled]{opacity:.5;cursor:default}
.npref .aide{font-size:11.5px;opacity:.68;line-height:1.6;margin-top:14px;
padding-top:13px;border-top:1px solid var(--line)}
.npref .ko{color:var(--ko);font-weight:700}
@media(prefers-reduced-motion:reduce){.sw,.sw::before{transition:none}}
nav.tabs{display:flex;gap:8px;margin:10px 0 16px;flex-wrap:wrap}
nav.tabs button{flex:1;min-width:104px;padding:10px;border-radius:12px;border:1px solid var(--bd);
background:var(--card);font-weight:800;font-size:13px;color:var(--mut);cursor:pointer}
nav.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.subnav{display:flex;gap:6px;margin:0 0 14px;overflow-x:auto;-webkit-overflow-scrolling:touch;
padding-bottom:2px}
.subnav button{flex:1 0 auto;white-space:nowrap;padding:8px 12px;border-radius:99px;
border:1px solid var(--bd);background:var(--card);
font-weight:800;font-size:12px;color:var(--mut);cursor:pointer}
.subnav button.on{background:var(--gold);color:#3a2a00;border-color:var(--gold)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:16px;margin-bottom:16px}
.ctitle{display:flex;align-items:center;gap:9px;margin-bottom:14px}
.ctitle .ic{width:30px;height:30px;border-radius:9px;display:flex;align-items:center;justify-content:center;
font-size:15px;background:linear-gradient(135deg,#f6c453,#e8a20c);flex:none}
.ctitle h3{margin:0;font-size:15px;font-weight:800}
.m{display:flex;align-items:center;gap:10px;padding:12px 2px}
.m+.m{border-top:1px solid var(--line)}
.m .t{flex:1;display:flex;align-items:center;gap:7px;min-width:0}
.m .t.a{justify-content:flex-end;text-align:right}
.m .t b{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.m img{width:22px;height:22px;object-fit:contain;flex:none}
.sc2{flex:none;text-align:center;min-width:76px}
.sc2 .v{font-size:17px;font-weight:900}
.sc2 .k{font-size:9px;opacity:.55;text-transform:uppercase;letter-spacing:.04em}
.sc2 .p{color:var(--gold)}
.meta{display:flex;align-items:center;gap:6px;justify-content:center;flex-wrap:wrap;margin-top:5px}
/* En-tête de journée : le badge « J2 » n'est plus répété sur chaque rencontre. */
.jrn{font-size:11px;font-weight:800;opacity:.5;text-transform:uppercase;letter-spacing:.07em;
margin:16px 0 2px;padding-top:12px;border-top:1px solid var(--line)}
.jrn:first-child{margin-top:0;padding-top:0;border-top:0}
/* Journée terminée : dépliant. On garde le balisage natif details/summary, donc
   le pli fonctionne même si le JS n'a pas encore pris la main. */
.jsec+.jsec{border-top:1px solid var(--line)}
.jsec>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;
padding:13px 2px;font-size:11px;font-weight:800;opacity:.62;text-transform:uppercase;
letter-spacing:.07em}
.jsec>summary::-webkit-details-marker{display:none}
.jsec>summary::before{content:"▸";font-size:10px;opacity:.8}
.jsec[open]>summary::before{content:"▾"}
.jsec>summary:hover{opacity:1;color:var(--lien)}
.jsec>summary .cnt{margin-left:auto;font-weight:700;text-transform:none;letter-spacing:0;
font-size:11px;opacity:.9}
.jsec[open]>summary{opacity:.85}
/* Lien vers le résumé vidéo officiel de la journée. Discret par défaut, il prend
   le rouge de la lecture au survol : reconnaissable sans crier dans la page. */
.vid{display:inline-flex;align-items:center;gap:5px;margin-left:10px;flex:none;
padding:3px 9px;border-radius:99px;border:1px solid var(--bd);background:var(--card);
color:var(--mut);text-decoration:none;font-size:10px;font-weight:800;
text-transform:none;letter-spacing:0;white-space:nowrap}
.vid .pl{font-size:9px;color:#e5332a}
.vid:hover{border-color:#e5332a;color:#e5332a}
.jsec>summary .cnt+.vid{margin-left:8px}
.jrn .vid{margin-left:8px;vertical-align:middle}
.ctitle .vid{margin-left:auto}
.sc2 .v.h{font-size:15px;font-weight:800}
/* Date et heure au-dessus du score : quand une rencontre est jouée, savoir QUAND
   compte autant que le résultat, et l'information n'a plus à figurer en badge. */
.sc2 .dt{font-size:10px;font-weight:700;opacity:.55;line-height:1.3;margin-bottom:3px;
white-space:nowrap}
/* Rappel du pronostic sous un score réel ou en direct. */
.sc2 .pr{font-size:11px;font-weight:800;color:var(--gold);margin-top:3px;white-space:nowrap}
/* Fiche club : les résultats passés sont repliés sous un titre de section. */
.jsec.res{margin-top:18px;border-top:1px solid var(--line)}
.jsec.res>summary{padding:14px 0 8px;font-size:14px;font-weight:800;
text-transform:none;letter-spacing:0;opacity:.95}
.jsec.res>summary .ic{width:26px;height:26px;border-radius:8px;display:flex;align-items:center;
justify-content:center;font-size:13px;background:linear-gradient(135deg,#f6c453,#e8a20c);flex:none}
.jsec.res>summary .cnt{opacity:.6}
.sc2 .v.dir{color:var(--ko)}
.b.dir{background:var(--ko);color:#fff}
.b{font-size:10px;font-weight:800;padding:2px 7px;border-radius:99px;background:var(--soft);color:var(--mut)}
.b.ex{background:#dcfce7;color:#166534}.b.bo{background:#fef3c7;color:#92400e}.b.ra{background:#fee2e2;color:#991b1b}
.b.fg{background:#e0e7ff;color:#3730a3}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:10px;opacity:.55;text-transform:uppercase;padding:6px 4px;font-weight:800}
td{padding:8px 4px;border-top:1px solid var(--line)}
td.n{text-align:center;width:26px;font-weight:800}
td.p{text-align:center;font-weight:900}
tr.c1 td.n{color:var(--ok)}tr.c2 td.n{color:var(--lien)}tr.c3 td.n{color:var(--ko)}
.tm{display:flex;align-items:center;gap:8px}.tm img{width:20px;height:20px;object-fit:contain}
.empty{text-align:center;opacity:.6;padding:26px 10px;font-size:14px}
.tn{cursor:pointer;border-bottom:1px dotted rgba(127,127,127,.5)}
.tn:hover{color:var(--lien)}
.alt{margin-top:6px;text-align:center;font-size:11px}
.alt button{border:0;background:var(--soft);color:inherit;font:inherit;font-size:11px;
font-weight:700;padding:3px 10px;border-radius:99px;cursor:pointer}
.alt button:hover{color:var(--lien)}
.alt .v2{margin-left:6px;font-weight:900;color:var(--gold)}
.jsel{display:flex;gap:6px;overflow-x:auto;padding:4px 0 10px;-webkit-overflow-scrolling:touch}
.jsel button{flex:none;padding:7px 13px;border-radius:99px;border:1px solid var(--bd);background:var(--card);
font-weight:800;font-size:12px;color:var(--mut);cursor:pointer}
.jsel button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.fold{width:100%;margin-top:8px;padding:12px;border-radius:12px;border:1px dashed rgba(127,127,127,.35);
background:transparent;color:inherit;font:inherit;font-weight:800;font-size:13px;cursor:pointer;opacity:.75}
.fold:hover{opacity:1;border-color:var(--lien);color:var(--lien)}
.dyn-row{display:flex;align-items:center;gap:10px;padding:11px 2px}
.dyn-row+.dyn-row{border-top:1px solid var(--line)}
.dyn-row .nm{flex:1;font-weight:700}
.serie{display:flex;gap:3px}
.serie i{width:19px;height:19px;border-radius:6px;font-style:normal;font-size:10px;font-weight:900;
display:flex;align-items:center;justify-content:center;color:#fff}
.serie i.V{background:var(--ok)}.serie i.N{background:#a1a1aa}.serie i.D{background:var(--ko)}
.dyn-row .pt{font-weight:900;min-width:52px;text-align:right;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:10px}
.club{display:flex;align-items:center;gap:10px;padding:12px;border-radius:14px;border:1px solid var(--bd);
background:var(--card);cursor:pointer;text-align:left;font:inherit;color:inherit;width:100%}
.club:hover{border-color:var(--lien);box-shadow:0 4px 14px var(--sh)}
.club img{width:30px;height:30px;object-fit:contain;flex:none}
.club .in{min-width:0;flex:1}
.club .nm{display:block;font-weight:800;font-size:13px;line-height:1.25}
.club .ln{display:block;font-size:11px;opacity:.6;margin-top:3px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:9px;margin:14px 0 4px}
.fact{background:var(--soft);border-radius:12px;padding:11px 12px}
.fact .k{font-size:10px;opacity:.65;text-transform:uppercase;letter-spacing:.04em}
.fact .v{font-size:15px;font-weight:900;margin-top:3px}
.fact .x{font-size:11px;opacity:.6;font-weight:600}
.ovl{position:fixed;inset:0;background:rgba(10,14,25,.55);display:flex;align-items:flex-end;
justify-content:center;z-index:50;padding:0}
.sheet{background:var(--card);width:100%;max-width:640px;max-height:86vh;overflow:auto;
border-radius:20px 20px 0 0;padding:20px 18px 28px}
.sheet h3{margin:0 0 4px;font-size:19px;font-weight:900}
.sheet .cls{position:sticky;top:0;float:right;border:0;background:var(--soft);
width:30px;height:30px;border-radius:50%;font-size:16px;cursor:pointer;color:inherit}
.shead{display:flex;align-items:center;gap:12px}
.shead img{width:46px;height:46px;object-fit:contain;flex:none}
footer{text-align:center;font-size:11px;opacity:.55;padding:22px 14px;line-height:1.7}
footer a{color:var(--lien)}
/* Pastilles d'icône : dégradé doré en thème clair, blanc franc en thème sombre.
   Sur fond nuit, le doré perdait tout contraste avec le pictogramme. */
html[data-theme="dark"] .ctitle .ic,
html[data-theme="dark"] .jsec.res>summary .ic{background:#fff}
/* Statistiques de la saison : une grille de cartes lisibles d'un coup d'œil. */
.stg{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:11px}
.st{background:var(--soft);border-radius:14px;padding:13px 14px}
.st .k{font-size:10px;opacity:.65;text-transform:uppercase;letter-spacing:.04em}
.st .v{font-size:22px;font-weight:900;margin-top:4px;line-height:1.15;color:var(--gold)}
.st .x{font-size:11px;opacity:.65;font-weight:600;margin-top:3px;line-height:1.45}
.st .who{display:flex;align-items:center;gap:7px;margin-top:6px;font-size:13px;font-weight:800}
.st .who img{width:20px;height:20px;object-fit:contain;flex:none}
.st .who+.who{margin-top:4px}
/* Le match le plus prolifique est une affiche, pas un chiffre : il prend toute
   la largeur pour que le score respire entre les deux blasons. */
.st.aff{grid-column:1/-1}
.aff .row{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:7px;
flex-wrap:wrap;font-size:14px;font-weight:800}
.aff .row img{width:24px;height:24px;object-fit:contain;flex:none}
.aff .sco{font-size:21px;font-weight:900;color:var(--gold);padding:0 4px;white-space:nowrap}
.aff .x{text-align:center;margin-top:6px}
.lg{display:inline}.sm{display:none}
@media(max-width:560px){.lg{display:none}.sm{display:inline}
.compnav a{width:38px;height:36px}nav.tabs button{min-width:0;flex:1 1 44%}
.modebar button{font-size:13px}}
</style></head><body>
<header>
 <div class="hrow">
  <div class="brand">
   <img src="../logo.png" alt="Pronostix">
   <div><h1>Pronostix</h1><div class="tag">Nono le robot, roi des prono 👑</div>
    <div class="sub">__NOM__ · Saison __SAISON__</div></div>
  </div>
  <div class="pct" id="pct-box" hidden><div class="b" id="pct">—</div><div class="l">Fiabilité</div></div>
  <button class="bell" id="bell" type="button" hidden
   title="Notifications" aria-label="Préférences de notification">🔔</button>
  <div class="theme" id="theme" role="group" aria-label="Thème d'affichage">
   <button data-t="light" title="Thème clair" aria-label="Thème clair">☀</button>
   <button data-t="auto" title="Thème automatique" aria-label="Thème automatique">◐</button>
   <button data-t="dark" title="Thème sombre" aria-label="Thème sombre">☾</button>
  </div>
 </div>
 <div class="scorebar" id="scorebar" hidden></div>
</header>
<div class="wrap">
 <div class="toprow">
  <nav class="compnav" aria-label="Compétition">
   __NAV__
  </nav>
  <div class="modebar" id="modebar" data-m="reel" role="group" aria-label="Mode d'affichage">
   <span class="glide" aria-hidden="true"></span>
   <button data-m="reel" class="on" aria-pressed="true"><span class="e">⚽</span> Réel</button>
   <button data-m="prono" aria-pressed="false"><span class="e">🤖</span> Prono</button>
  </div>
 </div>
 <div class="note" id="note"></div>
 <nav class="tabs" id="tabs">
  <button data-v="clubs">🛡️ Clubs</button>
  <button data-v="feed" class="on">🔥 Live feed</button>
  <button data-v="cal">📅 Calendrier</button>
  <button data-v="clt">📊 Classement</button>
 </nav>
 <div id="content"></div>
</div>
<div class="maj" id="maj"></div>
<footer>
 __NOM__ __SAISON__ · Pronostics générés par modèle IA · Résultats réels via football-data.org<br>
 Créé par <a href="https://github.com/Nico-Mtn">Nico-Mtn</a> · Projet gratuit, sans publicité, sans paris
</footer>
<script>
var DATA = /*__DATA__*/null;
var view="feed", mode="reel", sub="clt";
/* Un abonnement push par COMPÉTITION : le lecteur choisit ce qu'il veut suivre.
   La clé publique VAPID est publique par construction — elle n'a rien d'un secret. */
var PUSH = {
 topic: "__SLUG__",
 comps: /*__COMPS__*/[],
 base: "https://pronobot-push.nicolasmartin-contact.workers.dev",
 cle: "BEqVoqWHGNDWSknn8vx4a65zPx39eqGtEnt9wQ3tVW6z81BPxbdOe5kVdWeApywW27Qrd8NbT0KPUPvjzrUbhYw"
};
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
 return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function logo(u){return u?'<img src="'+esc(u)+'" alt="" loading="lazy">':'';}
/* Nom d'affichage : libellé complet, et abréviation sur les écrans étroits. */
function nm(k){var n=(DATA.noms||{})[k];return n?n.n:k;}
function nmHtml(k){var n=(DATA.noms||{})[k];
 if(!n) return esc(k);
 if(n.a===n.n) return esc(n.n);
 return '<span class="lg">'+esc(n.n)+'</span><span class="sm">'+esc(n.a)+'</span>';}
function num(v){return v==null?"—":String(v).replace(/\\B(?=(\\d{3})+(?!\\d))/g," ");}
/* Ordinal français : 1er, puis 2ᵉ, 3ᵉ… */
function ord(n){return n==1?"1er":(n+"ᵉ");}

/* ─── Thème clair / sombre / auto ─────────────────────────────────────────── */
function applyTheme(p){
 try{localStorage.setItem("px-theme",p);}catch(e){}
 var d=p==="dark"||(p==="auto"&&window.matchMedia("(prefers-color-scheme:dark)").matches);
 document.documentElement.dataset.theme=d?"dark":"light";
 document.documentElement.dataset.pref=p;
 Array.prototype.forEach.call(document.querySelectorAll("#theme button"),function(b){
  b.classList.toggle("on",b.dataset.t===p);});
}
Array.prototype.forEach.call(document.querySelectorAll("#theme button"),function(b){
 b.onclick=function(){applyTheme(b.dataset.t);};});
window.matchMedia("(prefers-color-scheme:dark)").addEventListener("change",function(){
 if(document.documentElement.dataset.pref==="auto") applyTheme("auto");});
applyTheme(document.documentElement.dataset.pref||"auto");

function head(){
 var s=DATA.stats||{},j=s.joue||0,ok=(s.exact||0)+(s.bon||0),pr=(mode==="prono");
 // Fiabilité, exacts, bons, ratés : ces compteurs jugent les pronostics. En mode
 // Réel la page ne montre que des faits officiels, ils n'ont donc rien à y faire.
 document.getElementById("pct-box").hidden = !pr;
 document.getElementById("scorebar").hidden = !pr;
 if(pr){
  document.getElementById("pct").textContent = j?((ok/j*100).toFixed(1)+" %"):"—";
  document.getElementById("scorebar").innerHTML =
   '<div class="sc"><b>'+j+'</b><span>joués</span></div>'
  +'<div class="sc ok"><b>'+(s.exact||0)+'</b><span>exacts</span></div>'
  +'<div class="sc ok"><b>'+(s.bon||0)+'</b><span>bons</span></div>'
  +'<div class="sc ko"><b>'+(s.rate||0)+'</b><span>ratés</span></div>';
 }
 document.getElementById("maj").textContent =
  "Dernière mise à jour : "+DATA.maj+" — "+j+"/"+(s.total||0)+" matchs joués";
 document.getElementById("note").innerHTML = pr
  ? 'Mode <b>Prono</b> : pronostics de Nono, indice de confiance et projections IA (résultats réels inclus).'
  : 'Mode <b>Réel</b> : résultats et classement officiels, sans projection.';
 var mb=document.getElementById("modebar");
 if(mb) mb.dataset.m=mode;
}
/* opt.sansJournee : la ligne est déjà dans une section de journée, inutile de
   répéter le badge « J2 » sur chacune des neuf rencontres. */
function matchRow(m, opt){
 opt = opt || {};
 var right="", dansScore=false;
 // En mode prono, le score affiché est le RÉEL (ou celui en direct) : sans rappel
 // du pronostic, un badge « raté » ne dit pas ce que Nono avait annoncé.
 var rappel = (mode==="prono" && m.prono && (m.reel || m.live))
   ? '<div class="pr">prono '+m.prono[0]+'–'+m.prono[1]+'</div>' : '';
 // Quand la rencontre s'est jouée, la date coiffe le score plutôt que d'être
 // reléguée en badge sous la ligne : on lit « quand » puis « combien ».
 var quand = m.date
   ? '<div class="dt">'+esc(m.date)+(m.heure?" · "+esc(m.heure):"")+'</div>' : '';
 if(m.live && m.direct){
  right=quand+'<div class="v dir">'+m.direct[0]+" – "+m.direct[1]+'</div><div class="k">en direct</div>'+rappel;
  dansScore=true;
 }else if(m.reel){
  right=quand+'<div class="v">'+m.reel[0]+" – "+m.reel[1]+'</div><div class="k">score final</div>'+rappel;
  dansScore=true;
 }else if(mode==="prono"&&m.prono){
  right='<div class="v p">'+m.prono[0]+" – "+m.prono[1]+'</div><div class="k">pronostic</div>';
 }else{
  // Match à venir sans pronostic affiché : plutôt qu'un tiret vide, la case du
  // score porte l'heure et la date. On ne les répète donc plus en dessous.
  right='<div class="v h">'+esc(m.heure||"—")+'</div>'
      + (m.date?'<div class="k">'+esc(m.date)+'</div>':'');
  dansScore=true;
 }
 var b=[];
 if(m.j && !opt.sansJournee) b.push('<span class="b">J'+m.j+'</span>');
 if(m.live) b.push('<span class="b dir">● en direct</span>');
 if(m.date && !dansScore) b.push('<span class="b">'+esc(m.date)+(m.heure?" · "+esc(m.heure):"")+'</span>');
 if(mode==="prono"){
  if(m.statut==="exact") b.push('<span class="b ex">✓ exact</span>');
  else if(m.statut==="bon") b.push('<span class="b bo">✓ bon</span>');
  else if(m.statut==="rate") b.push('<span class="b ra">✗ raté</span>');
  if(m.conf!=null) b.push('<span class="b">Indice : '+m.conf+' %</span>');
  if(!m.reel&&m.fige) b.push('<span class="b fg">prono figé</span>');
 }
 var alt="";
 if(mode==="prono"&&m.second){
  alt='<div class="alt"><button onclick="toggleAlt(this)" data-s="'+m.second.sh+' – '+m.second.sa
   +'" data-c="'+m.second.conf+'">2ᵉ scénario · Indice : '+m.second.conf+' %</button></div>';
 }
 // Sans badge à afficher, on n'émet pas la ligne : un conteneur vide laisserait
 // un blanc inexpliqué sous chaque rencontre terminée du mode Réel.
 var meta = b.length ? '<div class="meta">'+b.join(" ")+'</div>' : '';
 return '<div class="m"><div class="t">'+logo(m.ch)+'<b class="tn" data-team="'+esc(m.home)+'">'+nmHtml(m.home)+'</b></div>'
 +'<div class="sc2">'+right+'</div>'
 +'<div class="t a"><b class="tn" data-team="'+esc(m.away)+'">'+nmHtml(m.away)+'</b>'+logo(m.ca)+'</div></div>'
 +meta+alt;
}
function toggleAlt(btn){
 if(btn.dataset.open==="1"){btn.dataset.open="0";
  btn.innerHTML='2ᵉ scénario · Indice : '+btn.dataset.c+' %';}
 else{btn.dataset.open="1";
  btn.innerHTML='2ᵉ scénario <span class="v2">'+btn.dataset.s+'</span> · Indice : '+btn.dataset.c+' %';}
}

/* ─── Fiche club ──────────────────────────────────────────────────────────── */
function clubOf(key){
 var c=(DATA.clubs||[]).filter(function(x){return x.team===key;});
 return c.length?c[0]:null;
}
function fact(k,v,x){
 return '<div class="fact"><div class="k">'+esc(k)+'</div><div class="v">'+v
  +(x?' <span class="x">'+esc(x)+'</span>':'')+'</div></div>';
}
function openTeam(name){
 var c=clubOf(name)||{};
 var ms=(DATA.matches||[]).filter(function(m){return m.home===name||m.away===name;});
 var joues=ms.filter(function(m){return m.reel;}), avenir=ms.filter(function(m){return !m.reel;});
 var d=(DATA.dyn||[]).filter(function(x){return x.team===name;})[0];
 var h='<button class="cls" onclick="closeTeam()">×</button>'
  +'<div class="shead">'+(c.crest?'<img src="'+esc(c.crest)+'" alt="">':'')
  +'<div><h3>'+esc(c.nom||name)+'</h3>'
  +'<div class="sub">'+(c.pos?(ord(c.pos)+" du classement · "+(c.pts||0)+" pts"):"Saison en cours")+'</div>'
  +'</div></div>';
 h+='<div class="facts">'
  +fact("Fondé en", c.fonde||"—")
  +fact("Titres de champion", c.titres==null?"—":c.titres)
  +fact("Saison "+(c.prev?c.prev.saison:"précédente"),
        c.prev?ord(c.prev.pos):"—",
        c.prev?("de "+esc(c.prev.division||"")+(c.prev.pts!=null?" · "+c.prev.pts+" pts":"")
                +(c.prev.promu?" · promu":"")):"donnée manquante")
  +fact("Stade", '<span style="font-size:13px">'+esc(c.stade||"—")+'</span>')
  +fact("Capacité", num(c.capacite), c.capacite?"places":"")
  +fact("À domicile", (c.dom?c.dom.v:0)+" / "+(c.dom?c.dom.j:0), "victoires / matchs")
  +fact("À l'extérieur", (c.ext?c.ext.v:0)+" / "+(c.ext?c.ext.j:0), "victoires / matchs")
  +'</div>';
 if(c.capacite==null||c.titres==null||!c.fonde){
  h+='<div class="maj" style="text-align:left;margin:2px 0 0">Les champs affichant « — » ne sont pas '
   +'encore renseignés : ils viennent de data/clubs_ref.json, complété à la main.</div>';
 }
 if(d){h+='<div class="ctitle" style="margin:18px 0 8px"><span class="ic">📈</span>'
  +'<h3 style="font-size:14px">Dynamique</h3></div>'
  +'<div class="serie">'+d.serie.map(function(r){return '<i class="'+r+'">'+r+'</i>';}).join("")
  +'</div><div style="font-size:12px;opacity:.7;margin-top:6px">'+d.pts+' pts sur '+d.sur
  +' · différence '+(d.diff>0?"+":"")+d.diff+' sur 5 matchs</div>';}
 // Ce qui arrive d'abord : on ouvre une fiche club surtout pour savoir qui il
 // affronte ensuite. Le passé reste accessible, replié, juste en dessous.
 h+='<div class="ctitle" style="margin:18px 0 8px"><span class="ic">🔜</span><h3 style="font-size:14px">Prochains matchs</h3></div>';
 h+= avenir.length ? avenir.slice(0,5).map(function(m){return matchRow(m);}).join("")
                   : '<div class="empty">Aucun match à venir.</div>';
 var derniers = joues.slice(-5).reverse();
 h+='<details class="jsec res"><summary><span class="ic">✅</span>'
  +'<span class="ttl">Derniers résultats</span>'
  +'<span class="cnt">'+(derniers.length ? derniers.length+(derniers.length>1?' matchs':' match') : 'aucun')
  +'</span></summary>'
  + (derniers.length ? derniers.map(function(m){return matchRow(m);}).join("")
                     : '<div class="empty">Aucun match joué.</div>')
  +'</details>';
 closeTeam();   // un seul calque à la fois
 var o=document.createElement("div");o.className="ovl";o.id="ovl";
 o.onclick=function(e){if(e.target===o)closeTeam();};
 o.innerHTML='<div class="sheet">'+h+'</div>';
 document.body.appendChild(o);
}
function closeTeam(){var o=document.getElementById("ovl");if(o)o.remove();}
function clubsHtml(){
 var cs=DATA.clubs||[];
 if(!cs.length) return '<div class="card"><div class="empty">Les fiches clubs arrivent avec le calendrier.</div></div>';
 var h='<div class="card"><div class="ctitle"><span class="ic">🛡️</span><h3>Les '+cs.length
  +' clubs de la saison</h3></div><div class="grid">';
 cs.forEach(function(c){
  var l=c.pos?(ord(c.pos)+" · "+(c.pts||0)+" pts"):(c.stade||"");
  h+='<button class="club" data-team="'+esc(c.team)+'">'+logo(c.crest)
   +'<span class="in"><span class="nm">'+esc(c.nom)+'</span><span class="ln">'+esc(l)+'</span></span></button>';
 });
 return h+'</div><div class="maj" style="margin-top:12px">Touchez un club pour ouvrir sa fiche : '
  +'identité, stade, bilan à domicile et à l\\'extérieur, derniers et prochains matchs.</div></div>';
}
/* Mémorise quelles journées terminées sont dépliées, pour que l'état survive à
   un changement d'onglet ou de mode. */
var feedOuvert={};
/* Regroupe une liste de matchs par journée, en conservant l'ordre reçu.
   opt.pliable : chaque journée devient un dépliant (la première est ouverte). */
/* Résumé vidéo officiel d'une journée. Affiché seulement si au moins un match a
   été joué : avant le coup d'envoi, la vidéo n'existe pas encore.
   stopPropagation sinon le clic replierait aussi la section qui contient le lien. */
function videoLien(j, ms){
 var u=(DATA.videos||{})[j];
 if(!u || !ms.some(function(m){return m.reel;})) return "";
 return '<a class="vid" href="'+esc(u)+'" target="_blank" rel="noopener"'
  +' onclick="event.stopPropagation()" title="Résumé vidéo officiel de la journée '+j+'">'
  +'<span class="pl">▶</span>Résumé</a>';
}
function sections(ms, opt){
 opt = opt || {};
 var ordre=[], par={};
 ms.forEach(function(m){var j=m.j||0; if(!par[j]){par[j]=[];ordre.push(j);} par[j].push(m);});
 return ordre.map(function(j, i){
  var lignes=par[j].map(function(m){return matchRow(m,{sansJournee:true});}).join("");
  var vid=videoLien(j, par[j]);
  if(!opt.pliable) return '<div class="jrn">Journée '+j+vid+'</div>'+lignes;
  if(feedOuvert[j]===undefined) feedOuvert[j] = (i===0);
  var n=par[j].length;
  var reussis=par[j].filter(function(m){return m.statut==="exact"||m.statut==="bon";}).length;
  var compte = (mode==="prono") ? (reussis+'/'+n+' réussis') : (n+(n>1?' matchs':' match'));
  return '<details class="jsec"'+(feedOuvert[j]?' open':'')
   +' ontoggle="feedOuvert['+j+']=this.open">'
   +'<summary>Journée '+j+'<span class="cnt">'+compte+'</span>'+vid+'</summary>'
   +lignes+'</details>';
 }).join("");
}
function feedHtml(){
 var ms=(DATA.matches||[]).slice();
 var direct=ms.filter(function(m){return m.live;});
 // Journées ENTIÈRES : un plafond en nombre de matchs coupait la journée la plus
 // ancienne en plein milieu, et le dépliant annonçait « 8 matchs » sur une
 // journée qui en compte 9.
 var past=[], vues=[];
 ms.filter(function(m){return m.reel;}).reverse().forEach(function(m){
  var j=m.j||0;
  if(vues.indexOf(j)<0){ if(vues.length>=3) return; vues.push(j); }
  past.push(m);
 });
 var next=ms.filter(function(m){return !m.reel && !m.live;}).slice(0,10);
 var h="";
 if(!ms.length) return '<div class="card"><div class="empty">Calendrier bientôt disponible.</div></div>';
 if(direct.length){
  h+='<div class="card"><div class="ctitle"><span class="ic">🔴</span><h3>En ce moment</h3></div>'
   + sections(direct) + '</div>';
 }
 if(next.length){
  h+='<div class="card"><div class="ctitle"><span class="ic">🔜</span><h3>Prochains matchs</h3></div>'
   + sections(next) + '</div>';
 }
 if(past.length){
  h+='<div class="card"><div class="ctitle"><span class="ic">✅</span><h3>Derniers résultats</h3></div>'
   + sections(past,{pliable:true}) + '</div>';
 }
 return h;
}
var calJ=null, calPast=false;
function calGroups(){
 var by={},order=[];
 (DATA.matches||[]).forEach(function(m){var k=m.j||0;if(!by[k]){by[k]=[];order.push(k);}by[k].push(m);});
 order.sort(function(a,b){return a-b;});
 return {by:by,order:order};
}
function journeeCourante(g){
 for(var i=0;i<g.order.length;i++){
  var j=g.order[i];
  if(g.by[j].some(function(m){return !m.reel;})) return j;
 }
 return g.order[g.order.length-1];
}
function bloc(g,j){
 var h='<div class="card"><div class="ctitle"><span class="ic">'+j+'</span><h3>Journée '+j+'</h3>'
  +videoLien(j, g.by[j])+'</div>';
 g.by[j].forEach(function(m){h+=matchRow(m,{sansJournee:true});});
 return h+'</div>';
}
function calHtml(){
 var g=calGroups();
 if(!g.order.length) return '<div class="card"><div class="empty">Calendrier bientôt disponible.</div></div>';
 var cur=journeeCourante(g);
 if(calJ===null) calJ=cur;
 var sel='<div class="jsel">'+g.order.map(function(j){
   return '<button class="'+(j===calJ?"on":"")+'" onclick="calJ='+j+';render();">J'+j+'</button>';
 }).join("")+'</div>';
 var suivantes=g.order.filter(function(j){return j>=calJ;});
 var passees=g.order.filter(function(j){return j<calJ;});
 var h=sel+suivantes.map(function(j){return bloc(g,j);}).join("");
 if(passees.length){
  h+='<button class="fold" onclick="calPast=!calPast;render();">'
   +(calPast?"▲ Masquer les journées précédentes":"▼ Journées précédentes ("+passees.length+")")+'</button>';
  if(calPast) h+=passees.slice().reverse().map(function(j){return bloc(g,j);}).join("");
 }
 return h;
}
function dynHtml(){
 var d=DATA.dyn||[];
 if(!d.length) return '<div class="card"><div class="empty">La dynamique apparaîtra dès les premiers matchs.</div></div>';
 var h='<div class="card"><div class="ctitle"><span class="ic">📈</span><h3>Dynamique sur les 5 derniers matchs</h3></div>';
 d.forEach(function(x){
  h+='<div class="dyn-row"><span class="nm tn" data-team="'+esc(x.team)+'">'+nmHtml(x.team)+'</span>'
   +'<span class="serie">'+x.serie.map(function(r){return '<i class="'+r+'">'+r+'</i>';}).join("")+'</span>'
   +'<span class="pt">'+x.pts+'/'+x.sur+' pts</span></div>';
 });
 return h+'<div class="maj" style="margin-top:10px">Forme récente : points pris et série sur les 5 dernières rencontres.</div></div>';
}
function tableHtml(){
 var rows=(mode==="prono")?(DATA.projected||[]):(DATA.table||[]);
 if(!rows.length) return '<div class="card"><div class="empty">Le classement s\\'affichera dès la première journée.</div></div>';
 var t='<div class="card"><div class="ctitle"><span class="ic">📊</span><h3>'
 +(mode==="prono"?"Classement projeté en fin de saison":"Classement officiel")+'</h3></div>'
 +'<table><tr><th></th><th>Équipe</th><th>J</th><th>G</th><th>N</th><th>P</th><th>Diff</th><th>Pts</th></tr>';
 rows.forEach(function(r){
  var c=r.pos<=3?"c1":(r.pos<=6?"c2":(r.pos>=rows.length-2?"c3":""));
  t+='<tr class="'+c+'"><td class="n">'+r.pos+'</td><td><div class="tm">'+logo(r.crest)
  +'<span class="tn" data-team="'+esc(r.team)+'">'+nmHtml(r.team)+'</span></div></td>'
  +'<td class="n">'+(r.j||0)+'</td><td class="n">'+(r.g||0)+'</td><td class="n">'+(r.n||0)+'</td>'
  +'<td class="n">'+(r.p||0)+'</td><td class="n">'+((r.diff>0?"+":"")+(r.diff||0))+'</td>'
  +'<td class="p">'+(r.pts||0)+'</td></tr>';
 });
 t+='</table>';
 if(mode==="prono") t+='<div class="maj" style="margin-top:10px">Projection : résultats acquis + pronostics des matchs à venir.</div>';
 return t+'</div>';
}
function scorersHtml(){
 var s=DATA.scorers||[];
 if(!s.length) return '<div class="card"><div class="empty">Les buteurs apparaîtront dès les premiers matchs.</div></div>';
 var mx=s[0].goals||1,h='<div class="card"><div class="ctitle"><span class="ic">⚽</span><h3>Meilleurs buteurs</h3></div><table>';
 s.forEach(function(p,i){
  h+='<tr><td class="n">'+(i+1)+'</td><td><div class="tm">'+logo(p.crest)+'<b>'+esc(p.player)+'</b></div>'
  +'<div style="height:6px;border-radius:4px;background:var(--soft);margin-top:5px;overflow:hidden">'
  +'<span style="display:block;height:100%;width:'+Math.round((p.goals||0)/mx*100)+'%;'
  +'background:linear-gradient(90deg,#f6c453,#e8a20c)"></span></div></td>'
  +'<td style="opacity:.6;font-size:11px"><span class="tn" data-team="'+esc(p.team)+'">'+nmHtml(p.team)+'</span></td>'
  +'<td class="p">'+(p.goals||0)+'</td></tr>';
 });
 return h+'</table></div>';
}
/* Statistiques de la saison en cours. Uniquement des faits mesurés sur les
   rencontres jouées : rien de pronostiqué, la carte vaut dans les deux modes. */
function equipes(e){
 if(!e||!e.teams||!e.teams.length) return "";
 return e.teams.map(function(t){
  return '<div class="who">'+logo(t.crest)
   +'<span class="tn" data-team="'+esc(t.team)+'">'+nmHtml(t.team)+'</span></div>';
 }).join("");
}
function statsHtml(){
 var s=DATA.sstats;
 if(!s) return '<div class="card"><div class="empty">Les statistiques apparaîtront dès les premiers matchs.</div></div>';
 var h='<div class="card"><div class="ctitle"><span class="ic">📐</span>'
  +'<h3>Statistiques de la saison</h3></div><div class="stg">';
 h+='<div class="st"><div class="k">Buts par match</div><div class="v">'+s.bpm.toFixed(2).replace(".",",")
  +'</div><div class="x">'+s.buts+' buts en '+s.matchs+' match'+(s.matchs>1?"s":"")+'</div></div>';
 if(s.journee){
  h+='<div class="st"><div class="k">Meilleure journée</div><div class="v">J'+s.journee.j
   +'</div><div class="x">'+s.journee.ratio.toFixed(2).replace(".",",")+' but'
   +(s.journee.ratio>=2?"s":"")+' par match · '+s.journee.buts+' buts en '
   +s.journee.matchs+' rencontres</div></div>';
 }
 if(s.attaque){
  h+='<div class="st"><div class="k">Meilleure attaque</div><div class="v">'+s.attaque.v
   +' buts</div>'+equipes(s.attaque)+'</div>';
 }
 if(s.defense){
  h+='<div class="st"><div class="k">Meilleure défense</div><div class="v">'+s.defense.v
   +' encaissé'+(s.defense.v>1?"s":"")+'</div>'+equipes(s.defense)+'</div>';
 }
 var c=s.carton;
 if(c){
  h+='<div class="st aff"><div class="k">Match le plus prolifique</div>'
   +'<div class="row">'+logo(c.ch)
   +'<span class="tn" data-team="'+esc(c.home)+'">'+nmHtml(c.home)+'</span>'
   +'<span class="sco">'+c.sh+' – '+c.sa+'</span>'
   +'<span class="tn" data-team="'+esc(c.away)+'">'+nmHtml(c.away)+'</span>'+logo(c.ca)+'</div>'
   +'<div class="x">'+c.buts+' buts'+(c.j?' · J'+c.j:"")+(c.date?' · '+esc(c.date):"")+'</div></div>';
 }
 return h+'</div><div class="maj" style="margin-top:12px;text-align:left">Calculé sur les '
  +s.matchs+' rencontres déjà disputées, hors pronostics.</div></div>';
}
/* L'onglet Classement regroupe les lectures d'une même réalité : le classement,
   la forme récente, les buteurs et les statistiques de la saison. */
function cltHtml(){
 var s='<div class="subnav">'
  +'<button data-s="clt" class="'+(sub==="clt"?"on":"")+'">📊 Classement</button>'
  +'<button data-s="dyn" class="'+(sub==="dyn"?"on":"")+'">📈 Dynamique</button>'
  +'<button data-s="but" class="'+(sub==="but"?"on":"")+'">⚽ Buteurs</button>'
  +'<button data-s="sta" class="'+(sub==="sta"?"on":"")+'">📐 Stats</button></div>';
 var v=(sub==="dyn")?dynHtml():(sub==="but")?scorersHtml():(sub==="sta")?statsHtml():tableHtml();
 return s+v;
}
function render(){
 head();
 Array.prototype.forEach.call(document.querySelectorAll("#tabs button"),function(b){
  b.classList.toggle("on",b.dataset.v===view);});
 Array.prototype.forEach.call(document.querySelectorAll(".modebar button"),function(b){
  var on=b.dataset.m===mode;
  b.classList.toggle("on",on); b.setAttribute("aria-pressed",on?"true":"false");});
 var c=document.getElementById("content");
 c.innerHTML = view==="clubs"?clubsHtml():(view==="feed"?feedHtml():(view==="cal"?calHtml():cltHtml()));
 Array.prototype.forEach.call(document.querySelectorAll(".subnav button"),function(b){
  b.onclick=function(){sub=b.dataset.s;render();};});
}
document.addEventListener("click",function(e){
 var el=e.target.closest?e.target.closest("[data-team]"):null;
 if(el&&el.dataset.team){openTeam(el.dataset.team);}
});

/* ─── Notifications de journée ─────────────────────────────────────────────
   Le service worker vit à la RACINE du site : un seul scope pour toutes les
   compétitions, donc un seul abonnement par appareil, portant la liste des
   championnats suivis. */
function b64(s){
 var p="=".repeat((4-s.length%4)%4), b=atob((s+p).replace(/-/g,"+").replace(/_/g,"/"));
 return Uint8Array.from(b, function(c){return c.charCodeAt(0);});
}
/* Racine du site, quelle que soit la page : /pronostix/ligue-1-france/ -> /pronostix/
   C'est le scope du service worker, donc celui de l'abonnement push. */
function racine(){
 var p=location.pathname;
 if(p.charAt(p.length-1)!=="/") p=p.slice(0,p.lastIndexOf("/")+1);
 return p.replace(/[^/]+\/$/,"");
}
var bell=document.getElementById("bell");
/* Topics suivis par CE navigateur. Mémorisés après chaque appel au Worker pour
   que la popup s'ouvre déjà à jour, sans attendre le réseau. */
var suivis=[], prefsGen=0;
function pushDispo(){
 return ("serviceWorker" in navigator) && ("PushManager" in window) && ("Notification" in window);
}
async function reg(){
 if(!pushDispo()) return null;
 return navigator.serviceWorker.register(racine()+"sw.js", {scope: racine()});
}
async function lireTopics(){
 var r=await reg(); if(!r) return [];
 await navigator.serviceWorker.ready;
 var s=await r.pushManager.getSubscription();
 if(!s) return [];
 var t=await fetch(PUSH.base+"/topics?endpoint="+encodeURIComponent(s.endpoint))
       .then(function(x){return x.json();}).catch(function(){return {topics:[]};});
 return t.topics||[];
}
function majCloche(){
 if(!bell) return;
 bell.hidden = !pushDispo();
 bell.dataset.on = suivis.length ? "1" : "0";
 bell.title = suivis.length
   ? "Notifications actives ("+suivis.length+")" : "Notifications";
}
/* iOS n'autorise le push que depuis une application ajoutée à l'écran d'accueil :
   le dire franchement évite un bouton qui semble cassé. */
function iosNonInstalle(){
 var ios=/iPad|iPhone|iPod/.test(navigator.userAgent)
      || (navigator.platform==="MacIntel" && navigator.maxTouchPoints>1);
 return ios && !window.matchMedia("(display-mode: standalone)").matches && !navigator.standalone;
}
function prefsHtml(){
 var refuse = (window.Notification && Notification.permission==="denied");
 var h='<div class="npref"><p class="intro">Choisissez les championnats à suivre. '
  +'Pour chacun : les affiches <b>1 h avant le coup d\\'envoi</b>, et le bilan '
  +'<b>le lendemain à 9 h</b>.</p>';
 PUSH.comps.forEach(function(c){
  var on=suivis.indexOf(c.slug)>=0;
  h+='<div class="nrow">'
   +'<img class="flg" src="https://flagcdn.com/w40/'+esc(c.flag)+'.png" width="26" height="20" alt="">'
   +'<div class="nm">'+esc(c.nom)+'<span>'+(on?"Notifications activées":"Désactivées")+'</span></div>'
   +'<button class="sw" role="switch" data-slug="'+esc(c.slug)+'" '
   +'aria-checked="'+(on?"true":"false")+'" '
   +'aria-label="Notifications '+esc(c.nom)+'"'+(refuse?" disabled":"")+'></button></div>';
 });
 if(refuse){
  h+='<p class="aide"><span class="ko">Les notifications sont bloquées</span> pour ce site '
   +'dans les réglages du navigateur. Réautorisez-les pour utiliser ces interrupteurs.</p>';
 }else if(iosNonInstalle()){
  h+='<p class="aide">Sur iPhone et iPad, les notifications ne fonctionnent qu\\'une fois '
   +'Pronostix ajouté à l\\'écran d\\'accueil : bouton <b>Partager</b>, puis '
   +'<b>Sur l\\'écran d\\'accueil</b>. Rouvrez ensuite le site depuis cette icône.</p>';
 }else{
  h+='<p class="aide">Sur ordinateur, ce sont les notifications du navigateur. '
   +'Sur mobile, elles arrivent comme n\\'importe quelle notification du téléphone, '
   +'même site fermé.</p>';
 }
 return h+'</div>';
}
function ouvrirPrefs(){
 fermerPrefs();   // la fiche club et la popup partagent le même calque
 var o=document.createElement("div");
 o.className="ovl"; o.id="ovl";
 o.innerHTML='<div class="sheet" role="dialog" aria-modal="true" aria-label="Préférences de notification">'
  +'<button class="cls" onclick="fermerPrefs()" aria-label="Fermer">×</button>'
  +'<h3>🔔 Notifications</h3>'+prefsHtml()+'</div>';
 o.addEventListener("click",function(e){ if(e.target===o) fermerPrefs(); });
 document.body.appendChild(o);
 brancherSwitches();
 // Rafraîchit depuis le Worker : l'abonnement a pu changer sur un autre onglet.
 // Le compteur protège d'une course : si le lecteur a basculé un interrupteur
 // pendant que la requête était en vol, la réponse est périmée, on l'ignore.
 var gen = ++prefsGen;
 lireTopics().then(function(t){
  if(gen!==prefsGen) return;
  suivis=t; majCloche();
  var s=document.querySelector("#ovl .npref");
  if(s){ s.outerHTML=prefsHtml(); brancherSwitches(); }
 }).catch(function(){});
}
function fermerPrefs(){var o=document.getElementById("ovl");if(o)o.remove();}
/* Échap ferme le calque ouvert, quel qu'il soit : fiche club ou préférences. */
document.addEventListener("keydown",function(e){
 if(e.key==="Escape") fermerPrefs();
});
function brancherSwitches(){
 Array.prototype.forEach.call(document.querySelectorAll("#ovl .sw"),function(b){
  b.onclick=function(){ basculer(b); };
 });
}
function etatLigne(b, on, texte){
 b.setAttribute("aria-checked", on?"true":"false");
 var l=b.parentElement.querySelector(".nm span");
 if(l) l.textContent = texte || (on?"Notifications activées":"Désactivées");
}
async function basculer(b){
 var slug=b.dataset.slug, on=b.getAttribute("aria-checked")==="true";
 prefsGen++;   // périme tout rafraîchissement réseau encore en vol
 b.disabled=true; etatLigne(b, on, on?"Désactivation…":"Activation…");
 try{
  var r=await reg(); if(!r) throw new Error("push indisponible");
  await navigator.serviceWorker.ready;
  var s=await r.pushManager.getSubscription();
  if(on){
   if(s) await fetch(PUSH.base+"/unsubscribe",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({endpoint:s.endpoint, topics:[slug]})});
   suivis=suivis.filter(function(x){return x!==slug;});
   etatLigne(b,false);
  }else{
   if(Notification.permission!=="granted"){
    var p=await Notification.requestPermission();
    if(p!=="granted"){ etatLigne(b,false,"Refusé par le navigateur"); b.disabled=false; return; }
   }
   if(!s) s=await r.pushManager.subscribe({userVisibleOnly:true,
        applicationServerKey:b64(PUSH.cle)});
   var rep=await fetch(PUSH.base+"/subscribe",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({subscription:s.toJSON(), topics:[slug]})});
   if(!rep.ok) throw new Error("worker "+rep.status);
   if(suivis.indexOf(slug)<0) suivis.push(slug);
   etatLigne(b,true);
  }
  majCloche();
 }catch(e){ etatLigne(b, on, "Échec — réessayer"); }
 b.disabled=false;
}
if(bell){
 bell.onclick=ouvrirPrefs;
 majCloche();
 if(pushDispo()) lireTopics().then(function(t){ suivis=t; majCloche(); }).catch(function(){});
}

/* Lien profond « #cal-J3 » : une notification ouvre directement sa journée. */
(function(){
 var m=/^#cal-J(\d+)$/.exec(location.hash||"");
 if(m){ view="cal"; calJ=parseInt(m[1],10); }
})();
Array.prototype.forEach.call(document.querySelectorAll("#tabs button"),function(b){
 b.onclick=function(){view=b.dataset.v;render();};});
Array.prototype.forEach.call(document.querySelectorAll(".modebar button"),function(b){
 b.onclick=function(){mode=b.dataset.m;render();};});
render();
</script></body></html>"""

def build_league(lg):
    """Génère la page complète d'UN championnat."""
    set_league(lg)
    os.makedirs(OUTDIR, exist_ok=True)
    m, s, b = fetch_all()
    payload = build(m, s, b)
    # La popup de notifications règle TOUTES les compétitions, pas seulement celle
    # de la page ouverte : on lui passe donc la liste complète.
    comps = json.dumps([{"slug": x["slug"], "nom": x["nom"], "flag": x["flag"]}
                        for x in LEAGUES], ensure_ascii=False)
    html = (PAGE.replace("__NAV__", nav_html(lg["slug"]))
                .replace("/*__COMPS__*/[]", comps)
                .replace("__SLUG__", lg["slug"])
                .replace("__NOM__", lg["nom"])
                .replace("__SAISON__", payload["saison"])
                .replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False)))
    with open(os.path.join(OUTDIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(OUTDIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    sig_src = {k: v for k, v in payload.items() if k != "maj"}
    sig = hashlib.md5(json.dumps(sig_src, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    with open(os.path.join(OUTDIR, "content.sig"), "w", encoding="utf-8") as f:
        f.write(sig)
    st = payload["stats"]
    print(f"[OK] {lg['slug']} — {len(payload['matches'])} matchs | "
          f"{st['joue']} joués : {st['exact']} exacts, {st['bon']} bons, {st['rate']} ratés | "
          f"{len(payload['clubs'])} clubs")

def main():
    for lg in LEAGUES:
        try:
            build_league(lg)
        except Exception as e:
            # Un championnat en échec (API, données) ne doit pas empêcher les autres.
            print(f"[ERREUR] {lg['slug']} : {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
