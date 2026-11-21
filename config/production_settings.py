"""Opt-in deployment settings. This file never enables release or model acceptance.

References: https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
https://docs.djangoproject.com/en/5.2/ref/databases/#postgresql-notes
"""
import os as _os

from operations.production import production_values as _production_values

# Validate before base settings can fall back to local keys or localhost defaults.
_validated = _production_values(_os.environ)
from .settings import *  # noqa: E402,F403

globals().update(_validated)
del _validated
