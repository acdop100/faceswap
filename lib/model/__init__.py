#!/usr/bin/env python3
""" Conditional imports depending on whether the AMD version is installed or not """
from __future__ import annotations

from .loss import losses  # noqa
from .normalization import AdaInstanceNormalization
from .normalization import GroupNormalization
from .normalization import InstanceNormalization
from .normalization import LayerNormalization
from .normalization import RMSNormalization
from lib.utils import get_backend

if get_backend() == "amd":
    from . import optimizers_plaid as optimizers  # noqa
else:
    from . import optimizers_tf as optimizers  # type:ignore # noqa
