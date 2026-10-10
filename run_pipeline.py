"""
run_pipeline.py — Orchestrateur du pipeline complet GA4 → Plan Marketing
=========================================================================

Enchaîne les 3 étapes du pipeline :
  1. Analyse par dossier  (analyze_multi_month_copy.py sur chaque dossier)
  2. Synthèse             (synthesize_insights.py → insights_consolides.json)
  3. Plan marketing LLM   (generate_marketing_plan.py → plan_marketing.md)

Utilisation :
    python run_pipeline.py [OPTIONS]

Options :
    --skip-analysis       Saute l'étape 1 (si les resultats_analyse/ sont déjà là)
    --skip-synthesis      Saute l'étape 2 (si insights_consolides.json existe déjà)
    --llm BACKEND         Surcharge le LLM backend de config.py (ollama|openai|anthropic)
    --folder DOSSIER      Analyse uniquement ce dossier (ex: --folder Device)
    --no-llm              Saute la génération LLM (utile pour tester les étapes 1+2)

Exemple :
    python run_pipeline.py --skip-analysis --llm openai
    python run_pipeline.py --no-llm
    python run_pipeline.py
"""

import argparse
import io
import subprocess
import sys
import time
from pathlib import Path

# ── Console UTF-8 ──────────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent

ALL_FOLDERS = [
    "City",
    "Device",
    "New vs Established",
    "Session Channel",
    "Page name",
    "Purchased item",
    "Session source",
]


def run_step(label: str, args: list, cwd: Path) -> bool:
    """Lance un sous-processus et affiche sa sortie en temps réel."""
    print(f"\n{'='*65}")
    print(f"  ÉTAPE : {label}")
    print(f"{'='*65}")
    t0 = time.time()
    result = subprocess.run(
        args,
        cwd=str(cwd),
        stdout=sys.stdout,
        stderr=sys.stdout,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"\n[ERREUR] L'étape '{label}' a échoué (code {result.returncode}) après {elapsed:.1f}s.")
        return False
    print(f"\n[OK] '{label}' terminée en {elapsed:.1f}s.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline GA4 → Insights → Plan Marketing (Maroc Telecom)"
    )
    parser.add_argument(
        "--skip-analysis", action="store_true",
        help="Sauter l'analyse par dossier (étape 1)"
    )
    parser.add_argument(
        "--skip-synthesis", action="store_true",
        help="Sauter la synthèse (étape 2)"
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Sauter la génération LLM (étape 3)"
    )
    parser.add_argument(
        "--llm", type=str, default=None,
        choices=["openrouter", "gemini", "ollama", "openai", "anthropic"],
        help="Surcharger le backend LLM de config.py"
    )
    parser.add_argument(
        "--folder", type=str, default=None,
        help="Analyser uniquement ce dossier (ex: Device)"
    )
    args = parser.parse_args()

    print("=" * 65)
    print("  PIPELINE GA4 — MAROC TELECOM iam.ma")
    print("=" * 65)

    # ── Surcharge du backend LLM si demandé ───────────────────────────────────
    if args.llm:
        import config
        config.LLM_BACKEND = args.llm
        print(f"  [Config] LLM_BACKEND surchargé : {args.llm}")

    # ── ÉTAPE 1 : Analyse par dossier ─────────────────────────────────────────
    if not args.skip_analysis:
        folders = [args.folder] if args.folder else ALL_FOLDERS
        for folder in folders:
            folder_path = ROOT / folder
            if not folder_path.is_dir():
                print(f"  [SKIP] Dossier '{folder}' introuvable.")
                continue
            ok = run_step(
                label=f"Analyse — {folder}",
                args=[sys.executable, str(ROOT / "analyze_multi_month.py"), folder],
                cwd=ROOT,
            )
            if not ok:
                print("  [ATTENTION] L'analyse a échoué sur ce dossier — on continue avec les suivants.")
    else:
        print("\n[SKIP] Étape 1 (analyse) ignorée.")

    # ── ÉTAPE 2 : Synthèse cross-dimensions ───────────────────────────────────
    if not args.skip_synthesis:
        ok = run_step(
            label="Synthèse cross-dimensions",
            args=[sys.executable, str(ROOT / "synthesize_insights.py")],
            cwd=ROOT,
        )
        if not ok:
            print("\n[ERREUR FATALE] La synthèse a échoué. Arrêt du pipeline.")
            sys.exit(1)
    else:
        print("\n[SKIP] Étape 2 (synthèse) ignorée.")

    # ── ÉTAPE 3 : Génération du plan marketing ────────────────────────────────
    if not args.no_llm:
        ok = run_step(
            label=f"Génération plan marketing (LLM)",
            args=[sys.executable, str(ROOT / "generate_marketing_plan.py")],
            cwd=ROOT,
        )
        if not ok:
            print("\n[ERREUR] La génération LLM a échoué.")
            print("  → Vérifiez config.py et que le service LLM est accessible.")
            sys.exit(1)
    else:
        print("\n[SKIP] Étape 3 (LLM) ignorée.")

    # ── Résumé des fichiers produits ──────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  PIPELINE TERMINÉ — Fichiers produits :")
    print("=" * 65)
    outputs = [
        ROOT / "insights_consolides.json",
        ROOT / "rapport_insights.md",
        ROOT / "plan_marketing.md",
        ROOT / "prompt_debug.txt",
    ]
    for p in outputs:
        status = "OK" if p.exists() else "ABSENT"
        size   = f"{p.stat().st_size:,} octets" if p.exists() else "—"
        print(f"  [{status}] {p.name:<35} {size}")
    print("=" * 65)


if __name__ == "__main__":
    main()
