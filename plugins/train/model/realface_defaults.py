#!/usr/bin/env python3
"""
    The default options for the faceswap Realface Model plugin.

    Defaults files should be named <plugin_name>_defaults.py
    Any items placed into this file will automatically get added to the relevant config .ini files
    within the faceswap/config folder.

    The following variables should be defined:
        _HELPTEXT: A string describing what this plugin does
        _DEFAULTS: A dictionary containing the options, defaults and meta information. The
                   dictionary should be defined as:
                       {<option_name>: {<metadata>}}

                   <option_name> should always be lower text.
                   <metadata> dictionary requirements are listed below.

    The following keys are expected for the _DEFAULTS <metadata> dict:
        datatype:  [required] A python type class. This limits the type of data that can be
                   provided in the .ini file and ensures that the value is returned in the
                   correct type to faceswap. Valid datatypes are: <class 'int'>, <class 'float'>,
                   <class 'str'>, <class 'bool'>.
        default:   [required] The default value for this option.
        info:      [required] A string describing what this option does.
        choices:   [optional] If this option's datatype is of <class 'str'> then valid
                   selections can be defined here. This validates the option and also enables
                   a combobox / radio option in the GUI.
        gui_radio: [optional] If <choices> are defined, this indicates that the GUI should use
                   radio buttons rather than a combobox to display this option.
        min_max:   [partial] For <class 'int'> and <class 'float'> datatypes this is required
                   otherwise it is ignored. Should be a tuple of min and max accepted values.
                   This is used for controlling the GUI slider range. Values are not enforced.
        rounding:  [partial] For <class 'int'> and <class 'float'> datatypes this is
                   required otherwise it is ignored. Used for the GUI slider. For floats, this
                   is the number of decimal places to display. For ints this is the step size.
        fixed:     [optional] [train only]. Training configurations are fixed when the model is
                   created, and then reloaded from the state file. Marking an item as fixed=False
                   indicates that this value can be changed for existing models, and will override
                   the value saved in the state file with the updated value in config. If not
                   provided this will default to True.
"""
from __future__ import annotations


_HELPTEXT = (
    "An extra detailed variant of Original model.\n"
    "Incorporates ideas from Bryanlyon and inspiration from the Villain model.\n"
    "Requires about 6GB-8GB of VRAM (batchsize 8-16).\n"
)


_DEFAULTS = {
    "input_size": {
        "default": 64,
        "info": "Resolution (in pixels) of the input image to train on.\n"
        "BE AWARE Larger resolution will dramatically increase VRAM requirements.\n"
        "Higher resolutions may increase prediction accuracy, but does not effect the "
        "resulting output size.\nMust be between 64 and 128 and be divisible by 16.",
        "datatype": int,
        "rounding": 16,
        "min_max": (64, 128),
        "choices": [],
        "gui_radio": False,
        "fixed": True,
        "group": "size",
    },
    "output_size": {
        "default": 128,
        "info": "Output image resolution (in pixels).\nBe aware that larger resolution will "
        "increase VRAM requirements.\nNB: Must be between 64 and 256 and be divisible "
        "by 16.",
        "datatype": int,
        "rounding": 16,
        "min_max": (64, 256),
        "choices": [],
        "gui_radio": False,
        "fixed": True,
        "group": "size",
    },
    "dense_nodes": {
        "default": 1536,
        "info": "Number of nodes for decoder. Might affect your model's ability to learn in "
        "general.\nNote that: Lower values will affect the ability to predict "
        "details.",
        "datatype": int,
        "rounding": 64,
        "min_max": (768, 2048),
        "choices": [],
        "gui_radio": False,
        "fixed": True,
        "group": "network",
    },
    "complexity_encoder": {
        "default": 128,
        "info": "Encoder Convolution Layer Complexity. sensible ranges: 128 to 150.",
        "datatype": int,
        "rounding": 4,
        "min_max": (96, 160),
        "choices": [],
        "gui_radio": False,
        "fixed": True,
        "group": "network",
    },
    "complexity_decoder": {
        "default": 512,
        "info": "Decoder Complexity.",
        "datatype": int,
        "rounding": 4,
        "min_max": (512, 544),
        "choices": [],
        "gui_radio": False,
        "fixed": True,
        "group": "network",
    },
}
