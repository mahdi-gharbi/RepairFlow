# PROGRESS — RepairFlow

Journal de travail (français). Cible : **V1.1**. Dernière mise à jour : 2026-10-10, ~14:30 UTC.

## État par milestone

| Milestone | État | Preuve principale |
|---|---|---|
| M0 — Environnement et contrats | ✅ | clone propre → `.env` neuf → `compose up --build` → provisioning depuis les exports → démo OK (§4.7) |
| M1 — Tranche sans LLM | ✅ | W1/W3/W4 réellement exécutés ; ticket + email vérifiés auprès de ticket-service, Mailpit et `execution_entity` de n8n |
| M2 — Fiabilité et concurrence | ✅ | 18 tests de fiabilité PostgreSQL + 10 tests de pannes via le vrai n8n ; test de charge documenté (§4.5) |
| M3 — Preuves et propositions IA | ✅ (LLM **distant** Groq, décision du porteur) | full-text PostgreSQL, citations validées, sorties `gpt-oss-20b` réelles, 14 tests de contrat |
| M4 — Dashboard, recovery/digest, n8n reproductible | ✅ | dashboard React+TS (SSE, revue versionnée, actions incertaines) ; W0/W5/W6 vérifiés avec de vraies exécutions ; Playwright 3/3 |
| M5 — Replay Lab et évaluation | ✅ (labels à relire) | 12 scénarios sandbox, 11 tests ; campagne 51 cas, rapport `evaluation/reports/20261009T212922Z` |
| M6 — Extensions | décidé : non justifié pour l'instant | `docs/extensions.md` (Recall@3 déjà 1,0 ; corrélation suffit ; connecteurs locaux imposés) |

**Reste avant de considérer V1.1 « présentable »** : relecture humaine des labels, mesure du prompt v2 + règle de cohérence (bloquée par le quota quotidien Groq, §4.6), vidéo de démo, révocation de la clé Groq exposée (§6).

## 1. Audit initial

* Dépôt vide → `git init`, `.gitattributes` force LF (`core.autocrlf` actif sur la machine ; scripts shell montés dans des conteneurs Linux).
* i7-10510U (4C/8T), 20 Go RAM, MX330 2 Go VRAM, Windows 10, Docker Desktop 29.1.3 (VM 10 Go), Python 3.12, Node 22.14. ~15 Go libres sur C:.
* Ports 5432/8080 occupés par d'autres projets → 15xxx/18xxx sur 127.0.0.1.

## 2. Versions réellement testées

| Composant | Version |
|---|---|
| n8n | `docker.n8n.io/n8nio/n8n:2.42.6` (digest `sha256:526daa38b68e…197a9a`) |
| Nodes n8n (lus dans `n8n-nodes-base/dist`) | Webhook 2, HTTP Request 4.2, Code 2, If 2.2, Respond to Webhook 1.1, Send Email 2.1, Schedule Trigger 1.2, Error Trigger 1, Form Trigger 2.2 |
| PostgreSQL | `postgres:17-alpine` (n8n 2.42 : PG16 en « compatibility support » seulement) |
| Mailpit | `axllent/mailpit:v1.31.4` |
| API/worker | `python:3.12-slim`, FastAPI 0.115.12, SQLAlchemy 2.0.40, psycopg 3.2.6, Alembic 1.15.2, Pydantic 2.11.3 |
| Dashboard | `node:22.14.0-alpine` (build), React 18.3.1, Vite 5.4.10, TypeScript 5.6.3, Playwright 1.48.2 (Chromium 1140) |
| LLM (distant) | Groq `openai/gpt-oss-20b`, prompt `triage-llm-2026.10-2` déployé (v1 conservée), `temperature=0`, `seed=20261009` |

## 3. Commandes

```bash
python scripts/init_env.py                    # .env avec secrets aléatoires (+ GROQ_API_KEY optionnelle)
docker compose up -d --build                  # postgres, api (+dashboard), worker, simulateurs, mailpit, n8n
bash scripts/provision_n8n.sh                 # credentials depuis .env, import des 6 workflows, publication, scan de secrets
python scripts/demo.py [--outage]             # scénario de démo via le webhook n8n
.venv/Scripts/python -m pytest -q tests       # 68 tests (pile démarrée)
cd apps/dashboard && npx playwright test      # 3 parcours e2e
python evaluation/runner/run_eval.py          # campagne d'évaluation (~25 min, limite de débit Groq)
python scripts/load_test.py                   # test de charge local
python scripts/diagnostics.py                 # export de diagnostic partageable (liste blanche, scan de secrets)
python scripts/reset_demo.py --yes            # vide les données de démo (garde équipements, équipes, manuels)
bash scripts/provision_n8n.sh --from-src      # après modification de scripts/build_workflows.py
```

Formulaire opérateur n8n : <http://127.0.0.1:15678/form/rf-report> (`RF_FORM_USER` / `RF_FORM_PASSWORD`).
Interfaces : dashboard <http://127.0.0.1:18000/app/> (jetons `RF_REVIEWER_TOKEN` / `RF_OPERATOR_TOKEN`), page de secours `/review`,
OpenAPI `/docs`, Mailpit <http://127.0.0.1:18025>, n8n <http://127.0.0.1:15678> (compte owner à créer soi-même pour l'éditeur).

## 4. Résultats

### 4.1 Tests (2026-10-10 ~14:45 UTC, fournisseur LLM en limite quotidienne → replis règles pendant les tests)

| Suite | Résultat |
|---|---|
| `tests/unit` | 10 passed |
| `tests/integration` (fiabilité 18, contrat LLM 10 avec double local du fournisseur, retrieval 5, replay 11) | 44 passed |
| `tests/faults` (vrai n8n, worker actif) | 10 passed |
| `tests/workflow` (W0/W5/W6, SSE, métriques, diagnostic, corrélation, formulaire W2) | 9 passed |
| **Total pytest** | **73 passed** (12 min 50) |
| Playwright `apps/dashboard/e2e` | **3 passed** |

### 4.2 Chaîne M1/M2 (rappel)

Rejeu 200 / conflit 409 / 422 ; approbation périmée 409 ; 503×2 → 3 tentatives ; timeout après création → `uncertain` → réconcilié par clé, 1 ticket ;
n8n arrêté → `dispatch_not_received` puis reprise ; SMTP arrêté → pas de recréation de ticket ; redémarrage API/worker/n8n → reprise.

### 4.3 M4 — opérations et dashboard

* W5 (Schedule Trigger chaque minute) → `POST /v1/ops/recovery` : exécutions `trigger` vues dans `execution_entity`, audit `recovery_sweep`.
* W6 : deux déclenchements pour la même période → **1 seul email** (`digests.period_key`, statut `sent`).
* W0 (Error Trigger, `errorWorkflow` de tous les workflows) : vraie erreur provoquée (422 sur clé de période invalide) → audit `n8n_execution_failed` avec workflow et nœud.
* Dashboard : SSE via `fetch` (jeton en en-tête), bandeau + approbation désactivée si une autre révision arrive pendant la lecture, citations cliquables, panneau « action incertaine » (réconcilier / marquer / réessayer avec avertissement de doublon / abandonner), timeline, volet technique, onglet Replay.
* Bugs trouvés : `Decimal` non sérialisable en JSONB (métriques du digest) ; `index.html` mis en cache par le navigateur après redéploiement → `no-cache` sur `index.html`, `immutable` sur les assets hachés.

### 4.4 M5 — Replay Lab et évaluation

* Replay (`docs/replay-lab.md`) : 12 scénarios ; simulateur sandbox qui réutilise règles, validation des refs, politique, digest, table de transitions et `dispatch.decide` (fonction extraite de `/ack`) ; aucune ligne `actions`/`outbox` créée (testé).
* Évaluation (`evaluation/README.md`) : 51 cas FR/EN (24 dev / 27 test), **labels brouillon non relus**. Test : macro-F1 catégorie règles 0,967 / LLM 1,0 / LLM+preuves 1,0 ; contradiction P/R règles 1,0/1,0, LLM 0,714/1,0 ; clarification P/R règles 1,0/1,0, LLM 0,545/0,923, LLM+preuves 0,556/0,769 ; schéma valide 27/27 ; Recall@3 = 1,0, MRR@3 = 0,848 ; **0 action non autorisée**. Le LLM sur-demande des clarifications ; LLM+preuves a proposé `prepare_ticket` pour une demande hors sujet.
* Biais déclarés : même auteur pour règles, cas et labels (favorise les règles) ; jeu facile (codes d'erreur explicites).
* Incidents de campagne : 1er passage invalidé par **mon** protocole (règles évaluées sans manuels ; cas sans document comptés comme échecs) → corrigé et relancé ; coupure DNS 21:43–21:50 UTC → 8 cas relancés (`--retry-unreachable`, consigné dans `meta.json`).

### 4.5 Test de charge (`artifacts/demo/load_test_20261009T220159Z.json`)

Portable i7-10510U, toute la pile dans Docker, 1 processus uvicorn, worker tick 1 s.
* Intake : 200 signalements, 20 clients → 200 créés, 0 erreur, **8,9 incidents/s** (création + télémétrie + triage règles) ; création p50 667 ms / p95 1 081 ms ; payload 256 o.
* Dispatch : 30 approbations simultanées (60 actions, 60 exécutions n8n) → 30 complétés, approbation→fin p50 22 s, max 25 s (~58 incidents/min).
* Pas un benchmark : une machine, pas de réplication, valeurs indicatives.

### 4.6 Jour 2 (2026-10-10) — exploitation, formulaire, prompt v2

* **Corrélation** : `X-Correlation-Id` propagé par W1 et W3/W4 vers l'API (journal JSON par requête, sans corps) et le
  ticket-service ; même valeur retrouvée dans les logs API, worker, ticket-service (test `test_correlation_id_reaches_the_ticket_service`).
  C'est une corrélation, pas du tracing distribué.
* **Rétention** : élagage des exécutions n8n (14 jours / 20 000), l'état métier n'en dépend pas. Audit conservé (démo) ;
  `scripts/reset_demo.py --yes` vide les données métier (testé sur une pile jetable : refus sans `--yes`, incidents/audit → 0,
  équipements/manuels conservés). `scripts/diagnostics.py` : export en liste blanche, scan de secrets (testé : aucun texte de signalement).
* **W2 formulaire opérateur** (Form Trigger 2.2, auth basique) → webhook W1 : un seul chemin d'intake. 401 sans identifiants ;
  soumission réelle → incident trié. Piège trouvé : à partir de la v2.2 le chemin du formulaire se lit dans `options.path`.
* **Bug trouvé** : `demo.py` plantait quand sa sortie était redirigée (cp1252 sous Windows + texte LLM) → stdout UTF-8 forcé.
* **Prompt v2** (dev seulement) : référence v1 sur le dev (`evaluation/reports/20261010T131909Z`, 24 cas) : sur-clarification
  confirmée (clarify P 0,565 / 0,706). v2 = critères de décision explicites + âge/péremption de la télémétrie calculés de façon
  déterministe. Signal partiel sur 10 cas dev identiques : clarify P/R `llm` 0,556/1,0 → 1,0/1,0, mais `retrieval_llm`
  0,667/0,8 → 1,0/**0,4** : le modèle propose `prepare_ticket` malgré une contradiction qu'il a lui-même rapportée (3/3 cas).
  → **règle de cohérence déterministe** (`enforce_next_step`) : contradiction ou télémétrie indisponible/périmée ⇒ `clarify`,
  sortie brute conservée, ajustement tracé et affiché (« Adjusted by rule »). Testée avec le double du fournisseur ; **pas encore mesurée sur le vrai modèle**.
* **Quota** : le palier gratuit Groq plafonne à **200 000 tokens/jour** (fenêtre glissante). Atteint à 13:59 UTC
  (référence dev v1 + moitié de v2). Mesurer « v2 + règle » sur le dev puis une fois sur le test ≈ 250 000 tokens, soit plus
  d'une journée de quota. Le runner est désormais reprenable (`--resume`) et enregistre les versions (`meta.json`).
* Contamination déclarée : les erreurs du test avaient été vues lors de la campagne du 09/10 ; v2 a été écrit à partir du dev,
  mais le test n'est plus parfaitement aveugle pour cette comparaison.

### 4.7 Reproductibilité

Refait le 2026-10-09 ~22:15 UTC après M4/M5 : `git clone` → `.env` neuf **sans clé LLM** → `COMPOSE_PROJECT_NAME=rfclean docker compose up -d --build` (volumes vierges, build multi-étapes du dashboard) → `provision_n8n.sh` (6 workflows réimportés depuis les exports, actifs, scan de secrets propre) → `/app/` 200 → `demo.py` : rejeu 200, contradiction, 409 périmé, `completed`, 1 ticket, 1 email. Provenance `rules` : le système complet fonctionne sans LLM.

## 5. Décisions (détails courants)

* Répartition n8n / API / worker : `docs/architecture.md`. Garanties par connecteur : `docs/reliability.md`.
* PostgreSQL = seule horloge des leases ; ordre de verrouillage incident → outbox → action ; lease d'exécution par connecteur (ticket 60 s, email 180 s).
* Ticket et email = deux actions ; email éligible après le ticket. Marqueur `[RF-…]` dans le sujet (pas de `Message-ID` personnalisable dans le node).
* LLM appelé par l'API (contexte exact, validation, réparation, repli) ; schéma strict construit par requête avec refs en `enum`.
* Dashboard servi par l'API (`/app`), jeton bearer (pas de cookie → pas de surface CSRF), tests e2e dans `apps/dashboard/e2e` (résolution des modules Node).
* Digest : réservation de la période avant envoi ; doublon possible si l'accusé « sent » est perdu (accepté et documenté).
* Replay : simulateur plutôt que rejouer la vraie pile ; les pannes réelles restent couvertes par `tests/faults`.

## 6. Travail restant

1. **Sécurité (porteur)** : révoquer la clé Groq collée dans la conversation et en créer une nouvelle.
2. **Mesure v2 + règle** : `run_eval.py --llm-split dev` puis, une seule fois, `--llm-split test` (≈ 250 k tokens : 2 jours de palier gratuit, ou un palier payant / un autre fournisseur compatible OpenAI). Décider ensuite de la version par défaut.
3. **Labels** : relire `evaluation/datasets/cases_v1.jsonl` (surtout « télémétrie périmée = contradiction ? »), compléter vers 60–100 cas avec des formulations écrites par quelqu'un d'autre, relancer `run_eval.py`.
4. Vidéo de démo 90–120 s (doublon → contradiction → revue versionnée → panne/reprise → timeline → replay).
5. M6 : seulement si un cas concret le justifie (`docs/extensions.md`).

## 7. Scénarios à reproduire soi-même (préparation entretien)

1. `python scripts/demo.py`, puis dashboard : lire la timeline et expliquer chaque transition.
2. Ouvrir un incident dans le dashboard, le corriger via l'API dans un autre terminal : bandeau « révision périmée », approbation désactivée. Expliquer le digest.
3. `docker compose stop n8n`, approuver, observer `dispatch_not_received`, relancer n8n : rien n'est perdu (outbox).
4. Injecter `{"mode":"delay_after","delay_s":15}` sur `127.0.0.1:18081/_faults` (en-tête `X-Faults-Key`) puis approuver : `action_outcome_unknown` → `reconciled_found`, un seul ticket. Pourquoi un timeout n'est pas un échec distant.
5. Onglet Replay : `email_accepted_response_lost` puis `policy_clarify_on_contradiction` ; expliquer pourquoi SMTP n'a pas les garanties du ticket et ce que change une autre règle.
6. Lire `evaluation/README.md` et défendre pourquoi les chiffres ne sont pas encore une preuve de qualité.
