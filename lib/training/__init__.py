#!/usr/bin/env python3
""" Package for handling alignments files, detected faces and aligned faces along with their
associated objects. """
from __future__ import annotations

from typing import Type
from typing import TYPE_CHECKING

from .augmentation import ImageAugmentation
from .generator import PreviewDataGenerator
from .generator import TrainingDataGenerator
from .preview_cv import PreviewBuffer
from .preview_cv import TriggerType

if TYPE_CHECKING:
    from .preview_cv import PreviewBase

    Preview: type[PreviewBase]

try:
    from .preview_tk import PreviewTk as Preview
except ImportError:
    from .preview_cv import PreviewCV as Preview
