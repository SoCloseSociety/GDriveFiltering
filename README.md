# GDriveFiltering

Backup complet de tes Google Drive (My Drive + Shared Drives + éléments partagés) en local
(PC + disque dur externe), puis vérification, dédup, réorganisation propre -- **sans jamais
rien supprimer avant qu'une backup vérifiée existe**.

## Contrat de sécurité
- Extraction **READ-ONLY** sur Google Drive (scope `drive.readonly`). Le code ne peut pas
  modifier ou supprimer quoi que ce soit sur Drive.
- **Preflight disque** : avant d'extraire, on compare la taille du Drive à l'espace libre.
  Si ça ne rentre pas, le programme **s'arrête et te demande de brancher un disque dur**.
- Dédup / réorg = **détection + rapport + quarantaine dans une COPIE**. Jamais de suppression.
- La seule commande qui peut supprimer (`purge`) **refuse** de tourner sans miroir principal
  + miroir externe **vérifiés** (sha256) et un flag explicite, et n'agit que sur la COPIE.

## Installation
```bash
make setup        # crée .venv et installe les dépendances
cp .env.example .env   # ajuste au besoin (les creds Google sont auto-détectés)
make doctor       # vérifie creds, espace disque, Ollama
```

## Utilisation (dans l'ordre)
```bash
# 1. Authentifier un compte Google (consent dans le navigateur, une seule fois)
make auth ACCOUNT=perso

# 2. Backup READ-ONLY de tous les drives (preflight disque inclus)
make backup ACCOUNT=perso

# 2b. Suivre la progression en direct (barre + ETA), pendant que le backup tourne
python -m gdrivefilter status --account perso --watch

# 3. Vérifier l'intégrité (count + taille + sha256)
make verify DIR=backups/AAAAMMJJ_HHMMSS/perso

# 3b. Proposer le clean tree final (rapport texte + JSON + dashboard HTML), sans rien écrire
python -m gdrivefilter propose --account perso   # -> reports/proposal.(txt|json|html)

# 4. Détecter les doublons (aucune suppression). --semantic ajoute Ollama bge-m3
make dedup DIR=backups/AAAAMMJJ_HHMMSS/perso SEMANTIC=1

# 5. Réorganiser en COPIE dans une arbo propre (par catégorie/année)
make reorganize DIR=backups/AAAAMMJJ_HHMMSS/perso DEST=clean/perso

# 6. (Optionnel, ultra-gardé) purger les doublons dans la COPIE
#    dry-run par défaut ; APPLY=1 pour supprimer réellement
make purge DIR=backups/AAAAMMJJ_HHMMSS/perso DEST=clean/perso        # dry-run
make purge DIR=backups/AAAAMMJJ_HHMMSS/perso DEST=clean/perso APPLY=1
```

Multi-comptes : relance `auth` + `backup` avec un `ACCOUNT` différent.

### Reprise & performance
- **Reprise automatique** : relancer `backup` reprend le dernier backup INCOMPLET du compte
  (saute ce qui est déjà fait). `--new` force un nouveau backup.
- **Téléchargements parallèles** : `DOWNLOAD_WORKERS` (défaut 8) contrôle la concurrence.
  Monter à 16 sature mieux la bande passante sur les gros volumes; baisser si quotas API.
- **100% resumable** : coupure/veille/débranchement -> relancer la même commande reprend.

## Credentials
Aucun projet GCP à créer. Les `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` sont réutilisés
depuis les projets voisins (`KontentReachDash`, `clip-generator-local`) si absents du `.env`
local. Si Google renvoie `redirect_uri_mismatch`, ajoute `http://localhost:8765/` aux redirect
URIs autorisés du client OAuth (voir `OAUTH_LOOPBACK_PORT`).

## Tests
```bash
make test    # 21 tests, aucun accès réseau (fake Drive API)
```

## Application de bureau (Mac / Windows)
Un lanceur double-cliquable qui démarre le dashboard et l'ouvre — sans terminal.
```bash
make app        # macOS: crée ~/Desktop/GDriveFiltering.app (icône incluse)
```
Windows : `desktop/windows/GDriveFiltering.vbs` (raccourci sur le Bureau). Détails dans
[desktop/README.md](desktop/README.md).

## Dashboard & suivi (monitoring + quick actions)
```bash
python -m gdrivefilter dashboard          # http://127.0.0.1:8787 (s'ouvre tout seul)
```
Dashboard web local (127.0.0.1 uniquement) : progression live par compte (barre + ETA + débit),
répartition par catégorie/source/année, doublons et junk, et **quick actions** (résumer backup,
vérifier, proposer le clean tree, dédup, réorg dry-run, ouvrir le dossier). La suppression
(`purge`) n'y est **pas** exposée -- action destructive réservée à la ligne de commande gardée.

## Robustesse (gros volumes)
- **Parallélisme** (`DOWNLOAD_WORKERS`) + **streaming disque** (mémoire bornée, gros fichiers OK).
- **Timeout socket + retry réseau** : un téléchargement bloqué est réessayé, jamais de hang.
- **Reprise automatique** du dernier backup incomplet ; **heartbeat** pour un suivi fiable.
- **exFAT/insensible à la casse** géré (chemins uniques casefold, noms sûrs FAT).

## Architecture
| Module | Rôle |
|---|---|
| `config.py` | Config + résolution des creds depuis les .env voisins |
| `auth.py` | OAuth loopback, token par compte, refresh |
| `drive_client.py` | Drive v3 all-drives, export des fichiers Google natifs |
| `preflight.py` | Contrôle d'espace disque (stop + demande de disque dur) |
| `extract.py` | Backup resumable, multi-destinations, READ-ONLY |
| `manifest.py` | Index sha256 + point de reprise |
| `verify.py` | Vérif intégrité + **gate de sécurité** |
| `dedup.py` | Doublons exacts (hash) + sémantiques (Ollama) |
| `filters.py` | Détection junk/clutter (système, temp, fichiers vides) |
| `propose.py` | Analyse + proposition du clean tree (texte/JSON/HTML) |
| `reorganize.py` | Arbo propre en COPIE, quarantaine (doublons + junk) |
| `clean.py` | `purge` ultra-gardé (seule voie de suppression) |
| `progress.py` | Heartbeat + snapshots pour `status`/dashboard |
| `dashboard.py` | Dashboard web local (monitoring + quick actions) |
| `ollama_client.py` | LLM/embeddings locaux (RTX 4070), dégradation gracieuse |
