"""Training, dataset validation, hardware estimation, evaluation, and model registry subsystem."""
from ...config import DATA

TRAINING_DIR = DATA / "learning" / "training"
RAW_DIR = TRAINING_DIR / "raw"
CLEANED_DIR = TRAINING_DIR / "cleaned"
PREPARED_DIR = TRAINING_DIR / "prepared"
QUARANTINE_DIR = TRAINING_DIR / "quarantine"
JOBS_DIR = TRAINING_DIR / "jobs"
EVALUATIONS_DIR = DATA / "learning" / "evaluations"
REGISTRY_PATH = DATA / "learning" / "registry.json"
