"""
synthesize_insights.py — Synthèse cross-dimensions GA4 (Maroc Telecom)
=======================================================================

Lit les fichiers CSV générés par analyze_multi_month_copy.py dans chaque
sous-dossier resultats_analyse/, croise les signaux, et produit :

  1. insights_consolides.json   → entrée pour le LLM (generate_marketing_plan.py)
  2. rapport_insights.md        → rapport lisible pour l'humain

Utilisation :
    python synthesize_insights.py

(Le script cherche automatiquement les dossiers depuis le répertoire courant.)
"""

import io
import json
import sys
from pathlib import Path

import pandas as pd

# ── Console UTF-8 (Windows cp1252 fix) ────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Configuration ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent          # dossier du script = racine du projet
OUT_JSON = ROOT / "insights_consolides.json"
OUT_MD   = ROOT / "rapport_insights.md"

MONTH_ORDER = [
    "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "october",
    "november", "december",
]

# Mappage des noms de colonnes → étiquettes lisibles dans le rapport
FUNNEL_STEPS = {
    "1. View item":        "1. Vue produit",
    "2. Add to card":      "2. Ajout panier",
    "3. Begin Checkout":   "3. Début checkout",
    "4. Add Info":         "4. Info livraison/paiement",
    "5. Purchase":         "5. Achat confirmé",
}

DIMENSION_LABELS = {
    "City":               "Ville",
    "Device":             "Appareil",
    "New vs Established": "Type utilisateur",
    "Session Channel":    "Canal d'acquisition",
    "Page name":          "Page produit",
}

# Valeurs de la colonne "Dimension" qui ne sont PAS de vraies catégories à classer :
# "Total" est l'agrégat de toutes les catégories (double-compte si inclus dans un classement),
# "(not set)" est le marqueur GA4 pour une donnée non trackée (pas un vrai segment).
EXCLUDED_DIMENSION_VALUES = {"total", "(not set)", ""}


def _is_excluded(value) -> bool:
    return str(value).strip().lower() in EXCLUDED_DIMENSION_VALUES

# ── Helpers ────────────────────────────────────────────────────────────────────

def sort_months(cols):
    """Trie une liste de libellés 'Mois' (ex : 'December 2025',
    'April 2026') par ordre chronologique réel (année puis mois).
    Nécessaire car la période couvre deux années civiles : un simple
    tri par nom de mois mélangerait Décembre 2025 et un futur
    Décembre d'une autre année, et un tri alphabétique ne respecte
    pas l'ordre calendaire."""
    def key(c):
        parts = str(c).strip().split()
        month_name = parts[0].lower() if parts else str(c).lower()
        year = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        try:
            month_idx = MONTH_ORDER.index(month_name)
        except ValueError:
            month_idx = 99
        return (year, month_idx)

    return sorted(cols, key=key)


def load_funnel_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    # Normalise les types
    for col in ["Active users", "Taux de passage", "Taux d'abandon", "Utilisateurs perdus"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_item_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ["Active users", "Item revenue", "Total purchasers",
                "Taux de conversion", "Revenu moyen par acheteur"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ── Analyse funnel par dimension ───────────────────────────────────────────────

def analyze_funnel_dimension(folder_name: str) -> dict:
    """
    Lit analyse_funnel_<folder_name>.csv et retourne un dict structuré avec :
    - taux d'abandon moyen par étape (sur tous les mois)
    - top 3 étapes critiques (abandon le plus élevé)
    - évolution mensuelle du taux d'abandon à l'étape la plus critique
    - top 3 valeurs de la dimension avec le plus fort abandon global
    """
    csv_path = ROOT / folder_name / "resultats_analyse" / f"analyse_funnel_{folder_name}.csv"
    if not csv_path.exists():
        print(f"  [SKIP] {csv_path.name} introuvable.")
        return {}

    df = load_funnel_csv(csv_path)
    dim_label = DIMENSION_LABELS.get(folder_name, folder_name)

    # ── Taux d'abandon moyen par étape ──────────────────────────────────────────
    # On utilise UNIQUEMENT la ligne "Total" (déjà agrégée correctement par GA4),
    # pas une moyenne naïve entre catégories : une simple moyenne entre villes
    # compterait "Total" en double avec les villes individuelles, et donnerait
    # un poids égal à des villes de tailles très différentes (biais).
    total_rows = df[df["Dimension"].astype(str).str.strip().str.lower() == "total"]
    base_for_step_avg = total_rows if not total_rows.empty else df.dropna(subset=["Taux d'abandon"])

    step_avg = (
        base_for_step_avg.dropna(subset=["Taux d'abandon"])
        .groupby("Step")["Taux d'abandon"]
        .mean()
        .sort_values(ascending=False)
    )
    step_avg_pct = (step_avg * 100).round(1).to_dict()

    # ── Top 3 étapes critiques ──────────────────────────────────────────────────
    top3_steps = list(step_avg_pct.items())[:3]

    # ── Évolution mensuelle de l'étape la plus critique (sur la ligne Total) ────
    worst_step = step_avg.idxmax() if not step_avg.empty else None
    monthly_trend = {}
    if worst_step:
        sub = base_for_step_avg[base_for_step_avg["Step"] == worst_step].dropna(subset=["Taux d'abandon"])
        trend = sub.groupby("Mois")["Taux d'abandon"].mean() * 100
        ordered_months = sort_months(trend.index.tolist())
        monthly_trend = {m: round(float(trend[m]), 1) for m in ordered_months if m in trend}

    # ── Top 3 valeurs de dimension avec le plus fort abandon ───────────────────
    # On exclut "Total" (agrégat, pas une vraie catégorie) et "(not set)"
    # (donnée non trackée par GA4, pas un vrai segment) pour ne pas fausser
    # le classement des catégories réelles.
    df_categories = df[~df["Dimension"].apply(_is_excluded)]
    dim_abandon = (
        df_categories.dropna(subset=["Taux d'abandon"])
        .groupby("Dimension")["Taux d'abandon"]
        .mean()
        .sort_values(ascending=False)
    )
    top3_dim = [(str(k), round(float(v) * 100, 1)) for k, v in dim_abandon.head(3).items()]

    # ── Top 3 valeurs de dimension avec le MEILLEUR taux de passage ─────────────
    best3_dim = [(str(k), round(float(v) * 100, 1)) for k, v in dim_abandon.tail(3).items()]

    return {
        "dimension": dim_label,
        "abandon_moyen_par_etape_pct": step_avg_pct,
        "etapes_critiques_top3": [{"etape": s, "abandon_pct": v} for s, v in top3_steps],
        "etape_la_plus_critique": worst_step,
        "evolution_mensuelle_etape_critique_pct": monthly_trend,
        "top3_dim_fort_abandon": [{"valeur": k, "abandon_moyen_pct": v} for k, v in top3_dim],
        "top3_dim_meilleur_passage": [{"valeur": k, "abandon_moyen_pct": v} for k, v in best3_dim],
    }


# ── Analyse produits ───────────────────────────────────────────────────────────

def analyze_items() -> dict:
    csv_path = ROOT / "Purchased item" / "resultats_analyse" / "analyse_produits_Purchased item.csv"
    if not csv_path.exists():
        print(f"  [SKIP] {csv_path.name} introuvable.")
        return {}

    df = load_item_csv(csv_path)

    # Déduplique les noms de produits (variantes arabes/latins du même produit fusionnées)
    grouped = df.groupby("Item name", as_index=True).agg(
        Revenu_total=("Item revenue", "sum"),
        Acheteurs_total=("Total purchasers", "sum"),
        Utilisateurs_total=("Active users", "sum"),
    )
    grouped["Taux_conversion"] = (
        grouped["Acheteurs_total"] / grouped["Utilisateurs_total"]
    ).round(4)
    grouped["Revenu_moyen_acheteur"] = (
        grouped["Revenu_total"] / grouped["Acheteurs_total"]
    ).round(1)
    grouped = grouped.sort_values("Revenu_total", ascending=False)

    # ── Top 5 produits par revenu ───────────────────────────────────────────────
    top5_rev = grouped.head(5).reset_index()
    top5_rev_list = [
        {
            "produit": row["Item name"],
            "revenu_total_mad": round(float(row["Revenu_total"]), 0),
            "acheteurs": int(row["Acheteurs_total"]),
            "taux_conversion_pct": round(float(row["Taux_conversion"]) * 100, 1),
        }
        for _, row in top5_rev.iterrows()
    ]

    # ── Top 5 produits fort trafic / faible conversion (opportunités) ───────────
    # Filtre : au moins 10 utilisateurs sur la période
    opport = grouped[grouped["Utilisateurs_total"] >= 10].sort_values("Taux_conversion").head(5).reset_index()
    opport_list = [
        {
            "produit": row["Item name"],
            "utilisateurs": int(row["Utilisateurs_total"]),
            "taux_conversion_pct": round(float(row["Taux_conversion"]) * 100, 1),
            "revenu_total_mad": round(float(row["Revenu_total"]), 0),
        }
        for _, row in opport.iterrows()
    ]

    # ── Évolution mensuelle du revenu total ─────────────────────────────────────
    monthly_rev = df.groupby("Mois")["Item revenue"].sum()
    ordered_months = sort_months(monthly_rev.index.tolist())
    monthly_rev_dict = {m: round(float(monthly_rev[m]), 0) for m in ordered_months if m in monthly_rev}

    return {
        "top5_produits_par_revenu": top5_rev_list,
        "top5_opportunites_conversion": opport_list,
        "evolution_revenu_mensuel_mad": monthly_rev_dict,
        "revenu_total_periode_mad": round(float(grouped["Revenu_total"].sum()), 0),
    }


# ── Hypothèses cross-dimensions ────────────────────────────────────────────────

def build_cross_dimension_hypotheses(insights: dict) -> list[str]:
    """
    Génère des hypothèses de segmentation en croisant les signaux
    des différentes dimensions (device × canal × ville).
    """
    hypotheses = []

    # Mobile + fort abandon → hypothèse UX
    dev = insights.get("Device", {})
    top3_dev = dev.get("top3_dim_fort_abandon", [])
    if top3_dev and top3_dev[0]["valeur"].lower() in ("mobile", "tablet"):
        hypotheses.append(
            f"SEGMENT CRITIQUE — Mobile : appareil '{top3_dev[0]['valeur']}' affiche le plus fort abandon "
            f"({top3_dev[0]['abandon_moyen_pct']}%). Couplé aux canaux organiques (trafic majoritairement "
            "mobile), ce segment représente probablement le plus grand gisement de conversions perdues. "
            "Action prioritaire : optimisation UX du tunnel mobile (réduction des frictions au checkout)."
        )

    # Canal avec fort abandon → hypothèse acquisition
    chan = insights.get("Session Channel", {})
    top3_chan = chan.get("top3_dim_fort_abandon", [])
    best3_chan = chan.get("top3_dim_meilleur_passage", [])
    if top3_chan:
        hypotheses.append(
            f"CANAL À FORT ABANDON — '{top3_chan[0]['valeur']}' ({top3_chan[0]['abandon_moyen_pct']}% d'abandon). "
            "Ces sessions ont probablement une intention d'achat plus faible. "
            "Action : retravailler les landing pages et la cohérence message/offre pour ce canal."
        )
    if best3_chan:
        hypotheses.append(
            f"CANAL PERFORMANT — '{best3_chan[0]['valeur']}' est le canal avec le meilleur taux de passage "
            f"({best3_chan[0]['abandon_moyen_pct']}% d'abandon seulement). "
            "Action : augmenter les investissements sur ce canal, créer des campagnes ciblées."
        )

    # Ville avec fort abandon → hypothèse géographique
    city = insights.get("City", {})
    top3_city = city.get("top3_dim_fort_abandon", [])
    if top3_city:
        villes = ", ".join(f"'{h['valeur']}'" for h in top3_city[:3])
        hypotheses.append(
            f"VILLES À RISQUE — {villes} concentrent les abandons les plus élevés. "
            "Hypothèse : problèmes de livraison (délais, couverture) ou manque de confiance dans le paiement en ligne. "
            "Action : promotions ciblées géographiquement, amélioration des options de livraison."
        )

    # Nouveau vs récurrent
    nve = insights.get("New vs Established", {})
    top3_nve = nve.get("top3_dim_fort_abandon", [])
    if top3_nve:
        segment = top3_nve[0]["valeur"]
        hypotheses.append(
            f"SEGMENT '{segment}' — abandon le plus élevé ({top3_nve[0]['abandon_moyen_pct']}%). "
            + (
                "Les nouveaux utilisateurs manquent de confiance → Action : ajouter des éléments de réassurance "
                "(avis clients, garantie, paiement sécurisé) sur les pages de checkout."
                if "new" in segment.lower() else
                "Les utilisateurs établis qui abandonnent ont probablement été freinés par le prix ou une rupture de stock "
                "→ Action : relance par email/SMS avec offre de réduction limitée."
            )
        )

    # Produits opportunités
    items = insights.get("Purchased item", {})
    opport = items.get("top5_opportunites_conversion", [])
    if opport:
        prod = opport[0]
        hypotheses.append(
            f"PRODUIT OPPORTUNITÉ — '{prod['produit']}' : {prod['utilisateurs']} utilisateurs mais seulement "
            f"{prod['taux_conversion_pct']}% de conversion. Fort potentiel inexploité. "
            "Action : page produit enrichie (photos, vidéo démo), offre bundle ou facilité de paiement."
        )

    return hypotheses


# ── Génération du rapport Markdown ────────────────────────────────────────────

def build_markdown_report(insights: dict, hypotheses: list[str]) -> str:
    lines = [
        "# Rapport d'Insights GA4 — Maroc Telecom (iam.ma)",
        "",
        "> **Période analysée :** Janvier – Juin 2026  ",
        "> **Source :** Exports manuels GA4 Explorer (funnel + monétisation)  ",
        "> **Tunnel :** Vue produit → Ajout panier → Checkout → Info livraison/paiement → Achat",
        "",
        "---",
        "",
    ]

    # ── Section 1 : Funnel par dimension ───────────────────────────────────────
    lines += ["## 1. Analyse du Tunnel par Dimension", ""]

    for folder_name, dim_data in insights.items():
        if folder_name == "Purchased item" or not dim_data:
            continue

        lines += [
            f"### {dim_data.get('dimension', folder_name)}",
            "",
        ]

        etape_critique = dim_data.get("etape_la_plus_critique", "—")
        lines.append(f"**Étape la plus critique :** `{etape_critique}`")
        lines.append("")

        lines.append("**Taux d'abandon moyen par étape (tous mois confondus) :**")
        lines.append("")
        lines.append("| Étape | Abandon moyen |")
        lines.append("|---|---|")
        for step, val in dim_data.get("abandon_moyen_par_etape_pct", {}).items():
            lines.append(f"| {step} | {val}% |")
        lines.append("")

        top3 = dim_data.get("top3_dim_fort_abandon", [])
        if top3:
            lines.append(f"**Top 3 valeurs avec le plus fort abandon ({dim_data.get('dimension', '')}) :**")
            lines.append("")
            for item in top3:
                lines.append(f"- `{item['valeur']}` → {item['abandon_moyen_pct']}%")
            lines.append("")

        trend = dim_data.get("evolution_mensuelle_etape_critique_pct", {})
        if trend:
            lines.append(f"**Évolution mensuelle (étape `{etape_critique}`) :**")
            lines.append("")
            lines.append("| Mois | Abandon % |")
            lines.append("|---|---|")
            for mois, val in trend.items():
                lines.append(f"| {mois.capitalize()} | {val}% |")
            lines.append("")

        lines.append("---")
        lines.append("")

    # ── Section 2 : Produits ───────────────────────────────────────────────────
    items = insights.get("Purchased item", {})
    if items:
        lines += ["## 2. Analyse Produits & Revenus", ""]

        rev_total = items.get("revenu_total_periode_mad", 0)
        lines.append(f"**Revenu total période (Jan–Juin) :** {rev_total:,.0f} MAD")
        lines.append("")

        lines.append("**Évolution mensuelle du revenu total :**")
        lines.append("")
        lines.append("| Mois | Revenu MAD |")
        lines.append("|---|---|")
        for mois, val in items.get("evolution_revenu_mensuel_mad", {}).items():
            lines.append(f"| {mois.capitalize()} | {val:,.0f} |")
        lines.append("")

        lines.append("**Top 5 produits par revenu :**")
        lines.append("")
        lines.append("| Produit | Revenu MAD | Acheteurs | Taux conversion |")
        lines.append("|---|---|---|---|")
        for p in items.get("top5_produits_par_revenu", []):
            lines.append(
                f"| {p['produit']} | {p['revenu_total_mad']:,.0f} | "
                f"{p['acheteurs']} | {p['taux_conversion_pct']}% |"
            )
        lines.append("")

        lines.append("**Top 5 opportunités (fort trafic, faible conversion) :**")
        lines.append("")
        lines.append("| Produit | Utilisateurs | Conversion | Revenu MAD |")
        lines.append("|---|---|---|---|")
        for p in items.get("top5_opportunites_conversion", []):
            lines.append(
                f"| {p['produit']} | {p['utilisateurs']} | "
                f"{p['taux_conversion_pct']}% | {p['revenu_total_mad']:,.0f} |"
            )
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── Section 3 : Hypothèses cross-dimensions ────────────────────────────────
    lines += ["## 3. Hypothèses Cross-Dimensions (Segmentation Indirecte)", ""]
    if hypotheses:
        for i, h in enumerate(hypotheses, 1):
            lines.append(f"### Hypothèse {i}")
            lines.append("")
            lines.append(h)
            lines.append("")
    else:
        lines.append("*(Pas assez de données pour générer des hypothèses.)*")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Rapport généré automatiquement par `synthesize_insights.py`*")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("SYNTHÈSE CROSS-DIMENSIONS — GA4 iam.ma (Jan–Juin 2026)")
    print("=" * 65)

    # Dossiers funnel (dans l'ordre voulu pour le rapport)
    funnel_folders = [
        "City",
        "Device",
        "Session Channel",
        "New vs Established",
        "Page name",
    ]

    insights: dict = {}

    # ── Analyse funnel ─────────────────────────────────────────────────────────
    for folder in funnel_folders:
        print(f"\n[{folder}] Chargement...")
        result = analyze_funnel_dimension(folder)
        if result:
            insights[folder] = result
            etape = result.get("etape_la_plus_critique", "?")
            top3 = result.get("etapes_critiques_top3", [])
            print(f"  -> Étape critique : {etape}")
            if top3:
                print(f"  -> Top abandons : " + " | ".join(f"{e['etape']} ({e['abandon_pct']}%)" for e in top3))

    # ── Analyse produits ───────────────────────────────────────────────────────
    print("\n[Purchased item] Chargement...")
    item_result = analyze_items()
    if item_result:
        insights["Purchased item"] = item_result
        rev = item_result.get("revenu_total_periode_mad", 0)
        top1 = item_result.get("top5_produits_par_revenu", [{}])[0]
        print(f"  -> Revenu total : {rev:,.0f} MAD")
        print(f"  -> Produit #1  : {top1.get('produit', '?')} ({top1.get('revenu_total_mad', 0):,.0f} MAD)")

    # ── Hypothèses cross-dimensions ────────────────────────────────────────────
    print("\n[Synthèse] Génération des hypothèses cross-dimensions...")
    hypotheses = build_cross_dimension_hypotheses(insights)
    print(f"  -> {len(hypotheses)} hypothèse(s) générée(s).")

    # ── Sauvegarde JSON ─────────────────────────────────────────────────────────
    payload = {
        "metadata": {
            "periode": "Janvier–Juin 2026",
            "portail": "iam.ma",
            "tunnel": "view_item -> add_to_cart -> begin_checkout -> add_shipping_info -> add_payment_info -> purchase",
            "generated_by": "synthesize_insights.py",
        },
        "dimensions": insights,
        "hypotheses_cross_dimensions": hypotheses,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] Insights JSON sauvegardé : {OUT_JSON}")

    # ── Sauvegarde Markdown ─────────────────────────────────────────────────────
    md_content = build_markdown_report(insights, hypotheses)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"[OK] Rapport Markdown sauvegardé  : {OUT_MD}")

    print("\n" + "=" * 65)
    print("Prochaine étape : python generate_marketing_plan.py")
    print("=" * 65)


if __name__ == "__main__":
    main()