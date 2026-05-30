# Health Data Analytics (HDA) Textbook — Datasets & Local Environment

This repository contains seven synthetic healthcare datasets used throughout the
Health Data Analytics textbook, plus a small Python environment for loading and
exploring them locally.

## Datasets

Each dataset has two versions: a canonical file and an `_1` variant with a
different set of rows under the same schema (10,000 rows each).

| Short name        | File                                                  | Columns |
|-------------------|-------------------------------------------------------|---------|
| `ehr`             | Dataset 1 Patient Electronic Health Records (EHR).csv | 16      |
| `operations`      | Dataset 2 Hospital Operations Data.csv                | 9       |
| `satisfaction`    | Dataset 3 Patient Satisfaction Surveys.csv            | 9       |
| `billing`         | Dataset 4 Healthcare Costs and Billing.csv            | 9       |
| `clinical_trials` | Dataset 5 Clinical Trials and Research Data.csv       | 11      |
| `iot_wearable`    | Dataset 6 IoT and Wearable Health Data.csv            | 10      |
| `public_health`   | Dataset 7 Public Health Data.csv                      | 10      |

## Run it on your machine

You need **Python 3.10+** (tested on 3.11).

### 1. Clone and enter the repo

```bash
git clone <your-repo-url>
cd HDAtextbook
```

### 2. Create and activate a virtual environment

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Sanity-check the data loader

```bash
python hda_data.py
```

This prints an overview table of all seven datasets (rows, columns, column names).

### 5. Launch JupyterLab and open the starter notebook

```bash
jupyter lab
```

Then open **`explore.ipynb`**, which loads the datasets and shows a couple of
example plots.

## Using the loader in your own code

```python
import hda_data as hda

ehr  = hda.load("ehr")          # one dataset as a pandas DataFrame
data = hda.load_all()           # dict of all datasets, keyed by short name
hda.summary()                   # overview table

alt  = hda.load("ehr", variant=True)   # the _1 variant (different rows)
```

## Notes

- The CSV files are committed to the repo, so no separate download is needed.
- Virtual environments, caches, and notebook checkpoints are ignored via
  `.gitignore`.
