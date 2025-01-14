#!/usr/bin/env python3
""" Command Line Arguments for tools """
from __future__ import annotations

import gettext

from lib.cli.actions import DirFullPaths
from lib.cli.actions import DirOrFileFullPaths
from lib.cli.actions import FileFullPaths
from lib.cli.args import FaceSwapArgs

# LOCALES
_LANG = gettext.translation("tools.preview", localedir="locales", fallback=True)
_ = _LANG.gettext


_HELPTEXT = _("This command allows you to preview swaps to tweak convert settings.")


class PreviewArgs(FaceSwapArgs):
    """Class to parse the command line arguments for Preview (Convert Settings) tool"""

    @staticmethod
    def get_info():
        """Return command information"""
        return _(
            "Preview tool\nAllows you to configure your convert settings with a live preview"
        )

    def get_argument_list(self):

        argument_list = []
        argument_list.append(
            dict(
                opts=("-i", "--input-dir"),
                action=DirOrFileFullPaths,
                filetypes="video",
                dest="input_dir",
                group=_("data"),
                required=True,
                help=_(
                    "Input directory or video. Either a directory containing the image files you "
                    "wish to process or path to a video file."
                ),
            )
        )
        argument_list.append(
            dict(
                opts=("-al", "--alignments"),
                action=FileFullPaths,
                filetypes="alignments",
                type=str,
                group=_("data"),
                dest="alignments_path",
                help=_(
                    "Path to the alignments file for the input, if not at the default location"
                ),
            )
        )
        argument_list.append(
            dict(
                opts=("-m", "--model-dir"),
                action=DirFullPaths,
                dest="model_dir",
                group=_("data"),
                required=True,
                help=_(
                    "Model directory. A directory containing the trained model you wish to "
                    "process."
                ),
            )
        )
        argument_list.append(
            dict(
                opts=("-s", "--swap-model"),
                action="store_true",
                dest="swap_model",
                default=False,
                help=_("Swap the model. Instead of A -> B, swap B -> A"),
            )
        )
        return argument_list
