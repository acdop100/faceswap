#!/usr/bin/env python3
""" Conditional imports depending on whether the AMD version is installed or not """
from __future__ import annotations

from .normalization_common import AdaInstanceNormalization  # noqa
from .normalization_common import GroupNormalization  # noqa
from .normalization_common import InstanceNormalization  # noqa
from lib.utils import get_backend


if get_backend() == "amd":
    from .normalization_plaid import LayerNormalization, RMSNormalization  # noqa
else:
    from .normalization_tf import LayerNormalization, RMSNormalization  # noqa
