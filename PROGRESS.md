# PROGRESS — RepairFlow

Journal de travail (français). Cible finale : **V1.1**. Dernière mise à jour : 2026-10-09.

## État par milestone

| Milestone | État | Preuve |
|---|---|---|
| M0 — Environnement et contrats | ✅ fait | clone propre → `.env` neuf → `compose up --build` → provisioning depuis les exports → démo OK (voir §3) |
| M1 — Tranche sans LLM | ✅ fait | 3 workflows n8n réellement exécutés (W1, W3, W4), ticket `TCK-1000` dans ticket-service, email dans Mailpit, audit de bout en bout, approbation depuis `/review` |
| M2 — Fiabilité et concurrence | ✅ cœur fait | 37 tests verts : 9 unitaires, 18 intégration PostgreSQL, 10 pannes via le vrai n8n (§4). Reste : petit test de charge documenté |
| M3 — Preuves et propositions IA | ⏳ à faire | Ollama **non installé** (décision demandée, §6). Le fallback à règles est opérationnel et indépendant |
| M4 — Dashboard complet, recovery/digest n8n | ⏳ à faire | page provisoire `/review` seulement ; W5/W6 non créés |
| M5 — Replay Lab, évaluation | ⏳ à faire | injection de pannes déjà disponible dans les simulateurs |
| M6 — Extensions | facultatif | — |

## 1. Audit initial (2026-10-09)

* Dépôt vide (pas de git) → `git init`, `.gitattributes` force LF (sinon `init.sh`/`entrypoint.sh` en CRLF cassent les conteneurs, `core.autocrlf` est actif sur la machine).
* Machine : i7-10510U (8 threads), 20 Go RAM, GPU NVIDIA MX330 **2 Go VRAM**, Windows 10, Docker Desktop 29.1.3 (VM 10 Go), Python 3.12, Node 22. **≈15 Go libres sur C:** (Docker contient ~21 Go de cache de build récupérable appartenant à d'autres projets — non touché).
* Ollama absent. Ports 5432 et 8080 déjà occupés par d'autres projets → ports 15xxx/18xxx sur 127.0.0.1.

## 2. Versions réellement testées

| Composant | Version |
|---|---|
| n8n | `docker.n8n.io/n8nio/n8n:2.42.6` (digest `sha256:526daa38b68e…197a9a`) |
| Nodes n8n (lus dans `n8n-nodes-base/dist`) | Webhook 2, HTTP Request 4.2, Code 2, If 2.2, Respond to Webhook 1.1, Send Email 2.1 |
| PostgreSQL | `postgres:17-alpine` (n8n 2.42 signale PG16 comme « compatibility support only » → passage à 17) |
| Mailpit | `axllent/mailpit:v1.31.4` |
| Python (images) | `python:3.12-slim`, FastAPI 0.115.12, SQLAlchemy 2.0.40, psycopg 3.2.6, Alembic 1.15.2, Pydantic 2.11.3 |

## 3. Commandes

```bash
python scripts/init_env.py                 # .env avec secrets aléatoires (refuse d'écraser)
docker compose up -d --build               # postgres, api (migrations + seed), worker, simulateurs, mailpit, n8n
bash scripts/provision_n8n.sh              # credentials depuis .env, import des exports, publication, redémarrage, scan de secrets
python scripts/demo.py                     # scénario de démo complet via le webhook n8n
python scripts/demo.py --outage            # idem avec 2×503 du ticket-service
.venv/Scripts/python -m pytest -q tests    # unit + intégration + pannes (pile démarrée)
bash scripts/provision_n8n.sh --from-src   # après modification de scripts/build_workflows.py : import puis réexport normalisé
```

Interfaces : revue `http://127.0.0.1:18000/review` (jeton `RF_REVIEWER_TOKEN`), OpenAPI `http://127.0.0.1:18000/docs`,
Mailpit `http://127.0.0.1:18025`, n8n `http://127.0.0.1:15678` (création du compte owner à faire soi-même pour voir l'éditeur).

## 4. Résultats

### Chaîne M1 (exécution du 2026-10-09, 15:08–15:09 UTC)

* Webhook sans clé ou mauvaise clé → **403** (n8n). Rejeu même payload → **200** même incident ; même `event_id` autre payload → **409** ; équipement inconnu → **422**.
* Proposition à règles : contradiction « batterie pleine » ↔ `battery_pct=12`, refs `msg-…` et `tel-…` appartenant au manifest.
* Opérateur qui approuve → **403** ; approbation révision périmée → **409 stale_revision** ; ancien digest → **409 plan_changed**.
* Approbation valide → worker → W3 → `TCK-1000` → W4 → Mailpit → `completed` en ~2,5 s.
* Vérifié auprès de sources indépendantes : ticket-service (`GET /tickets`), API Mailpit, table `execution_entity` de la base n8n (6 exécutions `success`).

### Tests (pile Docker locale, même machine)

| Suite | Résultat | Durée |
|---|---|---|
| `tests/unit` | 9 passed | < 1 s |
| `tests/integration` (worker arrêté, le test joue le worker) | 18 passed | ~70 s |
| `tests/faults` (vrai n8n, worker actif) | 10 passed | ~3 min |

Bugs trouvés et corrigés par ces exécutions :
1. FK `actions.approval_id` violée : sans `relationship()`, SQLAlchemy n'ordonnait pas l'insert approbation/actions → `flush()` explicite.
2. Horloges : leases calculées avec l'horloge de chaque processus (hôte Windows vs VM Docker) → **PostgreSQL `now()` est l'unique horloge**.
3. SMTP : avec Mailpit arrêté, le node Send Email ne renvoie pas ECONNREFUSED mais reste bloqué ~70 s (nodemailer, pas de timeout configurable dans n8n 2.42.6) → lease d'exécution par connecteur (email 180 s) + ack tardif accepté pour le même essai.
4. `/healthz` de n8n répond avant l'enregistrement des webhooks → le provisioning attend un 403 sur le webhook.

### Reproductibilité (clone propre)

`git clone` dans un répertoire temporaire, `init_env.py`, `COMPOSE_PROJECT_NAME=rfclean docker compose up -d --build`
(volumes vierges), `provision_n8n.sh` (import des exports normalisés) → `demo.py` : succès complet, `TCK-1000` + 1 email.

## 5. Décisions prises (détails courants)

* Répartition n8n / API / worker : `docs/architecture.md`.
* Une seule instance PostgreSQL, trois bases et rôles séparés (`repairflow`, `n8n`, `tickets`).
* Une image Python pour API + worker ; une image `sim` pour deux services indépendants (ticket-service, telemetry-sim) afin de faire tomber l'un sans l'autre.
* Ticket et email = deux actions ; l'email ne devient éligible qu'après le succès du ticket. Clé : `rf:<incident>:r<revision>:<type>`.
* Contrat du ticket-service fictif : 503/429 = rejet avant traitement ; autre 5xx/timeout = effet inconnu → réconciliation par clé.
* Marqueur `[RF-…]` dans le sujet des emails (le node n'accepte pas de `Message-ID` personnalisé).
* Exports n8n normalisés (suppression des IDs de projet/horodatages propres à l'instance) ; le provisioning par défaut réimporte ces exports, ce qui vérifie le réimport à chaque installation.
* Auth M1 : jetons bearer locaux par rôle (pas de cookie → pas de surface CSRF). À revoir avec le dashboard M4 si passage aux cookies.

## 6. Travail restant (ordre proposé)

1. **M3** — décision Ollama (voir question posée) ; corpus de manuels fictifs, `retrieval_chunks` + full-text PostgreSQL, citations de manuels dans la proposition, client Ollama avec schéma JSON + revalidation Pydantic + une réparation de format max + fallback règles ; runs enregistrés dans `triage_runs`. Tests : LLM indisponible, JSON invalide, refs inventées, injection.
2. **M2 (compléments)** — petit test de charge documenté (débit, matériel) ; workflow d'erreur n8n vérifié avec une vraie erreur.
3. **M4** — dashboard React + TS (liste/filtres, revue versionnée, timeline, actions incertaines), SSE `/v1/events`, W5 recovery et W6 digest (clé de période), Playwright sur le parcours essentiel.
4. **M5** — `replay_runs`, namespace sandbox avec action_keys propres, scénarios du §12, dataset 60–100 cas FR/EN (labels relus à la main), rapport comparatif règles / LLM / LLM+preuves.
5. Docs : `docs/model-card.md` (M3), vidéo de démo (M5).

## 7. Scénarios à reproduire soi-même (préparation entretien)

1. `python scripts/demo.py` puis ouvrir `/review` : lire la timeline et expliquer chaque transition.
2. Arrêter n8n (`docker compose stop n8n`), approuver un incident, observer `dispatch_not_received` puis relancer n8n : rien n'est perdu (outbox).
3. Injecter un timeout après création : `curl -X POST 127.0.0.1:18081/_faults -H "X-Faults-Key: …" -H 'Content-Type: application/json' -d '{"mode":"delay_after","delay_s":15}'` puis approuver : `action_outcome_unknown` → `reconciled_found`, un seul ticket. Expliquer pourquoi un timeout n'est pas un échec distant.
4. Corriger un incident après approbation : actions non commencées annulées, nouvelle revue exigée (digest différent).
5. Arrêter Mailpit pendant un envoi : le ticket n'est jamais recréé ; discuter pourquoi SMTP n'a pas les mêmes garanties.
