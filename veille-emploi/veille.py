"""Veille emploi : France Travail -> notation selon le profil -> Telegram."""
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

try:
    import feedparser
except ImportError:  # les flux RSS sont facultatifs
    feedparser = None

BASE = Path(__file__).parent
SEEN_FILE = BASE / "seen.json"
PROFIL = yaml.safe_load((BASE / "profil.yaml").read_text(encoding="utf-8"))

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
FT_ID = os.environ["FT_CLIENT_ID"]
FT_SECRET = os.environ["FT_CLIENT_SECRET"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

FT_TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
FT_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
LOOKBACK_HOURS = 24  # fenêtre de recherche ; les doublons sont filtrés par seen.json


# ---------- France Travail ----------
def ft_token() -> str:
    r = requests.post(FT_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": FT_ID,
        "client_secret": FT_SECRET,
        "scope": "api_offresdemploiv2 o2dsoffre",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def ft_search(token: str, mots: str, departement: str) -> list[dict]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "motsCles": mots,
        "departement": departement,
        "minCreationDate": (now - timedelta(hours=LOOKBACK_HOURS)).strftime(fmt),
        "maxCreationDate": now.strftime(fmt),
        "range": "0-149",
        "sort": "1",  # plus récentes d'abord
    }
    if PROFIL.get("types_contrat"):
        params["typeContrat"] = ",".join(PROFIL["types_contrat"])
    if PROFIL.get("alternance_uniquement"):
        params["natureContrat"] = "E2,FS"
    r = requests.get(FT_SEARCH_URL, params=params,
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 204:
        return []
    r.raise_for_status()
    return r.json().get("resultats", [])


# ---------- Notation ----------
def norm(texte: str) -> str:
    """minuscules, sans accents, apostrophes unifiées : « Chargée d’études » -> « chargee d'etudes »"""
    texte = texte.replace("\u2019", "'").replace("\u2018", "'").lower()
    return "".join(c for c in unicodedata.normalize("NFD", texte)
                   if unicodedata.category(c) != "Mn")


def contient(texte: str, mot: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(norm(mot))}(?!\w)", texte) is not None


def contient_sigle(texte_brut: str, sigle: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(sigle)}(?![\w-])", texte_brut) is not None


def texte_offre(offre: dict) -> str:
    lieu = (offre.get("lieuTravail") or {}).get("libelle", "")
    ent = (offre.get("entreprise") or {}).get("nom", "")
    return " ".join([offre.get("intitule", ""), ent, lieu, offre.get("description", "")])


def score_mots_cles(offre: dict, source_rss: bool = False) -> tuple[int, list[str]]:
    titre = norm(offre.get("intitule", ""))
    brut = texte_offre(offre)
    texte = norm(brut)
    if any(contient(titre, m) for m in PROFIL.get("mots_exclus", [])):
        return -1, []
    obligatoires = PROFIL.get("mots_obligatoires", [])
    if obligatoires and not any(contient(texte, m) for m in obligatoires):
        return -1, []
    lieux = [m for m in PROFIL.get("lieux", []) if contient(texte, m)]
    if source_rss and PROFIL.get("lieu_obligatoire_rss") and not lieux:
        return -1, []
    score, trouves = 0, []
    for m in PROFIL.get("mots_cles_forts", []):
        if contient(texte, m):
            score += 3
            trouves.append(m)
    for m in PROFIL.get("mots_cles_bonus", []):
        if contient(texte, m):
            score += 1
            trouves.append(m)
    for m in PROFIL.get("sigles", []):
        if contient_sigle(brut, m):
            score += 1
            trouves.append(m)
    if lieux:
        score += 1
        trouves.append(lieux[0])
    return score, trouves


# ---------- Flux RSS ----------
def lire_flux() -> list[dict]:
    if not feedparser or not PROFIL.get("flux_rss"):
        return []
    offres = []
    for url in PROFIL["flux_rss"]:
        try:
            flux = feedparser.parse(url, agent="Mozilla/5.0 veille-emploi")
            nom = flux.feed.get("title", url)
            for e in flux.entries[:50]:
                lien = e.get("link", "")
                if not lien:
                    continue
                resume = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
                offres.append({
                    "id": "rss:" + lien,
                    "intitule": html.unescape(e.get("title", "Offre")),
                    "description": html.unescape(resume),
                    "entreprise": {"nom": nom},
                    "typeContratLibelle": "Flux RSS",
                    "origineOffre": {"urlOrigine": lien},
                    "_rss": True,
                })
        except Exception as ex:  # une source en panne ne bloque pas les autres
            print(f"Flux ignoré {url}: {ex}", file=sys.stderr)
    return offres


def score_ia(offre: dict) -> tuple[int, str] | None:
    if not ANTHROPIC_KEY:
        return None
    prompt = (
        "Voici le profil d'un candidat :\n" + PROFIL.get("profil_texte", "") +
        "\n\nVoici une offre d'emploi :\nTitre : " + offre.get("intitule", "") +
        "\nEntreprise : " + (offre.get("entreprise") or {}).get("nom", "?") +
        "\nDescription : " + offre.get("description", "")[:3000] +
        '\n\nNote l\'adéquation de 0 à 10. Réponds UNIQUEMENT en JSON : '
        '{"score": <entier>, "raison": "<une phrase courte en français>"}'
    )
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": "claude-haiku-5-5",
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=60)
        r.raise_for_status()
        txt = r.json()["content"][0]["text"]
        data = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        return int(data["score"]), str(data.get("raison", ""))
    except Exception as e:  # l'IA ne doit jamais bloquer la veille
        print(f"IA indisponible pour {offre.get('id')}: {e}", file=sys.stderr)
        return None


# ---------- Telegram ----------
def envoyer(offre: dict, score: int, mots: list[str], ia: tuple[int, str] | None):
    e = html.escape
    lien = (offre.get("origineOffre") or {}).get("urlOrigine") or \
        f"https://candidat.francetravail.fr/offres/recherche/detail/{offre['id']}"
    lieu = (offre.get("lieuTravail") or {}).get("libelle")
    lignes = [
        f"<b>{e(offre.get('intitule', 'Offre'))}</b>",
        f"🏢 {e((offre.get('entreprise') or {}).get('nom', 'Entreprise non précisée'))}",
        f"📍 {e(lieu or 'voir l’offre')}"
        f"  ·  📄 {e(offre.get('typeContratLibelle') or offre.get('typeContrat', '?'))}",
    ]
    sal = (offre.get("salaire") or {}).get("libelle")
    if sal:
        lignes.append(f"💶 {e(sal)}")
    lignes.append(f"⭐ Score mots-clés : {score}" + (f" ({e(', '.join(mots))})" if mots else ""))
    if ia:
        lignes.append(f"🤖 Score IA : {ia[0]}/10 — {e(ia[1])}")
    lignes.append(f'🔗 <a href="{e(lien)}">Voir l\'offre</a>')
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={
        "chat_id": TG_CHAT,
        "text": "\n".join(lignes),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=30)
    if r.status_code == 429:  # limite Telegram : on patiente puis on réessaie
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
        return envoyer(offre, score, mots, ia)
    r.raise_for_status()
    time.sleep(1)


# ---------- Programme principal ----------
def main():
    premier_lancement = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not premier_lancement else set()

    token = ft_token()
    offres = {}
    for mots in PROFIL["recherches"]:
        for dep in PROFIL["departements"]:
            for o in ft_search(token, mots, dep):
                offres[o["id"]] = o

    for o in lire_flux():
        offres[o["id"]] = o

    nouvelles = [o for oid, o in offres.items() if oid not in seen]
    print(f"{len(offres)} offres trouvées, {len(nouvelles)} nouvelles")

    envoyees = 0
    for o in nouvelles:
        seen.add(o["id"])
        if premier_lancement:
            continue  # 1er passage : on mémorise sans inonder Telegram
        score, mots = score_mots_cles(o, source_rss=o.get("_rss", False))
        if score < PROFIL.get("score_minimum", 0):
            continue
        ia = score_ia(o)
        if ia and ia[0] < PROFIL.get("score_ia_minimum", 0):
            continue
        envoyer(o, score, mots, ia)
        envoyees += 1

    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:]))
    print(f"{envoyees} offres envoyées sur Telegram")
    if premier_lancement:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={
            "chat_id": TG_CHAT,
            "text": f"✅ Veille activée : {len(seen)} offres existantes mémorisées. "
                    "Vous recevrez désormais uniquement les nouvelles offres pertinentes.",
        }, timeout=30)


if __name__ == "__main__":
    main()
