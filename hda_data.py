"""Reusable loader for the Health Data Analytics (HDA) textbook datasets.

The repository ships seven CSV datasets. Each one exists in two versions:
a canonical file (e.g. ``Dataset 1 Patient Electronic Health Records (EHR).csv``)
and an ``_1`` variant containing a different set of rows under the same schema.

Typical use::

    import hda_data as hda

    ehr = hda.load("ehr")          # one dataset as a DataFrame
    data = hda.load_all()          # dict of all datasets, keyed by short name
    print(hda.summary())           # quick overview table

Run as a script for a quick sanity check::

    python hda_data.py
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pandas as pd

# Directory that holds the CSV files (the repo root, alongside this module).
DATA_DIR = Path(__file__).resolve().parent

# Short, code-friendly names mapped to the canonical CSV filename.
DATASETS: dict[str, str] = {
    "ehr": "Dataset 1 Patient Electronic Health Records (EHR).csv",
    "operations": "Dataset 2 Hospital Operations Data.csv",
    "satisfaction": "Dataset 3 Patient Satisfaction Surveys.csv",
    "billing": "Dataset 4 Healthcare Costs and Billing.csv",
    "clinical_trials": "Dataset 5 Clinical Trials and Research Data.csv",
    "iot_wearable": "Dataset 6 IoT and Wearable Health Data.csv",
    "public_health": "Dataset 7 Public Health Data.csv",
}


def _resolve(name: str, variant: bool) -> Path:
    """Return the path to a dataset, optionally the ``_1`` variant."""
    if name not in DATASETS:
        valid = ", ".join(DATASETS)
        raise KeyError(f"Unknown dataset {name!r}. Valid names: {valid}")
    filename = DATASETS[name]
    if variant:
        filename = filename.replace(".csv", "_1.csv")
    path = DATA_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Expected data file not found: {path}")
    return path


def load(name: str, variant: bool = False, **read_csv_kwargs) -> pd.DataFrame:
    """Load a single dataset by its short name (see :data:`DATASETS`).

    Set ``variant=True`` to load the ``_1`` version of the file. Any extra
    keyword arguments are forwarded to :func:`pandas.read_csv`.
    """
    return pd.read_csv(_resolve(name, variant), **read_csv_kwargs)


def load_all(variant: bool = False, **read_csv_kwargs) -> dict[str, pd.DataFrame]:
    """Load every dataset into a dict keyed by short name."""
    return {name: load(name, variant=variant, **read_csv_kwargs) for name in DATASETS}


def summary(variant: bool = False) -> pd.DataFrame:
    """Return a small overview table: rows, columns, and column names."""
    rows = []
    for name in DATASETS:
        df = load(name, variant=variant)
        rows.append(
            {
                "name": name,
                "rows": len(df),
                "columns": df.shape[1],
                "column_names": ", ".join(df.columns),
            }
        )
    return pd.DataFrame(rows).set_index("name")


if __name__ == "__main__":
    pd.set_option("display.max_colwidth", 80)
    print(f"Data directory: {DATA_DIR}\n")
    print(summary().to_string())
