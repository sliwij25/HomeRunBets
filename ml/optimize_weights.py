"""
optimize_weights.py
Train LightGBM on labeled pick_factors data → save ml_weights.json + lgbm_model.txt.
Homer's _score_player() reads these automatically once the files exist.

Run weekly (or after every ~50 new labeled days accumulate).

Usage:
    python optimize_weights.py              # train + save weights
    python optimize_weights.py --report     # report only, don't save weights
    python optimize_weights.py --min 50     # require at least N labeled rows (default 100)

Output:
    ml_weights.json          — metadata (AUC, feature_order, model_type)
    lgbm_model.txt           — LightGBM booster (loaded by Homer._ml_score)
    (stdout)                 — feature importances, calibration, rank-vs-hit-rate
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np

os.chdir(str(Path(__file__).parent.parent))
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bets.db")
WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "..", "ml_weights.json")
LGBM_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "lgbm_model.txt")

# Features for LightGBM training.
# Each entry: (column_name, transform)
# transform: None = use raw value, "platoon" = PLATOON+→1 / platoon-→-1 / else→0
# All contact-quality features included — LightGBM handles correlated features
# correctly (no sign-flipping like logistic regression).
FEATURES = [
    # Contact quality — all restored; LightGBM splits independently without multicollinearity issues
    ("barrel_rate",      None),    # r=0.70 predictive for HR%
    ("ev_avg",           None),    # r=0.57 predictive — avg exit velocity
    ("hard_hit_pct",     None),    # r=0.66 descriptive — EV 100+ mph
    ("sweet_spot_pct",   None),    # r=0.42 predictive — 8-32° launch angle%
    ("xiso",             None),    # expected ISO — power composite
    ("xslg",             None),    # expected slugging
    ("xhr_rate",         None),    # expected HR rate — populates mid-season
    # Batted ball profile
    ("fb_pct",           None),    # fly ball rate — per RotoGrinders: strong HR correlation
    ("launch_angle",     None),    # avg launch angle — r=0.42 predictive
    ("hr_fb_ratio",      None),    # HR/FB — volatile early, meaningful mid-season
    # Bat tracking
    ("blast_rate",       None),    # % of swings qualifying as a Blast — high HR correlation
    # Context
    ("bpp_hr_pct",       None),
    ("park_hr_factor",   None),
    ("ev_10",            None),
    ("value_edge",       None),
    ("recent_form_14d",  None),
    ("pitcher_hr_per_9",   None),
    ("pitcher_hr_vs_hand", None),
    ("pitcher_barrel_pct", None),
    ("is_home",            None),
    ("platoon",          "platoon"),
    ("h2h_hr",           None),
    ("career_park_hr",   None),    # career HR count at today's specific venue
    ("pitcher_career_hr_vs_hand", None),  # career HR/9 vs batter's handedness (explicit ML feature)
    # Pitcher pitch mix
    ("pitcher_fb_pct",       None),
    ("pitcher_breaking_pct", None),
    ("pitcher_offspeed_pct", None),
    # Batter split vs pitcher's dominant pitch type
    ("batter_xslg_vs_fastball",  None),
    ("batter_xslg_vs_breaking",  None),
    ("batter_xslg_vs_offspeed",  None),
]

FEATURE_NAMES = [name for name, _ in FEATURES]


def load_training_data() -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """
    Load pick_factors rows where homered IS NOT NULL.
    Returns (X, y, raw_rows).
    Missing feature values are imputed with the column median.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = ", ".join(name for name, _ in FEATURES)
        rows = conn.execute(f"""
            SELECT {cols}, homered, bet_date, player, score, rank, confidence
            FROM pick_factors
            WHERE homered IS NOT NULL
            ORDER BY bet_date
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        return np.array([]), np.array([]), []

    n_features = len(FEATURES)
    raw_rows = []
    X_raw = []
    y = []

    for row in rows:
        feat_vals = list(row[:n_features])
        homered   = row[n_features]
        bet_date  = row[n_features + 1]
        player    = row[n_features + 2]
        score     = row[n_features + 3]
        rank_val  = row[n_features + 4]
        conf      = row[n_features + 5]

        # Transform features
        transformed = []
        for i, (col, transform) in enumerate(FEATURES):
            val = feat_vals[i]
            if transform == "platoon":
                if val == "PLATOON+":
                    transformed.append(1.0)
                elif val == "platoon-":
                    transformed.append(-1.0)
                else:
                    transformed.append(0.0)
            else:
                transformed.append(float(val) if val is not None else np.nan)

        X_raw.append(transformed)
        y.append(int(homered))
        raw_rows.append({
            "player": player, "bet_date": bet_date,
            "score": score, "rank": rank_val,
            "confidence": conf, "homered": homered,
        })

    X = np.array(X_raw, dtype=float)

    # Impute missing values with column median
    for col_i in range(X.shape[1]):
        col = X[:, col_i]
        median = np.nanmedian(col)
        X[np.isnan(col), col_i] = median if not np.isnan(median) else 0.0

    return X, np.array(y), raw_rows


def point_biserial_correlation(X: np.ndarray, y: np.ndarray) -> list[tuple[str, float]]:
    """Compute correlation between each feature and the binary outcome."""
    from scipy import stats
    results = []
    for i, name in enumerate(FEATURE_NAMES):
        col = X[:, i]
        if col.std() < 1e-9:
            results.append((name, 0.0))
            continue
        r, p = stats.pointbiserialr(col, y)
        results.append((name, r))
    return sorted(results, key=lambda x: abs(x[1]), reverse=True)


def rank_hit_rate_analysis(raw_rows: list[dict]) -> None:
    """Show HR hit rate by Homer score rank bucket."""
    buckets = {
        "Top 5":    [r for r in raw_rows if r["rank"] and r["rank"] <= 5],
        "6–10":     [r for r in raw_rows if r["rank"] and 6 <= r["rank"] <= 10],
        "11–20":    [r for r in raw_rows if r["rank"] and 11 <= r["rank"] <= 20],
        "21–40":    [r for r in raw_rows if r["rank"] and 21 <= r["rank"] <= 40],
        "41+":      [r for r in raw_rows if r["rank"] and r["rank"] > 40],
        "No rank":  [r for r in raw_rows if not r["rank"]],
    }
    print("\n  Rank bucket → HR hit rate (this is the key metric):")
    print(f"  {'Bucket':<12} {'Players':>8} {'Homered':>8} {'Hit Rate':>10}")
    print("  " + "-" * 42)
    for label, group in buckets.items():
        if not group:
            continue
        hr_count = sum(r["homered"] for r in group)
        rate = hr_count / len(group) * 100
        bar = "█" * int(rate / 2)
        print(f"  {label:<12} {len(group):>8} {hr_count:>8} {rate:>9.1f}%  {bar}")

    overall_rate = sum(r["homered"] for r in raw_rows) / len(raw_rows) * 100
    print(f"\n  Overall HR rate: {overall_rate:.1f}%  (MLB base rate: ~15%)")
    print("  If Top 5 hit rate >> 41+ hit rate, Homer's ranking is working.")


def confidence_calibration(raw_rows: list[dict]) -> None:
    """Show actual HR rate by confidence tier."""
    tiers = {"HIGH": [], "MEDIUM": [], "LOW": [], None: []}
    for r in raw_rows:
        tier = r.get("confidence")
        if tier not in tiers:
            tier = None
        tiers[tier].append(r["homered"])

    print("\n  Confidence tier calibration:")
    print(f"  {'Tier':<10} {'Count':>6} {'HR Rate':>8}")
    print("  " + "-" * 28)
    for tier in ("HIGH", "MEDIUM", "LOW", None):
        vals = tiers[tier]
        if not vals:
            continue
        rate = sum(vals) / len(vals) * 100
        label = tier or "unknown"
        print(f"  {label:<10} {len(vals):>6} {rate:>7.1f}%")
    print("  (HIGH should have the highest hit rate — if not, tiers need recalibration)")


def train_and_save(X: np.ndarray, y: np.ndarray,
                   save: bool = True) -> dict:
    """
    Train LightGBM gradient boosted tree, output feature importances.
    Saves lgbm_model.txt (booster) and ml_weights.json (metadata).
    Returns weights dict.
    """
    try:
        import lightgbm as lgb
        import pandas as pd
        from sklearn.model_selection import cross_val_score, StratifiedKFold
    except ImportError:
        print("\n  lightgbm not installed. Run: pip install lightgbm")
        return {}

    # Wrap in DataFrame so LightGBM tracks feature names (suppresses sklearn warning)
    X_df = pd.DataFrame(X, columns=FEATURE_NAMES)

    scale_pos_weight = (len(y) - y.sum()) / max(y.sum(), 1)

    lgbm_params = {
        "objective":        "binary",
        "metric":           "auc",
        "n_estimators":     500,
        "learning_rate":    0.05,
        "num_leaves":       31,
        "min_child_samples": 50,   # prevents overfitting on rare HR events
        "scale_pos_weight": scale_pos_weight,
        "random_state":     42,
        "verbose":          -1,
    }

    model = lgb.LGBMClassifier(**lgbm_params)

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    auc_scores = cross_val_score(model, X_df, y, cv=cv, scoring="roc_auc")
    print(f"\n  Cross-val AUC: {auc_scores.mean():.3f} ± {auc_scores.std():.3f}")
    print("  (0.5 = random, 0.6+ = useful, 0.7+ = strong)")

    model.fit(X_df, y)

    importances = sorted(
        zip(FEATURE_NAMES, model.feature_importances_),
        key=lambda x: x[1], reverse=True
    )

    print("\n  Feature importances (LightGBM gain — split count):")
    print(f"  {'Feature':<22} {'Importance':>10}")
    print("  " + "-" * 36)
    for feat, imp in importances:
        bar = "█" * int(imp / max(v for _, v in importances) * 20)
        print(f"  {feat:<22} {imp:>10}  {bar}")

    weights = {
        "model_type":    "lightgbm",
        "trained_on":    date.today().isoformat(),
        "n_samples":     int(len(y)),
        "n_positives":   int(y.sum()),
        "cv_auc_mean":   float(auc_scores.mean()),
        "cv_auc_std":    float(auc_scores.std()),
        "feature_order": FEATURE_NAMES,
        "algo_version":  "4.0",
    }

    if save:
        model.booster_.save_model(LGBM_MODEL_PATH)
        with open(WEIGHTS_PATH, "w") as f:
            json.dump(weights, f, indent=2)
        print(f"\n  Model saved to lgbm_model.txt")
        print(f"  Metadata saved to ml_weights.json")
        print("  Homer will use these weights automatically on next run.")

    return weights


def main():
    parser = argparse.ArgumentParser(description="Train logistic regression on HR pick data.")
    parser.add_argument("--report", action="store_true",
                        help="Show report only — do not save weights")
    parser.add_argument("--min", type=int, default=100, dest="min_rows",
                        help="Minimum labeled rows required to train (default: 100)")
    args = parser.parse_args()

    print("=" * 60)
    print("  HOMER ML WEIGHT OPTIMIZER")
    print("=" * 60)

    X, y, raw_rows = load_training_data()

    if len(y) == 0:
        print("\n  No labeled data yet.")
        print("  Run fetch_actual_results.py after each game day to label picks.")
        print("  Come back after ~2 weeks of data.")
        sys.exit(0)

    n_pos = int(y.sum())
    n_neg = int(len(y) - n_pos)
    base_rate = n_pos / len(y) * 100

    print(f"\n  Labeled examples: {len(y)}")
    print(f"  Homered (positive): {n_pos} ({base_rate:.1f}%)")
    print(f"  Did not homer:      {n_neg}")

    # ── Correlation analysis (always run, no sklearn needed) ──────────────────
    print("\n" + "=" * 60)
    print("  SIGNAL CORRELATIONS  (point-biserial r vs homered)")
    print("=" * 60)
    try:
        correlations = point_biserial_correlation(X, y)
        print(f"  {'Feature':<22} {'r':>8}  Interpretation")
        print("  " + "-" * 58)
        for feat, r in correlations:
            if abs(r) >= 0.10:
                strength = "strong"
            elif abs(r) >= 0.05:
                strength = "moderate"
            else:
                strength = "weak"
            direction = "+" if r >= 0 else "-"
            bar = "█" * int(abs(r) * 40)
            print(f"  {feat:<22} {r:>+8.3f}  {strength} {direction}  {bar}")
    except ImportError:
        print("  (scipy not installed — skipping correlation. pip install scipy)")

    # ── Rank bucket analysis ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  RANK → HIT RATE ANALYSIS")
    print("=" * 60)
    rank_hit_rate_analysis(raw_rows)

    # ── Confidence calibration ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  CONFIDENCE TIER CALIBRATION")
    print("=" * 60)
    confidence_calibration(raw_rows)

    # ── Logistic regression ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  LOGISTIC REGRESSION")
    print("=" * 60)

    if len(y) < args.min_rows:
        print(f"\n  Only {len(y)} labeled rows — need {args.min_rows} to train reliably.")
        print(f"  Keep running daily_picks.py + fetch_actual_results.py.")
        est_days = (args.min_rows - len(y)) // 25
        print(f"  Estimated {est_days} more game days needed.")
        print("\n  Showing correlations above as a guide in the meantime.")
        sys.exit(0)

    weights = train_and_save(X, y, save=not args.report)

    print("\n" + "=" * 60)
    print("  NEXT STEPS")
    print("=" * 60)
    if args.report:
        print("  (--report mode: weights NOT saved)")
        print("  Re-run without --report to save weights to ml_weights.json")
    else:
        print("  1. ml_weights.json saved — Homer uses it automatically")
        print("  2. Re-run daily_picks.py to see ML-adjusted picks")
        print("  3. Run this script again weekly as more data accumulates")
        print("  4. Watch cv_auc_mean — it should rise over time")


if __name__ == "__main__":
    main()
