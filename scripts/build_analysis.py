"""
build_analysis.py
-----------------
Scrapes, cleans, validates, and analyzes WNBA vs NBA shooting accuracy data.
Produces clean CSVs, summary stats, and publication-ready plots.

Data source: Basketball Reference (2022-23 season)
  NBA:  https://www.basketball-reference.com/leagues/NBA_2023_per_game.html
  WNBA: https://www.basketball-reference.com/wnba/years/2023_per_game.html

Usage:
    python3 build_analysis.py

Outputs:
    data/nba_clean.csv
    data/wnba_clean.csv
    data/combined_clean.csv
    images/shooting_distributions.png
    images/shooting_boxplots.png
    data/analysis_results.txt
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import seaborn as sns
from scipy import stats
import warnings
import sys
import os

warnings.filterwarnings("ignore")

# ── 0. CONSTANTS ──────────────────────────────────────────────────────────────

NBA_URL  = "https://www.basketball-reference.com/leagues/NBA_2023_per_game.html"
WNBA_URL = "https://www.basketball-reference.com/wnba/years/2023_per_game.html"

PCT_COLS  = ["FG%", "FT%", "3P%"]
MIN_GAMES = 10   # minimum games played to be included in analysis

# Color palette — matches site aesthetic
COLOR_NBA  = "#2ABFBF"   # teal
COLOR_WNBA = "#F27D9D"   # pink
BG_COLOR   = "#F7F5F0"
INK_COLOR  = "#1A1A18"
INK_MID    = "#4A4A46"

# ── 1. SCRAPE ─────────────────────────────────────────────────────────────────

def scrape(url, league):
    """Scrape per-game stats table from Basketball Reference."""
    print(f"Scraping {league} data from Basketball Reference...", end=" ")
    try:
        tables = pd.read_html(url)
        df = tables[0].copy()
        df["League"] = league
        print(f"OK — {len(df)} rows")
        return df
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)

# ── 2. CLEAN ──────────────────────────────────────────────────────────────────

def clean(df, league):
    """
    Clean a raw Basketball Reference per-game table.

    Steps:
      1. Remove repeated header rows (Basketball Reference injects these
         every 20 rows; they have 'Player' in the Player column)
      2. Parse percentage columns to float
      3. Parse games played to int and filter minimum threshold
      4. Deduplicate traded players — keep the row with the most games played.
         Basketball Reference lists traded players once per team plus a 'TOT'
         summary row. We keep TOT (or the highest-G row if TOT is absent).
    """
    print(f"Cleaning {league} data...", end=" ")

    # Drop injected header rows
    df = df[df["Player"] != "Player"].copy()
    df = df[df["Player"].notna()].copy()

    # Parse games played
    df["G"] = pd.to_numeric(df["G"], errors="coerce")
    df = df[df["G"] >= MIN_GAMES].copy()

    # Parse percentage columns
    for col in PCT_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop rows missing all three shooting stats
    df = df.dropna(subset=PCT_COLS, how="all").copy()

    # Deduplicate: for players on multiple teams, keep TOT row if present,
    # otherwise keep the row with the most games played.
    def dedup_player(group):
        if len(group) == 1:
            return group
        # Basketball Reference uses 'TOT' as team code for traded players
        tot_rows = group[group.get("Team", group.get("Tm", "")) == "TOT"]
        if len(tot_rows) > 0:
            return tot_rows.iloc[[0]]
        # Fallback: keep row with most games
        return group.sort_values("G", ascending=False).iloc[[0]]

    team_col = "Team" if "Team" in df.columns else "Tm"
    before = len(df)
    df = df.groupby("Player", group_keys=False).apply(dedup_player).reset_index(drop=True)
    after = len(df)
    dupes_removed = before - after

    print(f"OK — {after} players ({dupes_removed} duplicate rows removed)")

    # Validation: percentages should be between 0 and 1
    for col in PCT_COLS:
        if col in df.columns:
            out_of_range = ((df[col] < 0) | (df[col] > 1)).sum()
            if out_of_range > 0:
                print(f"  WARNING: {out_of_range} out-of-range values in {col} — setting to NaN")
                df.loc[(df[col] < 0) | (df[col] > 1), col] = np.nan

    return df[["Player", team_col, "G"] + [c for c in PCT_COLS if c in df.columns] + ["League"]].copy()

# ── 3. VALIDATE ───────────────────────────────────────────────────────────────

def validate(nba, wnba):
    """Print a quick sanity check of the cleaned data."""
    print("\n── Validation ──────────────────────────────────────────────────")
    for league, df in [("NBA", nba), ("WNBA", wnba)]:
        print(f"\n{league}  (n={len(df)})")
        print(df[PCT_COLS].describe().round(3).to_string())

    # Flag anything that looks wrong
    issues = []
    for col in PCT_COLS:
        nba_mean  = nba[col].mean()
        wnba_mean = wnba[col].mean()
        if not (0.3 < nba_mean < 0.6):
            issues.append(f"NBA {col} mean ({nba_mean:.3f}) outside expected range")
        if not (0.3 < wnba_mean < 0.6):
            issues.append(f"WNBA {col} mean ({wnba_mean:.3f}) outside expected range")

    if issues:
        print("\n⚠️  Potential data issues:")
        for issue in issues:
            print(f"   {issue}")
    else:
        print("\n✅  All shooting percentage means within expected range (0.30–0.60)")

# ── 4. ANALYSIS ───────────────────────────────────────────────────────────────

def cohen_d(a, b):
    """Compute Cohen's d effect size between two arrays."""
    a = a.dropna()
    b = b.dropna()
    pooled_std = np.sqrt((a.std()**2 + b.std()**2) / 2)
    return (a.mean() - b.mean()) / pooled_std

def effect_label(d):
    d = abs(d)
    if d < 0.2:  return "negligible"
    if d < 0.5:  return "small"
    if d < 0.8:  return "medium"
    return "large"

def run_analysis(nba, wnba):
    """Run statistical tests and return results dict."""
    print("\n── Statistical Analysis ────────────────────────────────────────")

    results = {}
    lines   = []
    lines.append("WNBA vs NBA Shooting Accuracy Analysis — 2022-23 Season")
    lines.append(f"NBA n={len(nba)}  |  WNBA n={len(wnba)}")
    lines.append(f"Minimum games played threshold: {MIN_GAMES}")
    lines.append("=" * 60)

    for col in PCT_COLS:
        nba_vals  = nba[col].dropna()
        wnba_vals = wnba[col].dropna()

        # Normality (Shapiro-Wilk — use sample if n > 5000)
        _, p_norm_nba  = stats.shapiro(nba_vals.sample(min(len(nba_vals), 5000),
                                                        random_state=42))
        _, p_norm_wnba = stats.shapiro(wnba_vals.sample(min(len(wnba_vals), 5000),
                                                         random_state=42))
        normal = (p_norm_nba > 0.05) and (p_norm_wnba > 0.05)

        # Variance equality (Levene)
        _, p_levene = stats.levene(nba_vals, wnba_vals)
        equal_var = p_levene > 0.05

        # Choose appropriate test
        if normal and equal_var:
            test_name = "Independent t-test"
            stat, p_val = stats.ttest_ind(nba_vals, wnba_vals, equal_var=True)
        elif normal and not equal_var:
            test_name = "Welch t-test"
            stat, p_val = stats.ttest_ind(nba_vals, wnba_vals, equal_var=False)
        else:
            test_name = "Mann-Whitney U"
            stat, p_val = stats.mannwhitneyu(nba_vals, wnba_vals,
                                              alternative="two-sided")

        d = cohen_d(nba_vals, wnba_vals)

        results[col] = {
            "nba_mean":   nba_vals.mean(),
            "wnba_mean":  wnba_vals.mean(),
            "nba_std":    nba_vals.std(),
            "wnba_std":   wnba_vals.std(),
            "test":       test_name,
            "stat":       stat,
            "p_value":    p_val,
            "cohens_d":   d,
            "effect":     effect_label(d),
            "normal_nba": p_norm_nba,
            "normal_wnba":p_norm_wnba,
            "equal_var":  equal_var,
        }

        line = (
            f"\n{col}\n"
            f"  NBA:  mean={nba_vals.mean():.3f}  std={nba_vals.std():.3f}\n"
            f"  WNBA: mean={wnba_vals.mean():.3f}  std={wnba_vals.std():.3f}\n"
            f"  Normality (Shapiro-Wilk): NBA p={p_norm_nba:.2e}  "
            f"WNBA p={p_norm_wnba:.2e}\n"
            f"  Variance equality (Levene): p={p_levene:.2e}  "
            f"{'equal' if equal_var else 'unequal'}\n"
            f"  Test: {test_name}  stat={stat:.3f}  p={p_val:.2e}\n"
            f"  Cohen's d: {d:.3f} ({effect_label(d)} effect)"
        )
        print(line)
        lines.append(line)

    return results, "\n".join(lines)

# ── 5. VISUALISE ──────────────────────────────────────────────────────────────

LABELS = {"FG%": "Field Goal %", "FT%": "Free Throw %", "3P%": "Three-Point %"}

def plot_distributions(nba, wnba, results):
    """KDE + rug plots showing overlapping distributions for each metric."""
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.patch.set_facecolor(BG_COLOR)

    for ax, col in zip(axes, PCT_COLS):
        ax.set_facecolor(BG_COLOR)

        nba_vals  = nba[col].dropna()
        wnba_vals = wnba[col].dropna()

        sns.kdeplot(nba_vals,  ax=ax, color=COLOR_NBA,  fill=True,
                    alpha=0.35, linewidth=2, label="NBA",
                    clip=(0, 1))
        sns.kdeplot(wnba_vals, ax=ax, color=COLOR_WNBA, fill=True,
                    alpha=0.35, linewidth=2, label="WNBA",
                    clip=(0, 1))

        # Rug marks — sit just below x-axis
        y_min = ax.get_ylim()[0]
        ax.plot(nba_vals,  np.full_like(nba_vals,  -0.15),
                "|", color=COLOR_NBA,  alpha=0.35, markersize=4,
                transform=ax.get_xaxis_transform())
        ax.plot(wnba_vals, np.full_like(wnba_vals, -0.25),
                "|", color=COLOR_WNBA, alpha=0.35, markersize=4,
                transform=ax.get_xaxis_transform())

        # Mean lines
        ax.axvline(nba_vals.mean(),  color=COLOR_NBA,  linestyle="--",
                   linewidth=1.5, alpha=0.9)
        ax.axvline(wnba_vals.mean(), color=COLOR_WNBA, linestyle="--",
                   linewidth=1.5, alpha=0.9)

        r = results[col]
        p_str = f"p < 0.001" if r["p_value"] < 0.001 else f"p = {r['p_value']:.3f}"
        ax.set_title(
            f"{LABELS[col]}\n"
            f"Cohen's d = {r['cohens_d']:.2f} ({r['effect']}),  {p_str}",
            fontsize=10, color=INK_COLOR, pad=10
        )
        ax.set_xlabel("Shooting %", color=INK_MID, fontsize=9)
        ax.set_ylabel("Density", color=INK_MID, fontsize=9)
        ax.set_xlim(0, 1)
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0%}"))
        ax.tick_params(colors=INK_MID, labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor(INK_MID)
            spine.set_linewidth(0.5)

    # Shared legend
    nba_patch  = mpatches.Patch(color=COLOR_NBA,  alpha=0.7, label="NBA")
    wnba_patch = mpatches.Patch(color=COLOR_WNBA, alpha=0.7, label="WNBA")
    fig.legend(handles=[nba_patch, wnba_patch], loc="upper center",
               ncol=2, fontsize=10, frameon=False,
               bbox_to_anchor=(0.5, 1.02))

    fig.suptitle(
        "WNBA vs NBA Shooting Accuracy — 2022-23 Season",
        fontsize=13, color=INK_COLOR, y=1.06, fontweight="bold"
    )
    plt.tight_layout()
    os.makedirs("images", exist_ok=True)
    plt.savefig("images/shooting_distributions.png", dpi=150,
                bbox_inches="tight", facecolor=BG_COLOR)
    print("\n✅  Saved images/shooting_distributions.png")
    plt.close()


def plot_boxplots(combined, results):
    """Side-by-side box plots with annotated effect sizes."""
    fig, axes = plt.subplots(1, 3, figsize=(13, 6))
    fig.patch.set_facecolor(BG_COLOR)

    palette = {"NBA": COLOR_NBA, "WNBA": COLOR_WNBA}

    for ax, col in zip(axes, PCT_COLS):
        ax.set_facecolor(BG_COLOR)

        sns.boxplot(
            data=combined, x="League", y=col,
            palette=palette, ax=ax,
            width=0.5, linewidth=1.2,
            order=["NBA", "WNBA"],
            flierprops=dict(marker="o", markersize=3, alpha=0.4)
        )

        r = results[col]
        p_str = "p < 0.001" if r["p_value"] < 0.001 else f"p = {r['p_value']:.3f}"
        ax.set_title(
            f"{LABELS[col]}\n{p_str}  |  d = {r['cohens_d']:.2f}",
            fontsize=10, color=INK_COLOR, pad=8
        )
        ax.set_xlabel("")
        ax.set_ylabel("Shooting %", color=INK_MID, fontsize=9)
        ax.tick_params(colors=INK_MID, labelsize=9)
        for spine in ax.spines.values():
            spine.set_edgecolor(INK_MID)
            spine.set_linewidth(0.5)

    fig.suptitle(
        "WNBA vs NBA Shooting Accuracy — 2022-23 Season",
        fontsize=13, color=INK_COLOR, fontweight="bold"
    )
    plt.tight_layout()
    plt.savefig("images/shooting_boxplots.png", dpi=150,
                bbox_inches="tight", facecolor=BG_COLOR)
    print("✅  Saved images/shooting_boxplots.png")
    plt.close()

# ── 6. MAIN ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("WNBA vs NBA Shooting Accuracy — rebuild_analysis.py")
    print("=" * 60)

    # Scrape
    nba_raw  = scrape(NBA_URL,  "NBA")
    wnba_raw = scrape(WNBA_URL, "WNBA")

    # Clean
    nba  = clean(nba_raw,  "NBA")
    wnba = clean(wnba_raw, "WNBA")

    # Validate
    validate(nba, wnba)

    # Save clean CSVs
    os.makedirs("data", exist_ok=True)
    nba.to_csv("data/nba_clean.csv",  index=False)
    wnba.to_csv("data/wnba_clean.csv", index=False)

    combined = pd.concat([nba, wnba], ignore_index=True)
    combined.to_csv("data/combined_clean.csv", index=False)
    print("\n✅  Saved clean CSVs to data/")

    # Analyse
    results, results_text = run_analysis(nba, wnba)

    with open("data/analysis_results.txt", "w") as f:
        f.write(results_text)
    print("✅  Saved data/analysis_results.txt")

    # Plot
    plot_distributions(nba, wnba, results)
    plot_boxplots(combined, results)

    print("\n── Done ─────────────────────────────────────────────────────────")
    print("Next steps:")
    print("  1. Review images/ — do the distributions look sensible?")
    print("  2. Review data/analysis_results.txt for clean stats to use in README")
    print("  3. Run: git add data/ images/ scripts/ && git commit -m 'Clean data + rerun analysis'")

if __name__ == "__main__":
    main()
