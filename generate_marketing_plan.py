"""
generate_marketing_plan.py — Génération du plan marketing via LLM
==================================================================

Gère les limites de quota Gemini 2.5 Flash Free Tier :
  RPM = 5   → délai minimum de 13s entre chaque appel API
  TPM = 250k → le prompt (~1 700 tokens) est très loin de la limite
  RPD = 20   → mode "1 appel" par défaut, "2 appels" si output tronqué

Stratégie :
  1. Tentative en 1 seul appel  (coût : 1 RPD)
  2. Si réponse tronquée ou erreur → split en 2 appels  (coût : 2 RPD)
  3. Retry sur 429 : attente 13s + backoff (max 2 retries par appel)

Utilisation :
    python generate_marketing_plan.py

Prérequis :
    - insights_consolides.json doit exister (lancez synthesize_insights.py d'abord)
    - Clé Gemini dans .env : API_KEY=...
    - python -m pip install google-genai python-dotenv
"""

import io
import json
import os
import sys
import textwrap
import time
from pathlib import Path

# ── Console UTF-8 (Windows cp1252 fix) ────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config


# ══════════════════════════════════════════════════════════════════════════════
# GESTION DU QUOTA RPD
# ══════════════════════════════════════════════════════════════════════════════

# Fichier de suivi du quota journalier (reset à minuit UTC)
_QUOTA_FILE = config.ROOT / ".gemini_quota.json"

def _load_quota() -> dict:
    if _QUOTA_FILE.exists():
        try:
            data = json.loads(_QUOTA_FILE.read_text(encoding="utf-8"))
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if data.get("date") == today:
                return data
        except Exception:
            pass
    return {"date": time.strftime("%Y-%m-%d", time.gmtime()), "calls": 0}

def _save_quota(data: dict):
    _QUOTA_FILE.write_text(json.dumps(data), encoding="utf-8")

def _check_and_increment_quota(label: str = "") -> int:
    """Vérifie le quota RPD, incrémente le compteur, retourne le nb d'appels."""
    RPD_LIMIT = 20
    RPD_WARN  = 15
    data = _load_quota()
    data["calls"] += 1
    _save_quota(data)
    n = data["calls"]
    tag = f"[{label}] " if label else ""
    if n > RPD_LIMIT:
        print(f"  {tag}[QUOTA] ATTENTION : {n}/{RPD_LIMIT} appels aujourd'hui — limite atteinte !")
    elif n >= RPD_WARN:
        print(f"  {tag}[QUOTA] Attention : {n}/{RPD_LIMIT} appels Gemini utilises aujourd'hui.")
    else:
        print(f"  {tag}[QUOTA] Appel #{n}/{RPD_LIMIT} aujourd'hui.")
    return n


# ══════════════════════════════════════════════════════════════════════════════
# CONSTRUCTION DU PROMPT
# ══════════════════════════════════════════════════════════════════════════════

def _format_funnel_block(dims: dict) -> str:
    lines = []
    for folder, data in dims.items():
        if folder == "Purchased item" or not data:
            continue
        dim_label  = data.get("dimension", folder)
        etape_crit = data.get("etape_la_plus_critique", "?")
        top3_steps = data.get("etapes_critiques_top3", [])
        top3_dim   = data.get("top3_dim_fort_abandon", [])
        best3_dim  = data.get("top3_dim_meilleur_passage", [])

        lines.append(f"\n### Dimension : {dim_label}")
        lines.append(f"- Etape la plus critique : {etape_crit}")
        if top3_steps:
            parts = ", ".join(f"{e['etape']} ({e['abandon_pct']}%)" for e in top3_steps)
            lines.append(f"- Top 3 etapes par abandon : {parts}")
        if top3_dim:
            parts = ", ".join(f"{e['valeur']} ({e['abandon_moyen_pct']}%)" for e in top3_dim[:3])
            lines.append(f"- Plus fort abandon : {parts}")
        if best3_dim:
            parts = ", ".join(f"{e['valeur']} ({e['abandon_moyen_pct']}%)" for e in best3_dim[:3])
            lines.append(f"- Meilleur passage  : {parts}")
        trend = data.get("evolution_mensuelle_etape_critique_pct", {})
        if trend:
            trend_str = " > ".join(f"{m.capitalize()} {v}%" for m, v in trend.items())
            lines.append(f"- Evolution ({etape_crit}) : {trend_str}")
    return "\n".join(lines)


def _format_items_block(items: dict) -> str:
    rev_total   = items.get("revenu_total_periode_mad", 0)
    monthly_rev = items.get("evolution_revenu_mensuel_mad", {})
    top5_rev    = items.get("top5_produits_par_revenu", [])
    top5_opp    = items.get("top5_opportunites_conversion", [])

    monthly_str = " > ".join(f"{m.capitalize()} {v:,.0f} MAD" for m, v in monthly_rev.items())
    top5_rev_str = "\n".join(
        f"  {i+1}. {p['produit']} — {p['revenu_total_mad']:,.0f} MAD"
        f" ({p['acheteurs']} acheteurs, conv {p['taux_conversion_pct']}%)"
        for i, p in enumerate(top5_rev)
    )
    top5_opp_str = "\n".join(
        f"  {i+1}. {p['produit']} — {p['utilisateurs']} visites,"
        f" conv {p['taux_conversion_pct']}%, revenu {p['revenu_total_mad']:,.0f} MAD"
        for i, p in enumerate(top5_opp)
    )
    return (
        f"Revenu total Jan-Juin 2026 : {rev_total:,.0f} MAD\n"
        f"Evolution mensuelle : {monthly_str}\n\n"
        f"Top 5 produits par revenu :\n{top5_rev_str}\n\n"
        f"Top 5 opportunites (fort trafic, faible conversion) :\n{top5_opp_str}"
    )


def build_prompt_part1(insights: dict) -> str:
    """
    Partie 1 du plan : Synthese executive + Priorite 1 (abandon panier)
    Utilisee en mode chunk (2 appels) ou en mode single (1 appel).
    En mode single, le parametre include_all=True ajoute toutes les sections.
    """
    meta  = insights.get("metadata", {})
    dims  = insights.get("dimensions", {})
    hypo  = insights.get("hypotheses_cross_dimensions", [])
    items = dims.get("Purchased item", {})

    funnel_block = _format_funnel_block(dims)
    items_block  = _format_items_block(items)
    hypo_str     = "\n".join(f"- {h}" for h in hypo)

    return textwrap.dedent(f"""
    Tu es expert en marketing digital e-commerce pour Maroc Telecom (portail iam.ma).
    Periode analysee : {meta.get('periode', 'Janvier-Juin 2026')}.
    Tunnel : {meta.get('tunnel', 'view_item > add_to_cart > begin_checkout > add_shipping_info > add_payment_info > purchase')}

    ## DONNEES FUNNEL PAR DIMENSION
    {funnel_block}

    ## REVENUS ET PRODUITS
    {items_block}

    ## HYPOTHESES CROSS-DIMENSIONS
    {hypo_str}

    ---

    Redige les sections suivantes du plan marketing (en francais, ton professionnel, chiffres cites) :

    ### 1. SYNTHESE EXECUTIVE
    3 a 5 constats cles chiffres en bullet points.

    ### 2. PRIORITE 1 — REDUCTION DU TAUX D'ABANDON PANIER
    - 3 principaux points de friction dans le tunnel (avec chiffres)
    - Actions concretes par device, canal et etape du funnel
    - KPIs a suivre avant/apres mise en oeuvre
    """).strip()


def build_prompt_part2(insights: dict) -> str:
    """
    Partie 2 du plan : Priorite 2 + Plan 3 mois + KPIs de suivi.
    Utilisee uniquement en mode chunk (2 appels).
    """
    meta  = insights.get("metadata", {})
    dims  = insights.get("dimensions", {})
    hypo  = insights.get("hypotheses_cross_dimensions", [])
    items = dims.get("Purchased item", {})

    items_block = _format_items_block(items)
    hypo_str    = "\n".join(f"- {h}" for h in hypo)

    return textwrap.dedent(f"""
    Tu es expert en marketing digital e-commerce pour Maroc Telecom (portail iam.ma).
    Periode : {meta.get('periode', 'Janvier-Juin 2026')}.

    ## DONNEES PRODUITS ET HYPOTHESES
    {items_block}

    Hypotheses cross-dimensions :
    {hypo_str}

    ---

    Redige les sections suivantes du plan marketing (en francais, ton professionnel) :

    ### 3. PRIORITE 2 — AUGMENTATION DES VENTES
    - Actions sur produits a fort potentiel (trafic eleve, conversion faible)
    - Actions sur segments geografiques sous-exploites
    - Strategie d'optimisation des canaux performants

    ### 4. PLAN D'ACTION SUR 3 MOIS
    Tableau :
    | Mois | Action | Responsable | KPI cible | Impact estime |
    3-4 actions par mois (Mois 1, Mois 2, Mois 3).

    ### 5. KPIs DE SUIVI
    5 a 8 metriques a monitorer dans GA4 apres mise en oeuvre.
    """).strip()


def build_prompt_single(insights: dict) -> str:
    """
    Prompt complet pour un seul appel LLM (mode par defaut).
    """
    meta  = insights.get("metadata", {})
    dims  = insights.get("dimensions", {})
    hypo  = insights.get("hypotheses_cross_dimensions", [])
    items = dims.get("Purchased item", {})

    funnel_block = _format_funnel_block(dims)
    items_block  = _format_items_block(items)
    hypo_str     = "\n".join(f"- {h}" for h in hypo)

    return textwrap.dedent(f"""
    Tu es expert en marketing digital e-commerce pour Maroc Telecom (portail iam.ma).
    Periode analysee : {meta.get('periode', 'Janvier-Juin 2026')}.
    Tunnel : {meta.get('tunnel', 'view_item > add_to_cart > begin_checkout > add_payment_info > purchase')}

    ## DONNEES FUNNEL PAR DIMENSION
    {funnel_block}

    ## REVENUS ET PRODUITS
    {items_block}

    ## HYPOTHESES CROSS-DIMENSIONS
    {hypo_str}

    ---

    Redige un plan marketing complet, priorise et actionnable pour Maroc Telecom iam.ma :

    ### 1. SYNTHESE EXECUTIVE (3-5 constats cles chiffres)

    ### 2. PRIORITE 1 — REDUCTION DU TAUX D'ABANDON PANIER
    - 3 principaux points de friction (avec chiffres)
    - Actions concretes par device, canal et etape du funnel
    - KPIs avant/apres

    ### 3. PRIORITE 2 — AUGMENTATION DES VENTES
    - Actions sur produits fort potentiel (trafic eleve, conversion faible)
    - Actions sur segments geografiques
    - Optimisation des canaux

    ### 4. PLAN D'ACTION SUR 3 MOIS
    | Mois | Action | Responsable | KPI cible | Impact estime |
    3-4 actions par mois.

    ### 5. KPIs DE SUIVI GA4
    5-8 metriques a monitorer apres mise en oeuvre.

    Contraintes : francais, ton professionnel, citer les chiffres fournis, ne pas inventer de donnees.
    """).strip()


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND GEMINI (avec gestion quota RPM / RPD)
# ══════════════════════════════════════════════════════════════════════════════

class LLMError(Exception):
    pass


# Délai minimum entre appels Gemini (RPM=5 → 60s/5 = 12s + 1s marge)
GEMINI_INTER_CALL_DELAY = 13  # secondes

_last_gemini_call_ts: float = 0.0  # timestamp du dernier appel


def _gemini_rate_limit():
    """Attend si nécessaire pour respecter RPM=5."""
    global _last_gemini_call_ts
    elapsed = time.time() - _last_gemini_call_ts
    if elapsed < GEMINI_INTER_CALL_DELAY and _last_gemini_call_ts > 0:
        wait = GEMINI_INTER_CALL_DELAY - elapsed
        print(f"  [Rate limit] Pause {wait:.1f}s pour respecter RPM=5...")
        time.sleep(wait)
    _last_gemini_call_ts = time.time()


def _gemini_call(client, generate_config, prompt: str, label: str = "") -> str:
    """
    Effectue un appel Gemini avec :
      - respect du délai inter-appels (RPM=5)
      - comptage RPD
      - retry x2 sur 429 (attente 13s puis 26s)
    """
    _gemini_rate_limit()
    _check_and_increment_quota(label)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            from google.genai import types as genai_types
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=generate_config,
            )
            text = response.text
            if not text or len(text.strip()) < 50:
                raise LLMError("Reponse Gemini vide ou trop courte.")
            return text.strip()

        except Exception as e:
            err = str(e)
            if "429" in err and attempt < max_retries - 1:
                wait_s = GEMINI_INTER_CALL_DELAY * (attempt + 1) * 2  # 26s, 52s
                print(f"  [429] Quota temporaire depasse. Pause {wait_s}s (retry {attempt+2}/{max_retries})...")
                time.sleep(wait_s)
                _last_gemini_call_ts = time.time()  # reset timer
            else:
                raise LLMError(f"Erreur Gemini : {err[:300]}")

    raise LLMError("Max retries atteint.")


def generate_gemini_single(insights: dict) -> str:
    """
    Génère le plan marketing en 1 seul appel API (coût : 1 RPD).
    Lève LLMError si la réponse semble tronquée.
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        raise LLMError("Package 'google-genai' non installe. Lancez : python -m pip install google-genai")

    api_key = os.environ.get("API_KEY")
    if not api_key:
        raise LLMError("API_KEY manquante dans .env")

    client = genai.Client(api_key=api_key)
    gen_cfg = genai_types.GenerateContentConfig(
        temperature=config.GEMINI_TEMPERATURE,
        max_output_tokens=config.GEMINI_MAX_TOKENS,
    )

    prompt = build_prompt_single(insights)
    print(f"  [Single] Prompt : {len(prompt)} chars (~{len(prompt)//4} tokens)")
    print(f"  [Single] Envoi a Gemini ({config.GEMINI_MODEL})...")

    text = _gemini_call(client, gen_cfg, prompt, label="appel unique")

    # Detection de troncature : le plan complet doit avoir au moins 4 sections
    sections_found = sum(1 for kw in ["SYNTHESE", "PRIORITE 1", "PRIORITE 2", "PLAN D'ACTION", "KPI"]
                         if kw.upper() in text.upper())
    if sections_found < 3:
        raise LLMError(
            f"Reponse semble tronquee ({sections_found}/5 sections detectees). "
            "Passage en mode chunk (2 appels)."
        )

    return text


def generate_gemini_chunked(insights: dict) -> str:
    """
    Génère le plan marketing en 2 appels séquentiels (coût : 2 RPD).
    Sauvegarde le chunk 1 immédiatement pour ne pas le perdre.
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        raise LLMError("Package 'google-genai' non installe. Lancez : python -m pip install google-genai")

    api_key = os.environ.get("API_KEY")
    if not api_key:
        raise LLMError("API_KEY manquante dans .env")

    client = genai.Client(api_key=api_key)
    gen_cfg = genai_types.GenerateContentConfig(
        temperature=config.GEMINI_TEMPERATURE,
        max_output_tokens=config.GEMINI_MAX_TOKENS,
    )

    # ── Appel 1 : Sections 1 & 2 ────────────────────────────────────────────
    prompt1 = build_prompt_part1(insights)
    print(f"\n  [Chunk 1/2] Prompt : {len(prompt1)} chars (~{len(prompt1)//4} tokens)")
    print(f"  [Chunk 1/2] Envoi a Gemini ({config.GEMINI_MODEL})...")
    part1 = _gemini_call(client, gen_cfg, prompt1, label="chunk 1/2")

    # Sauvegarde intermédiaire du chunk 1
    chunk1_path = config.ROOT / "plan_marketing_chunk1.md"
    chunk1_path.write_text(part1, encoding="utf-8")
    print(f"  [Chunk 1/2] Sauvegarde intermediaire : {chunk1_path.name}")

    # ── Appel 2 : Sections 3, 4 & 5 ─────────────────────────────────────────
    prompt2 = build_prompt_part2(insights)
    print(f"\n  [Chunk 2/2] Prompt : {len(prompt2)} chars (~{len(prompt2)//4} tokens)")
    print(f"  [Chunk 2/2] Envoi a Gemini ({config.GEMINI_MODEL})...")
    part2 = _gemini_call(client, gen_cfg, prompt2, label="chunk 2/2")

    # Fusion des deux parties
    separator = "\n\n---\n\n"
    full_plan = part1 + separator + part2

    # Nettoyage du chunk intermédiaire
    if chunk1_path.exists():
        chunk1_path.unlink()

    return full_plan


def generate_gemini(insights: dict) -> str:
    """
    Point d'entrée principal. Essaie d'abord 1 seul appel,
    puis bascule automatiquement sur 2 appels si tronqué.
    Contraintes respectées : RPM=5 (13s entre appels), RPD=20 (compteur).
    """
    print("\n  Strategy : mode 1 appel (economique RPD)")
    try:
        return generate_gemini_single(insights)
    except LLMError as e:
        err = str(e)
        if "tronqu" in err or "chunk" in err.lower():
            print(f"  [Info] {err}")
            print("  Basculement vers mode 2 appels (chunked)...")
            return generate_gemini_chunked(insights)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# AUTRES BACKENDS (inchangés)
# ══════════════════════════════════════════════════════════════════════════════

def generate_openrouter(prompt: str) -> str:
    """Appel a OpenRouter (API compatible OpenAI). Cle : OPENROUTER_KEY dans .env."""
    import urllib.request
    import urllib.error

    api_key = os.environ.get("OPENROUTER_KEY")
    if not api_key:
        raise LLMError("OPENROUTER_KEY manquante dans .env")

    payload = json.dumps({
        "model": config.OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": "Tu es un expert en marketing digital e-commerce."},
            {"role": "user", "content": prompt},
        ],
        "temperature": config.OPENROUTER_TEMPERATURE,
        "max_tokens": config.OPENROUTER_MAX_TOKENS,
    }).encode("utf-8")

    req = urllib.request.Request(
        config.OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://localhost/mt-pipeline",
            "X-Title": "Marketing Pipeline",
        },
        method="POST",
    )

    print(f"  Envoi a OpenRouter ({config.OPENROUTER_MODEL})...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < max_retries - 1:
                wait_s = 30 * (attempt + 1)
                print(f"  [429] Pause {wait_s}s (retry {attempt+2}/{max_retries})...")
                time.sleep(wait_s)
            else:
                raise LLMError(f"OpenRouter HTTP {e.code}: {body[:300]}")
        except Exception as e:
            raise LLMError(f"Erreur OpenRouter : {e}")
    raise LLMError("Max retries atteint.")


def generate_ollama(prompt: str) -> str:
    """Appel a Ollama en local."""
    import urllib.request

    payload = json.dumps({
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": config.OLLAMA_TEMPERATURE, "num_predict": 4096},
    }).encode("utf-8")

    req = urllib.request.Request(
        config.OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"  Envoi a Ollama '{config.OLLAMA_MODEL}'... (timeout {config.OLLAMA_TIMEOUT_S}s)")
    try:
        with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        raise LLMError(f"Ollama inaccessible : {e}")
    return json.loads(raw).get("response", "").strip()


def generate_openai(prompt: str) -> str:
    """Appel a l'API OpenAI."""
    try:
        import openai
    except ImportError:
        raise LLMError("Package 'openai' non installe. Lancez : python -m pip install openai")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY manquante dans .env")
    client = openai.OpenAI(api_key=api_key)
    print(f"  Envoi a OpenAI ({config.OPENAI_MODEL})...")
    response = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Tu es un expert en marketing digital e-commerce."},
            {"role": "user",   "content": prompt},
        ],
        temperature=config.OPENAI_TEMPERATURE,
        max_tokens=config.OPENAI_MAX_TOKENS,
    )
    return response.choices[0].message.content.strip()


def generate_anthropic(prompt: str) -> str:
    """Appel a l'API Anthropic."""
    try:
        import anthropic
    except ImportError:
        raise LLMError("Package 'anthropic' non installe. Lancez : python -m pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY manquante dans .env")
    client = anthropic.Anthropic(api_key=api_key)
    print(f"  Envoi a Anthropic ({config.ANTHROPIC_MODEL})...")
    message = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.ANTHROPIC_MAX_TOKENS,
        temperature=config.ANTHROPIC_TEMPERATURE,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ══════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════

def generate(insights: dict) -> str:
    """
    Dispatche vers le bon backend selon config.LLM_BACKEND.
    Pour Gemini : passe directement les insights (gestion interne du prompt).
    Pour les autres : construit le prompt complet avant l'appel.
    """
    backend = config.LLM_BACKEND.lower()

    if backend == "gemini":
        return generate_gemini(insights)

    # Pour les autres backends, on construit le prompt unique
    prompt = build_prompt_single(insights)
    prompt_path = config.ROOT / "prompt_debug.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    print(f"[OK] Prompt sauvegarde pour debug : {prompt_path}")

    if backend == "openrouter":
        return generate_openrouter(prompt)
    elif backend == "ollama":
        return generate_ollama(prompt)
    elif backend == "openai":
        return generate_openai(prompt)
    elif backend == "anthropic":
        return generate_anthropic(prompt)
    else:
        raise LLMError(
            f"Backend inconnu : '{config.LLM_BACKEND}'. "
            "Valeurs valides : 'gemini', 'openrouter', 'ollama', 'openai', 'anthropic'."
        )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 65)
    print("GENERATION DU PLAN MARKETING — GA4 iam.ma")
    print(f"Backend LLM : {config.LLM_BACKEND.upper()}")
    if config.LLM_BACKEND.lower() == "gemini":
        quota = _load_quota()
        print(f"Quota Gemini aujourd'hui : {quota['calls']}/{20} RPD utilises")
        print(f"Modele : {config.GEMINI_MODEL} | RPM=5 | RPD=20 | TPM=250k")
    print("=" * 65)

    # ── Charge les insights ────────────────────────────────────────────────────
    if not config.INSIGHTS_JSON.exists():
        print(f"\n[ERREUR] '{config.INSIGHTS_JSON}' introuvable.")
        print("Lancez d'abord : python synthesize_insights.py")
        sys.exit(1)

    with open(config.INSIGHTS_JSON, "r", encoding="utf-8") as f:
        insights = json.load(f)
    print(f"\n[OK] Insights charges depuis : {config.INSIGHTS_JSON}")

    # Pour Gemini, sauvegarde du prompt debug avant l'appel
    if config.LLM_BACKEND.lower() == "gemini":
        prompt_preview = build_prompt_single(insights)
        prompt_path = config.ROOT / "prompt_debug.txt"
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt_preview)
        print(f"[OK] Prompt debug sauvegarde : {prompt_path}")
        print(f"     Taille : {len(prompt_preview)} chars (~{len(prompt_preview)//4} tokens estimes)")

    # ── Appel LLM ──────────────────────────────────────────────────────────────
    print(f"\n[LLM] Generation du plan marketing en cours...")
    t0 = time.time()
    try:
        plan_text = generate(insights)
    except LLMError as e:
        print(f"\n[ERREUR LLM] {e}")
        sys.exit(1)
    elapsed = time.time() - t0
    print(f"\n[OK] Generation terminee en {elapsed:.1f}s ({len(plan_text)} caracteres)")

    # ── Sauvegarde ──────────────────────────────────────────────────────────────
    model_label = getattr(config, config.LLM_BACKEND.upper() + "_MODEL",
                          config.GEMINI_MODEL if config.LLM_BACKEND == "gemini" else "?")
    header = (
        f"# Plan Marketing — Maroc Telecom iam.ma\n\n"
        f"> **Genere le :** {time.strftime('%Y-%m-%d %H:%M')}\n"
        f"> **Backend LLM :** {config.LLM_BACKEND} ({model_label})\n"
        f"> **Donnees :** GA4 exports Jan-Juin 2026\n\n"
        f"---\n\n"
    )
    full_plan = header + plan_text

    with open(config.MARKETING_PLAN_MD, "w", encoding="utf-8") as f:
        f.write(full_plan)

    print(f"\n[OK] Plan marketing sauvegarde : {config.MARKETING_PLAN_MD}")
    print("\n" + "=" * 65)
    print("Livrable pret ! Ouvrez plan_marketing.md dans VS Code.")
    print("=" * 65)


if __name__ == "__main__":
    main()
