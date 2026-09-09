"""Deterministic data-quality and anomaly-detection pipeline for loan tapes."""

from loan_dq.config import Config
from loan_dq.engine import run

__all__ = ["Config", "run"]
__version__ = "0.1.0"
