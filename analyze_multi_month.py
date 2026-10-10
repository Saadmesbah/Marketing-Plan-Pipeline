"""
Analyse multi-mois des exports GA4 (funnel + revenu produit) — v2
====================================================================

Gère directement les fichiers .xlsx exportés depuis GA4 Explorer, qui
contiennent des lignes de métadonnées avant le vrai tableau (ex :
"# Portail iam.ma", "# Funnel-Funnel exploration 1", "# 20260101-20260131").

Le script :
  - lit .xlsx ET .csv
  - détecte automatiquement où commence le vrai tableau (ligne "Step")
  - détecte automatiquement le type de fichier (funnel ou produit)
  - extrait le mois depuis le nom du fichier
  - calcule les taux de passage réels et l'évolution mensuelle

Utilisation :
    python analyze_multi_month.py "chemin/vers/dossier"

Exemples :
    python analyze_multi_month.py "City"
    python analyze_multi_month.py "Device"
    python analyze_multi_month.py "Purchased item"
"""

import sys
import io
import re
from pathlib import Path

# Force stdout en UTF-8 pour éviter les UnicodeEncodeError sur Windows (console cp1252)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pandas as pd

MONTH_ORDER = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

FUNNEL_KNOWN_COLS = {
    "Step", "Active users", "Completion rate",
    "Abandonments", "Abandonment rate",
}


def extract_month(filename: str) -> str:
    """Extrait le mois ET l'année depuis le nom du fichier (ex :
    'Funnel MT Data Source December 2025' -> 'December 2025'). Garder
    l'année est indispensable ici car les fichiers couvrent deux
    années civiles (ex : Novembre/Decembre 2025 et Janvier-Juin 2026),
    donc deux 'December' ou deux 'April' différents ne doivent pas
    être fusionnés dans le même mois."""
    name_lower = filename.lower()
    year_match = re.search(r"(20\d{2})", name_lower)
    year = year_match.group(1) if year_match else ""

    for month in MONTH_ORDER:
        if month in name_lower:
            label = month.capitalize()
            return f"{label} {year}" if year else label
    return filename


def month_sort_key(label: str):
    """Clé de tri chronologique pour un libellé 'Mois' du type
    'December 2025' ou 'April 2026' (année d'abord, puis mois)."""
    parts = str(label).strip().split()
    month_name = parts[0].lower() if parts else str(label).lower()
    year = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    try:
        month_idx = MONTH_ORDER.index(month_name)
    except ValueError:
        month_idx = 99
    return (year, month_idx)


import csv


def find_header_row_csv(path: Path) -> int:
    """Scanne le fichier texte ligne par ligne pour trouver l'index de la
    ligne d'en-tête (celle qui commence par 'Step' ou 'Item name')."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            first_field = row[0].strip() if row else ""
            if first_field in ("Step", "Item name"):
                return i
    raise ValueError("Ligne d'en-tête (Step / Item name) introuvable dans le fichier CSV.")


def read_csv_flexible(path: Path, header_row_idx: int) -> pd.DataFrame:
    """Lit un CSV en tolérant les lignes ayant un nombre de colonnes
    différent de l'en-tête (ex : ligne 'Grand total' avec une colonne en
    plus). Les colonnes en trop sont ignorées, les colonnes manquantes
    sont remplies avec des valeurs vides."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    header = [h.strip() for h in rows[header_row_idx]]
    ncols = len(header)

    data = []
    for row in rows[header_row_idx + 1:]:
        if not row or all(c.strip() == "" for c in row):
            continue
        if len(row) < ncols:
            row = row + [""] * (ncols - len(row))
        elif len(row) > ncols:
            row = row[:ncols]
        data.append(row)

    df = pd.DataFrame(data, columns=header)

    # Conversion numérique uniquement des colonnes numériques connues
    # (les autres colonnes, comme la dimension City/Device/Canal, restent du texte)
    numeric_cols = {
        "Active users", "Completion rate", "Abandonments", "Abandonment rate",
        "Item revenue", "Total purchasers",
    }
    for col in df.columns:
        if col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def read_ga4_export(path: Path) -> pd.DataFrame:
    """Lit un export GA4 (.xlsx ou .csv), en sautant automatiquement
    les lignes de métadonnées avant le vrai tableau, et en tolérant les
    lignes mal formées (ex : ligne 'Grand total' avec une colonne en plus)."""
    if path.suffix.lower() == ".xlsx":
        raw = pd.read_excel(path, header=None)
        header_row_idx = None
        for i in range(min(len(raw), 30)):
            first_cell = str(raw.iloc[i, 0]).strip()
            if first_cell in ("Step", "Item name"):
                header_row_idx = i
                break
        if header_row_idx is None:
            raise ValueError(
                f"Impossible de trouver la ligne d'en-tête (Step / Item name) dans {path.name}"
            )
        df = pd.read_excel(path, header=header_row_idx)
        df.columns = [str(c).strip() for c in df.columns]
    else:
        header_row_idx = find_header_row_csv(path)
        df = read_csv_flexible(path, header_row_idx)

    df = df.dropna(how="all")
    # Supprime la ligne "Grand total" et les lignes vides sur la 1ère colonne
    first_col = df.columns[0]
    df = df[
        df[first_col].notna()
        & (df[first_col].astype(str).str.strip() != "")
        & (~df[first_col].astype(str).str.strip().str.lower().str.contains("grand total", na=False))
    ]
    return df


def detect_file_type(df: pd.DataFrame) -> str:
    cols = set(df.columns)
    if "Step" in cols:
        return "funnel"
    if {"Item name", "Item revenue"}.issubset(cols):
        return "item"
    raise ValueError(f"Type de fichier non reconnu. Colonnes trouvées : {list(df.columns)}")


def detect_dimension_column_funnel(df: pd.DataFrame) -> str:
    candidates = [c for c in df.columns if c not in FUNNEL_KNOWN_COLS]
    if not candidates:
        raise ValueError(f"Dimension non détectée. Colonnes : {list(df.columns)}")
    return candidates[0]


# ---------------------------------------------------------------------------
# TRAITEMENT FUNNEL
# ---------------------------------------------------------------------------

def process_funnel_file(df: pd.DataFrame, month: str):
    dim_col = detect_dimension_column_funnel(df)
    df = df.rename(columns={dim_col: "Dimension"})
    df = df.sort_values(["Dimension", "Step"]).copy()

    results = []
    for cat, group in df.groupby("Dimension"):
        group = group.sort_values("Step").reset_index(drop=True)
        for i in range(len(group)):
            row = group.iloc[i]
            if i == 0:
                taux_passage = None
                perte = None
            else:
                prev_users = group.iloc[i - 1]["Active users"]
                curr_users = row["Active users"]
                taux_passage = curr_users / prev_users if prev_users else None
                perte = prev_users - curr_users

            results.append({
                "Mois": month,
                "Dimension": cat,
                "Step": row["Step"],
                "Active users": row["Active users"],
                "Taux de passage": taux_passage,
                "Taux d'abandon": (1 - taux_passage) if taux_passage is not None else None,
                "Utilisateurs perdus": perte,
            })
    return pd.DataFrame(results), dim_col


def summarize_funnel(all_months_df: pd.DataFrame, dim_col_name: str, verbose: bool = False):
    valid = all_months_df.dropna(subset=["Taux d'abandon"])

    if verbose:
        print("\n" + "=" * 75)
        print(f"ÉVOLUTION DU TAUX D'ABANDON PAR MOIS — dimension : {dim_col_name}")
        print("=" * 75)

        for cat in sorted(valid["Dimension"].astype(str).unique()):
            print(f"\n--- {dim_col_name} = {cat} ---")
            sub = valid[valid["Dimension"] == cat]
            pivot = sub.pivot_table(
                index="Step", columns="Mois", values="Taux d'abandon", aggfunc="mean"
            )
            pivot = pivot[sorted(pivot.columns, key=month_sort_key)]
            print((pivot * 100).round(1).to_string())

        print("\n" + "=" * 75)
        print("PIRE ÉTAPE PAR DIMENSION (sur l'ensemble des mois confondus)")
        print("=" * 75)
        worst = valid.loc[valid.groupby("Dimension")["Taux d'abandon"].idxmax()]
        for _, row in worst.iterrows():
            taux = row["Taux d'abandon"] * 100
            print(
                f"  {dim_col_name} = {row['Dimension']:<15} -> pire etape : {row['Step']} "
                f"(abandon {taux:.1f}%, mois : {row['Mois']})"
            )


# ---------------------------------------------------------------------------
# TRAITEMENT PRODUIT / REVENU
# ---------------------------------------------------------------------------

def process_item_file(df: pd.DataFrame, month: str) -> pd.DataFrame:
    df = df.copy()
    df["Mois"] = month
    df["Taux de conversion"] = df["Total purchasers"] / df["Active users"]
    df["Revenu moyen par acheteur"] = df["Item revenue"] / df["Total purchasers"]
    return df


def summarize_items(all_months_df: pd.DataFrame, verbose: bool = False):
    total_by_item = (
        all_months_df.groupby("Item name")
        .agg(
            Revenu_total=("Item revenue", "sum"),
            Acheteurs_total=("Total purchasers", "sum"),
            Utilisateurs_total=("Active users", "sum"),
        )
        .sort_values("Revenu_total", ascending=False)
    )
    total_by_item["Taux de conversion moyen"] = (
        total_by_item["Acheteurs_total"] / total_by_item["Utilisateurs_total"]
    )

    if verbose:
        print("\n" + "=" * 75)
        print("CLASSEMENT DES PRODUITS PAR REVENU TOTAL (toute la période)")
        print("=" * 75)
        print(total_by_item.round(3).to_string())

        print("\n" + "=" * 75)
        print("PRODUITS AVEC LE PLUS FAIBLE TAUX DE CONVERSION")
        print("=" * 75)
        print(total_by_item.sort_values("Taux de conversion moyen").head(5).round(3).to_string())

        print("\n" + "=" * 75)
        print("ÉVOLUTION MENSUELLE DU REVENU PAR PRODUIT")
        print("=" * 75)
        pivot = all_months_df.pivot_table(
            index="Item name", columns="Mois", values="Item revenue", aggfunc="sum"
        )
        pivot = pivot[sorted(pivot.columns, key=month_sort_key)]
        print(pivot.round(0).to_string())


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    args = [a for a in sys.argv[1:] if a != "--verbose"]
    verbose = "--verbose" in sys.argv[1:]

    if len(args) < 1:
        print('Usage : python analyze_multi_month.py "chemin/vers/dossier" [--verbose]')
        sys.exit(1)

    folder = Path(args[0])
    if not folder.is_dir():
        print(f"[ERREUR] '{folder}' n'est pas un dossier valide.")
        sys.exit(1)

    files = sorted(folder.glob("*.xlsx")) + sorted(folder.glob("*.csv"))
    if not files:
        print(f"[ERREUR] Aucun fichier .xlsx ou .csv trouvé dans {folder}")
        sys.exit(1)

    if verbose:
        print(f"{len(files)} fichier(s) trouvé(s) dans '{folder.name}' :")
        for f in files:
            print(f"  - {f.name}")

    funnel_frames = []
    item_frames = []
    dim_col_name = None
    had_error = False

    for f in files:
        month = extract_month(f.name)
        try:
            df_raw = read_ga4_export(f)
            ftype = detect_file_type(df_raw)
        except Exception as e:
            had_error = True
            print(f"[ERREUR] Impossible de traiter {f.name} : {e}")
            continue

        if ftype == "funnel":
            processed, dim_col_name = process_funnel_file(df_raw, month)
            funnel_frames.append(processed)
        else:
            processed = process_item_file(df_raw, month)
            item_frames.append(processed)

    out_dir = folder / "resultats_analyse"
    out_dir.mkdir(exist_ok=True)

    if funnel_frames:
        all_funnel = pd.concat(funnel_frames, ignore_index=True)
        summarize_funnel(all_funnel, dim_col_name or "Dimension", verbose=verbose)
        out_path = out_dir / f"analyse_funnel_{folder.name}.csv"
        all_funnel.to_csv(out_path, index=False, encoding="utf-8-sig")

    if item_frames:
        all_items = pd.concat(item_frames, ignore_index=True)
        summarize_items(all_items, verbose=verbose)
        out_path = out_dir / f"analyse_produits_{folder.name}.csv"
        all_items.to_csv(out_path, index=False, encoding="utf-8-sig")

    if had_error:
        print("Terminé avec erreur(s) — voir ci-dessus.")
    else:
        print(f"Terminé. Résultats sauvegardés dans : {out_dir}")


if __name__ == "__main__":
    main()