#!/usr/bin/env python3
""" Default configurations for convert """
from __future__ import annotations

import logging
import os

from lib.config import FaceswapConfig

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Config(FaceswapConfig):
    """Config File for Convert"""

    def set_defaults(self):
        """Set the default values for config"""
        self._defaults_from_plugin(os.path.dirname(__file__))
