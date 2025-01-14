#!/usr/bin/env python3
""" Tool to preview swaps and tweak configuration prior to running a convert """
from __future__ import annotations

import gettext
import logging
import os
import random
import sys
import tkinter as tk
from configparser import ConfigParser
from dataclasses import dataclass
from dataclasses import field
from threading import Event
from threading import Lock
from threading import Thread
from tkinter import ttk
from typing import Any
from typing import Callable
from typing import cast
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional
from typing import Tuple
from typing import TYPE_CHECKING
from typing import Union

import cv2
import numpy as np
from PIL import Image
from PIL import ImageTk

from lib.align import DetectedFace
from lib.align import transform_image
from lib.cli.args import ConvertArgs
from lib.convert import Converter
from lib.gui.control_helper import ControlPanel
from lib.gui.control_helper import ControlPanelOption
from lib.gui.custom_widgets import Tooltip
from lib.gui.utils import get_config
from lib.gui.utils import get_images
from lib.gui.utils import initialize_config
from lib.gui.utils import initialize_images
from lib.queue_manager import queue_manager
from lib.utils import FaceswapError
from plugins.convert._config import Config
from plugins.extract.pipeline import ExtractMedia
from plugins.plugin_loader import PluginLoader
from scripts.convert import ConvertItem
from scripts.convert import Predict
from scripts.fsmedia import Alignments
from scripts.fsmedia import Images

if TYPE_CHECKING:
    from argparse import Namespace
    from lib.align.aligned_face import CenteringType
    from lib.queue_manager import EventQueue

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

# LOCALES
_LANG = gettext.translation("tools.preview", localedir="locales", fallback=True)
_ = _LANG.gettext


class Preview(tk.Tk):  # pylint:disable=too-few-public-methods
    """This tool is part of the Faceswap Tools suite and should be called from
    ``python tools.py preview`` command.

    Loads up 5 semi-random face swaps and displays them, cropped, in place in the final frame.
    Allows user to live tweak settings, before saving the final config to
    :file:`./config/convert.ini`

    Parameters
    ----------
    arguments: :class:`argparse.Namespace`
        The :mod:`argparse` arguments as passed in from :mod:`tools.py`
    """

    _w: str

    def __init__(self, arguments: Namespace) -> None:
        logger.debug(
            "Initializing %s: (arguments: '%s'", self.__class__.__name__, arguments
        )
        super().__init__()
        self._config_tools = ConfigTools()
        self._lock = Lock()

        self._tk_vars: dict[Literal["refresh", "busy"], tk.BooleanVar] = dict(
            refresh=tk.BooleanVar(), busy=tk.BooleanVar()
        )
        for val in self._tk_vars.values():
            val.set(False)
        self._display = FacesDisplay(256, 64, self._tk_vars)

        trigger_patch = Event()
        self._samples = Samples(arguments, 5, self._display, self._lock, trigger_patch)
        self._patch = Patch(
            arguments,
            self._available_masks,
            self._samples,
            self._display,
            self._lock,
            trigger_patch,
            self._config_tools,
            self._tk_vars,
        )

        self._initialize_tkinter()
        self._image_canvas: ImagesCanvas | None = None
        self._opts_book: OptionsBook | None = None
        self._cli_frame: ActionFrame | None = None  # cli frame holds cli options
        logger.debug("Initialized %s", self.__class__.__name__)

    @property
    def _available_masks(self) -> list[str]:
        """list: The mask names that are available for every face in the alignments file"""
        retval = [
            key
            for key, val in self._samples.alignments.mask_summary.items()
            if val == self._samples.alignments.faces_count
        ]
        return retval

    def _initialize_tkinter(self) -> None:
        """Initialize a standalone tkinter instance."""
        logger.debug("Initializing tkinter")
        initialize_config(self, None, None)
        initialize_images()
        get_config().set_geometry(940, 600, fullscreen=False)
        self.title("Faceswap.py - Convert Settings")
        self.tk.call(
            "wm", "iconphoto", self._w, get_images().icons["favicon"]
        )  # pylint:disable=protected-access
        logger.debug("Initialized tkinter")

    def process(self) -> None:
        """The entry point for the Preview tool from :file:`lib.tools.cli`.

        Launch the tkinter preview Window and run main loop.
        """
        self._build_ui()
        self.mainloop()

    def _refresh(self, *args) -> None:
        """Load new faces to display in preview.

        Parameters
        ----------
        *args: tuple
            Unused, but required for tkinter callback.
        """
        logger.trace("Refreshing swapped faces. args: %s", args)  # type: ignore
        self._tk_vars["busy"].set(True)
        self._config_tools.update_config()
        with self._lock:
            assert self._cli_frame is not None
            self._patch.converter_arguments = self._cli_frame.convert_args
            self._patch.current_config = self._config_tools.config
        self._patch.trigger.set()
        logger.trace("Refreshed swapped faces")  # type: ignore

    def _build_ui(self) -> None:
        """Build the elements for displaying preview images and options panels."""
        container = ttk.PanedWindow(self, orient=tk.VERTICAL)
        container.pack(fill=tk.BOTH, expand=True)
        setattr(
            container, "preview_display", self._display
        )  # TODO subclass not setattr
        self._image_canvas = ImagesCanvas(container, self._tk_vars)
        container.add(self._image_canvas, weight=3)

        options_frame = ttk.Frame(container)
        self._cli_frame = ActionFrame(
            options_frame,
            self._available_masks,
            self._samples.predictor.has_predicted_mask,
            self._patch.converter.cli_arguments.color_adjustment.replace("-", "_"),
            self._patch.converter.cli_arguments.mask_type.replace("-", "_"),
            self._config_tools,
            self._refresh,
            self._samples.generate,
            self._tk_vars,
        )
        self._opts_book = OptionsBook(options_frame, self._config_tools, self._refresh)
        container.add(options_frame, weight=1)
        self.update_idletasks()
        container.sashpos(0, int(400 * get_config().scaling_factor))


class Samples:
    """The display samples.

    Obtains and holds :attr:`sample_size` semi random test faces for displaying in the
    preview GUI.

    The file list is split into evenly sized groups of :attr:`sample_size`. When a display set is
    generated, a random image from each of the groups is selected to provide an array of images
    across the length of the video.

    Parameters
    ----------
    arguments: :class:`argparse.Namespace`
        The :mod:`argparse` arguments as passed in from :mod:`tools.py`
    sample_size: int
        The number of samples to take from the input video/images
    display: :class:`FacesDisplay`
        The display section of the Preview GUI.
    lock: :class:`threading.Lock`
        A threading lock to prevent multiple GUI updates at the same time.
    trigger_patch:  :class:`threading.Event`
        An event to indicate that a converter patch should be run
    """

    def __init__(
        self,
        arguments: Namespace,
        sample_size: int,
        display: FacesDisplay,
        lock: Lock,
        trigger_patch: Event,
    ) -> None:
        logger.debug(
            "Initializing %s: (arguments: '%s', sample_size: %s, display: %s, lock: %s, "
            "trigger_patch: %s)",
            self.__class__.__name__,
            arguments,
            sample_size,
            display,
            lock,
            trigger_patch,
        )
        self._sample_size = sample_size
        self._display = display
        self._lock = lock
        self._trigger_patch = trigger_patch
        self._input_images: list[ConvertItem] = []
        self._predicted_images: list[tuple[ConvertItem, np.ndarray]] = []

        self._images = Images(arguments)
        self._alignments = Alignments(
            arguments, is_extract=False, input_is_video=self._images.is_video
        )
        if self._alignments.version == 1.0:
            logger.error(
                "The alignments file format has been updated since the given alignments "
                "file was generated. You need to update the file to proceed."
            )
            logger.error("To do this run the 'Alignments Tool' > 'Extract' Job.")
            sys.exit(1)
        if not self._alignments.have_alignments_file:
            logger.error("Alignments file not found at: '%s'", self._alignments.file)
            sys.exit(1)
        self._filelist = self._get_filelist()
        self._indices = self._get_indices()

        self._predictor = Predict(
            queue_manager.get_queue("preview_predict_in"), sample_size, arguments
        )
        self._display.set_centering(self._predictor.centering)
        self.generate()

        logger.debug("Initialized %s", self.__class__.__name__)

    @property
    def sample_size(self) -> int:
        """int: The number of samples to take from the input video/images"""
        return self._sample_size

    @property
    def predicted_images(self) -> list[tuple[ConvertItem, np.ndarray]]:
        """list: The predicted faces output from the Faceswap model"""
        return self._predicted_images

    @property
    def alignments(self) -> Alignments:
        """:class:`~lib.align.Alignments`: The alignments for the preview faces"""
        return self._alignments

    @property
    def predictor(self) -> Predict:
        """:class:`~scripts.convert.Predict`: The Predictor for the Faceswap model"""
        return self._predictor

    @property
    def _random_choice(self) -> list[int]:
        """list: Random indices from the :attr:`_indices` group"""
        retval = [random.choice(indices) for indices in self._indices]
        logger.debug(retval)
        return retval

    def _get_filelist(self) -> list[str]:
        """Get a list of files for the input, filtering out those frames which do
        not contain faces.

        Returns
        -------
        list
            A list of filenames of frames that contain faces.
        """
        logger.debug("Filtering file list to frames with faces")
        if isinstance(self._images.input_images, str):
            filelist = [
                f"{os.path.splitext(self._images.input_images)[0]}_{frame_no:06d}.png"
                for frame_no in range(1, self._images.images_found + 1)
            ]
        else:
            filelist = self._images.input_images

        retval = [
            filename
            for filename in filelist
            if self._alignments.frame_has_faces(os.path.basename(filename))
        ]
        logger.debug("Filtered out frames: %s", self._images.images_found - len(retval))
        try:
            assert retval
        except AssertionError as err:
            msg = (
                "No faces were found in any of the frames passed in. Make sure you are passing "
                "in a frames source rather than extracted faces, and that you have provided "
                "the correct alignments file."
            )
            raise FaceswapError(msg) from err
        return retval

    def _get_indices(self) -> list[list[int]]:
        """Get indices for each sample group.

        Obtain :attr:`self.sample_size` evenly sized groups of indices
        pertaining to the filtered :attr:`self._file_list`

        Returns
        -------
        list
            list of indices relating to the filtered file list, split into groups
        """
        # Remove start and end values to get a list divisible by self.sample_size
        no_files = len(self._filelist)
        crop = no_files % self._sample_size
        top_tail = list(range(no_files))[crop // 2 : no_files - (crop - (crop // 2))]
        # Partition the indices
        size = len(top_tail)
        retval = [
            top_tail[start : start + size // self._sample_size]
            for start in range(0, size, size // self._sample_size)
        ]
        logger.debug(
            "Indices pools: %s",
            [
                f"{idx}: (start: {min(pool)}, " f"end: {max(pool)}, size: {len(pool)})"
                for idx, pool in enumerate(retval)
            ],
        )
        return retval

    def generate(self) -> None:
        """Generate a sample set.

        Selects :attr:`sample_size` random faces. Runs them through prediction to obtain the
        swap, then trigger the patch event to run the faces through patching.
        """
        self._load_frames()
        self._predict()
        self._trigger_patch.set()

    def _load_frames(self) -> None:
        """ Load a sample of random frames.

        * Picks a random face from each indices group.

        * Takes the first face from the image (if there are multiple faces). Adds the images to \
        :attr:`self._input_images`.

        * Sets :attr:`_display.source` to the input images and flags that the display should be \
        updated
        """
        self._input_images = []
        for selection in self._random_choice:
            filename = os.path.basename(self._filelist[selection])
            image = self._images.load_one_image(self._filelist[selection])
            # Get first face only
            face = self._alignments.get_faces_in_frame(filename)[0]
            detected_face = DetectedFace()
            detected_face.from_alignment(face, image=image)
            inbound = ExtractMedia(
                filename=filename, image=image, detected_faces=[detected_face]
            )
            self._input_images.append(ConvertItem(inbound=inbound))
        self._display.source = self._input_images
        self._display.update_source = True
        logger.debug(
            "Selected frames: %s",
            [frame.inbound.filename for frame in self._input_images],
        )

    def _predict(self) -> None:
        """Predict from the loaded frames.

        With a threading lock (to prevent stacking), run the selected faces through the Faceswap
        model predict function and add the output to :attr:`predicted`
        """
        with self._lock:
            self._predicted_images = []
            for frame in self._input_images:
                self._predictor.in_queue.put(frame)
            idx = 0
            while idx < self._sample_size:
                logger.debug("Predicting face %s of %s", idx + 1, self._sample_size)
                items: (
                    Literal["EOF"] | list[tuple[ConvertItem, np.ndarray]]
                ) = self._predictor.out_queue.get()
                if items == "EOF":
                    logger.debug("Received EOF")
                    break
                for item in items:
                    self._predicted_images.append(item)
                    logger.debug("Predicted face %s of %s", idx + 1, self._sample_size)
                    idx += 1
        logger.debug("Predicted faces")


class Patch:
    """The Patch pipeline

    Runs in it's own thread. Takes the output from the Faceswap model predictor and runs the faces
    through the convert pipeline using the currently selected options.

    Parameters
    ----------
    arguments: :class:`argparse.Namespace`
        The :mod:`argparse` arguments as passed in from :mod:`tools.py`
    available_masks: list
        The masks that are available for convert
    samples: :class:`Samples`
        The Samples for display.
    display: :class:`FacesDisplay`
        The display section of the Preview GUI.
    lock: :class:`threading.Lock`
        A threading lock to prevent multiple GUI updates at the same time.
    trigger:  :class:`threading.Event`
        An event to indicate that a converter patch should be run
    config_tools: :class:`ConfigTools`
        Tools for loading and saving configuration files
    tk_vars: dict
        Global tkinter variables. `Refresh` and `Busy` :class:`tkinter.BooleanVar`

    Attributes
    ----------
    converter_arguments: dict
        The currently selected converter command line arguments for the patch queue
    current_config::class:`lib.config.FaceswapConfig`
        The currently set configuration for the patch queue
    """

    def __init__(
        self,
        arguments: Namespace,
        available_masks: list[str],
        samples: Samples,
        display: FacesDisplay,
        lock: Lock,
        trigger: Event,
        config_tools: ConfigTools,
        tk_vars: dict[Literal["refresh", "busy"], tk.BooleanVar],
    ) -> None:
        logger.debug(
            "Initializing %s: (arguments: '%s', available_masks: %s, samples: %s, "
            "display: %s, lock: %s, trigger: %s, config_tools: %s, tk_vars %s)",
            self.__class__.__name__,
            arguments,
            available_masks,
            samples,
            display,
            lock,
            trigger,
            config_tools,
            tk_vars,
        )
        self._samples = samples
        self._queue_patch_in = queue_manager.get_queue("preview_patch_in")
        self._display = display
        self._lock = lock
        self._trigger = trigger
        self.current_config = config_tools.config
        self.converter_arguments: None | (
            dict[str, Any]
        ) = None  # Updated converter args dict

        configfile = arguments.configfile if hasattr(arguments, "configfile") else None
        self._converter = Converter(
            output_size=self._samples.predictor.output_size,
            coverage_ratio=self._samples.predictor.coverage_ratio,
            centering=self._samples.predictor.centering,
            draw_transparent=False,
            pre_encode=None,
            arguments=self._generate_converter_arguments(arguments, available_masks),
            configfile=configfile,
        )
        self._shutdown = Event()

        self._thread = Thread(
            target=self._process,
            name="patch_thread",
            args=(
                self._trigger,
                self._shutdown,
                self._queue_patch_in,
                self._samples,
                tk_vars,
            ),
            daemon=True,
        )
        self._thread.start()
        logger.debug("Initializing %s", self.__class__.__name__)

    @property
    def trigger(self) -> Event:
        """:class:`threading.Event`: The trigger to indicate that a patching run should
        commence."""
        return self._trigger

    @property
    def converter(self) -> Converter:
        """:class:`lib.convert.Converter`: The converter to use for patching the images."""
        return self._converter

    @staticmethod
    def _generate_converter_arguments(
        arguments: Namespace, available_masks: list[str]
    ) -> Namespace:
        """Add the default converter arguments to the initial arguments. Ensure the mask selection
        is available.

        Parameters
        ----------
        arguments: :class:`argparse.Namespace`
            The :mod:`argparse` arguments as passed in from :mod:`tools.py`
        available_masks: list
            The masks that are available for convert
        Returns
        ----------
        arguments: :class:`argparse.Namespace`
            The :mod:`argparse` arguments as passed in with converter default
            arguments added
        """
        valid_masks = available_masks + ["none"]
        converter_arguments = ConvertArgs(None, "convert").get_optional_arguments()  # type: ignore
        for item in converter_arguments:
            value = item.get("default", None)
            # Skip options without a default value
            if value is None:
                continue
            option = item.get("dest", item["opts"][1].replace("--", ""))
            if option == "mask_type" and value not in valid_masks:
                logger.debug(
                    "Amending default mask from '%s' to '%s'", value, valid_masks[0]
                )
                value = valid_masks[0]
            # Skip options already in arguments
            if hasattr(arguments, option):
                continue
            # Add option to arguments
            setattr(arguments, option, value)
        logger.debug(arguments)
        return arguments

    def _process(
        self,
        trigger_event: Event,
        shutdown_event: Event,
        patch_queue_in: EventQueue,
        samples: Samples,
        tk_vars: dict[Literal["refresh", "busy"], tk.BooleanVar],
    ) -> None:
        """The face patching process.

        Runs in a thread, and waits for an event to be set. Once triggered, runs a patching
        cycle and sets the :class:`Display` destination images.

        Parameters
        ----------
        trigger_event: :class:`threading.Event`
            Set by parent process when a patching run should be executed
        shutdown_event :class:`threading.Event`
            Set by parent process if a shutdown has been requested
        patch_queue_in: :class:`~lib.queue_manager.EventQueue`
            The input queue for the patching process
        samples: :class:`Samples`
            The Samples for display.
        tk_vars: dict
            Global tkinter variables. `Refresh` and `Busy` :class:`tkinter.BooleanVar`
        """
        logger.debug(
            "Launching patch process thread: (trigger_event: %s, shutdown_event: %s, "
            "patch_queue_in: %s, samples: %s, tk_vars: %s)",
            trigger_event,
            shutdown_event,
            patch_queue_in,
            samples,
            tk_vars,
        )
        patch_queue_out = queue_manager.get_queue("preview_patch_out")
        while True:
            trigger = trigger_event.wait(1)
            if shutdown_event.is_set():
                logger.debug("Shutdown received")
                break
            if not trigger:
                continue
            # Clear trigger so calling process can set it during this run
            trigger_event.clear()
            queue_manager.flush_queue("preview_patch_in")
            self._feed_swapped_faces(patch_queue_in, samples)
            with self._lock:
                self._update_converter_arguments()
                self._converter.reinitialize(config=self.current_config)
            swapped = self._patch_faces(
                patch_queue_in, patch_queue_out, samples.sample_size
            )
            with self._lock:
                self._display.destination = swapped
            tk_vars["refresh"].set(True)
            tk_vars["busy"].set(False)

        logger.debug("Closed patch process thread")

    def _update_converter_arguments(self) -> None:
        """Update the converter arguments to the currently selected values."""
        logger.debug("Updating Converter cli arguments")
        if self.converter_arguments is None:
            logger.debug("No arguments to update")
            return
        for key, val in self.converter_arguments.items():
            logger.debug("Updating %s to %s", key, val)
            setattr(self._converter.cli_arguments, key, val)
        logger.debug("Updated Converter cli arguments")

    @staticmethod
    def _feed_swapped_faces(patch_queue_in: EventQueue, samples: Samples) -> None:
        """Feed swapped faces to the converter's in-queue.

        Parameters
        ----------
        patch_queue_in: :class:`~lib.queue_manager.EventQueue`
            The input queue for the patching process
        samples: :class:`Samples`
            The Samples for display.
        """
        logger.trace("feeding swapped faces to converter")  # type: ignore
        for item in samples.predicted_images:
            patch_queue_in.put(item)
        logger.trace(
            "fed %s swapped faces to converter",  # type: ignore
            len(samples.predicted_images),
        )
        logger.trace("Putting EOF to converter")  # type: ignore
        patch_queue_in.put("EOF")

    def _patch_faces(
        self, queue_in: EventQueue, queue_out: EventQueue, sample_size: int
    ) -> list[np.ndarray]:
        """Patch faces.

        Run the convert process on the swapped faces and return the patched faces.

        patch_queue_in: :class:`~lib.queue_manager.EventQueue`
            The input queue for the patching process
        queue_out: :class:`~lib.queue_manager.EventQueue`
            The output queue from the patching process
        sample_size: int
            The number of samples to be displayed

        Returns
        -------
        list
            The swapped faces patched with the selected convert settings
        """
        logger.trace("Patching faces")  # type: ignore
        self._converter.process(queue_in, queue_out)
        swapped = []
        idx = 0
        while idx < sample_size:
            logger.trace("Patching image %s of %s", idx + 1, sample_size)  # type: ignore
            item = queue_out.get()
            swapped.append(item[1])
            logger.trace("Patched image %s of %s", idx + 1, sample_size)  # type: ignore
            idx += 1
        logger.trace("Patched faces")  # type: ignore
        return swapped


@dataclass
class _Faces:
    """Dataclass for holding faces"""

    filenames: list[str] = field(default_factory=list)
    matrix: list[np.ndarray] = field(default_factory=list)
    src: list[np.ndarray] = field(default_factory=list)
    dst: list[np.ndarray] = field(default_factory=list)


class FacesDisplay:
    """Compiles the 2 rows of sample faces (original and swapped) into a single image

    Parameters
    ----------
    size: int
        The size of each individual face sample in pixels
    padding: int
        The amount of extra padding to apply to the outside of the face
    tk_vars: dict
        Global tkinter variables. `Refresh` and `Busy` :class:`tkinter.BooleanVar`

    Attributes
    ----------
    update_source: bool
        Flag to indicate that the source images for the preview have been updated, so the preview
        should be recompiled.
    source: list
        The list of :class:`numpy.ndarray` source preview images for top row of display
    destination: list
        The list of :class:`numpy.ndarray` swapped and patched preview images for bottom row of
        display
    """

    def __init__(
        self,
        size: int,
        padding: int,
        tk_vars: dict[Literal["refresh", "busy"], tk.BooleanVar],
    ) -> None:
        logger.trace(
            "Initializing %s: (size: %s, padding: %s, tk_vars: %s)",  # type: ignore
            self.__class__.__name__,
            size,
            padding,
            tk_vars,
        )
        self._size = size
        self._display_dims = (1, 1)
        self._tk_vars = tk_vars
        self._padding = padding

        self._faces = _Faces()
        self._centering: CenteringType | None = None
        self._faces_source: np.ndarray = np.array([])
        self._faces_dest: np.ndarray = np.array([])
        self._tk_image: ImageTk.PhotoImage | None = None

        # Set from Samples
        self.update_source = False
        self.source: list[ConvertItem] = []  # Source images, filenames + detected faces
        # Set from Patch
        self.destination: list[np.ndarray] = []  # Swapped + patched images

        logger.trace("Initialized %s", self.__class__.__name__)  # type: ignore

    @property
    def tk_image(self) -> ImageTk.PhotoImage | None:
        """:class:`PIL.ImageTk.PhotoImage`: The compiled preview display in tkinter display
        format"""
        return self._tk_image

    @property
    def _total_columns(self) -> int:
        """int: The total number of images that are being displayed"""
        return len(self.source)

    def set_centering(self, centering: CenteringType) -> None:
        """The centering that the model uses is not known at initialization time.
        Set :attr:`_centering` when the model has been loaded.

        Parameters
        ----------
        centering: str
            The centering that the model was trained on
        """
        self._centering = centering

    def set_display_dimensions(self, dimensions: tuple[int, int]) -> None:
        """Adjust the size of the frame that will hold the preview samples.

        Parameters
        ----------
        dimensions: tuple
            The (`width`, `height`) of the frame that holds the preview
        """
        self._display_dims = dimensions

    def update_tk_image(self) -> None:
        """Build the full preview images and compile :attr:`tk_image` for display."""
        logger.trace("Updating tk image")  # type: ignore
        self._build_faces_image()
        img = np.vstack((self._faces_source, self._faces_dest))
        size = self._get_scale_size(img)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pilimg = Image.fromarray(img)
        pilimg = pilimg.resize(size, Image.ANTIALIAS)
        self._tk_image = ImageTk.PhotoImage(pilimg)
        self._tk_vars["refresh"].set(False)
        logger.trace("Updated tk image")  # type: ignore

    def _get_scale_size(self, image: np.ndarray) -> tuple[int, int]:
        """Get the size that the full preview image should be resized to fit in the
        display window.

        Parameters
        ----------
        image: :class:`numpy.ndarray`
            The full sized compiled preview image

        Returns
        -------
        tuple
            The (`width`, `height`) that the display image should be sized to fit in the display
            window
        """
        frameratio = float(self._display_dims[0]) / float(self._display_dims[1])
        imgratio = float(image.shape[1]) / float(image.shape[0])

        if frameratio <= imgratio:
            scale = self._display_dims[0] / float(image.shape[1])
            size = (self._display_dims[0], max(1, int(image.shape[0] * scale)))
        else:
            scale = self._display_dims[1] / float(image.shape[0])
            size = (max(1, int(image.shape[1] * scale)), self._display_dims[1])
        logger.trace("scale: %s, size: %s", scale, size)  # type: ignore
        return size

    def _build_faces_image(self) -> None:
        """Compile the source and destination rows of the preview image."""
        logger.trace("Building Faces Image")  # type: ignore
        update_all = self.update_source
        self._faces_from_frames()
        if update_all:
            header = self._header_text()
            source = np.hstack([self._draw_rect(face) for face in self._faces.src])
            self._faces_source = np.vstack((header, source))
        self._faces_dest = np.hstack(
            [self._draw_rect(face) for face in self._faces.dst]
        )
        logger.debug(
            "source row shape: %s, swapped row shape: %s",
            self._faces_dest.shape,
            self._faces_source.shape,
        )

    def _faces_from_frames(self) -> None:
        """Extract the preview faces from the source frames and apply the requisite padding."""
        logger.debug(
            "Extracting faces from frames: Number images: %s", len(self.source)
        )
        if self.update_source:
            self._crop_source_faces()
        self._crop_destination_faces()
        logger.debug(
            "Extracted faces from frames: %s",
            {k: len(v) for k, v in self._faces.__dict__.items()},
        )

    def _crop_source_faces(self) -> None:
        """Extract the source faces from the source frames, along with their filenames and the
        transformation matrix used to extract the faces."""
        logger.debug("Updating source faces")
        self._faces = _Faces()  # Init new class
        for item in self.source:
            detected_face = item.inbound.detected_faces[0]
            src_img = item.inbound.image
            detected_face.load_aligned(
                src_img,
                size=self._size,
                centering=cast("CenteringType", self._centering),
            )
            matrix = detected_face.aligned.matrix
            self._faces.filenames.append(os.path.splitext(item.inbound.filename)[0])
            self._faces.matrix.append(matrix)
            self._faces.src.append(
                transform_image(src_img, matrix, self._size, self._padding)
            )
        self.update_source = False
        logger.debug("Updated source faces")

    def _crop_destination_faces(self) -> None:
        """Extract the swapped faces from the swapped frames using the source face destination
        matrices."""
        logger.debug("Updating destination faces")
        self._faces.dst = []
        destination = (
            self.destination
            if self.destination
            else [np.ones_like(src.inbound.image) for src in self.source]
        )
        for idx, image in enumerate(destination):
            self._faces.dst.append(
                transform_image(
                    image, self._faces.matrix[idx], self._size, self._padding
                )
            )
        logger.debug("Updated destination faces")

    def _header_text(self) -> np.ndarray:
        """Create the header text displaying the frame name for each preview column.

        Returns
        -------
        :class:`numpy.ndarray`
            The header row of the preview image containing the frame names for each column
        """
        font_scale = self._size / 640
        height = self._size // 8
        font = cv2.FONT_HERSHEY_SIMPLEX
        # Get size of placed text for positioning
        text_sizes = [
            cv2.getTextSize(self._faces.filenames[idx], font, font_scale, 1)[0]
            for idx in range(self._total_columns)
        ]
        # Get X and Y co-ordinates for each text item
        text_y = int((height + text_sizes[0][1]) / 2)
        text_x = [
            int((self._size - text_sizes[idx][0]) / 2) + self._size * idx
            for idx in range(self._total_columns)
        ]
        logger.debug(
            "filenames: %s, text_sizes: %s, text_x: %s, text_y: %s",
            self._faces.filenames,
            text_sizes,
            text_x,
            text_y,
        )
        header_box = (
            np.ones((height, self._size * self._total_columns, 3), np.uint8) * 255
        )
        for idx, text in enumerate(self._faces.filenames):
            cv2.putText(
                header_box,
                text,
                (text_x[idx], text_y),
                font,
                font_scale,
                (0, 0, 0),
                1,
                lineType=cv2.LINE_AA,
            )
        logger.debug("header_box.shape: %s", header_box.shape)
        return header_box

    def _draw_rect(self, image: np.ndarray) -> np.ndarray:
        """Place a white border around a given image.

        Parameters
        ----------
        image: :class:`numpy.ndarray`
            The image to place a border on to
        Returns
        -------
        :class:`numpy.ndarray`
            The given image with a border drawn around the outside
        """
        cv2.rectangle(
            image, (0, 0), (self._size - 1, self._size - 1), (255, 255, 255), 1
        )
        image = np.clip(image, 0.0, 255.0)
        return image.astype("uint8")


class ConfigTools:
    """Tools for loading, saving, setting and retrieving configuration file values.

    Attributes
    ----------
    tk_vars: dict
        Global tkinter variables. `Refresh` and `Busy` :class:`tkinter.BooleanVar`
    """

    def __init__(self) -> None:
        self._config = Config(None)
        self.tk_vars: dict[
            str, dict[str, tk.BooleanVar | tk.StringVar | tk.IntVar | tk.DoubleVar]
        ] = {}
        self._config_dicts = self._get_config_dicts()  # Holds currently saved config

    @property
    def config(self) -> Config:
        """:class:`plugins.convert._config.Config` The convert configuration"""
        return self._config

    @property
    def config_dicts(self) -> dict[str, Any]:
        """dict: The convert configuration options in dictionary form."""
        return self._config_dicts

    @property
    def sections(self) -> list[str]:
        """list: The sorted section names that exist within the convert Configuration options."""
        return sorted(
            {
                plugin.split(".")[0]
                for plugin in self._config.config.sections()
                if plugin.split(".")[0] != "writer"
            }
        )

    @property
    def plugins_dict(self) -> dict[str, list[str]]:
        """dict: Dictionary of configuration option sections as key with a list of containing
        plugins as the value"""
        return {
            section: sorted(
                [
                    plugin.split(".")[1]
                    for plugin in self._config.config.sections()
                    if plugin.split(".")[0] == section
                ]
            )
            for section in self.sections
        }

    def update_config(self) -> None:
        """Update :attr:`config` with the currently selected values from the GUI."""
        for section, items in self.tk_vars.items():
            for item, value in items.items():
                try:
                    new_value = str(value.get())
                except tk.TclError as err:
                    # When manually filling in text fields, blank values will
                    # raise an error on numeric data types so return 0
                    logger.debug(
                        "Error getting value. Defaulting to 0. Error: %s", str(err)
                    )
                    new_value = str(0)
                old_value = self._config.config[section][item]
                if new_value != old_value:
                    logger.trace(
                        "Updating config: %s, %s from %s to %s",  # type: ignore
                        section,
                        item,
                        old_value,
                        new_value,
                    )
                    self._config.config[section][item] = new_value

    def _get_config_dicts(self) -> dict[str, dict[str, Any]]:
        """Obtain a custom configuration dictionary for convert configuration items in use
        by the preview tool formatted for control helper.

        Returns
        -------
        dict
            Each configuration section as keys, with the values as a dict of option:
            :class:`lib.gui.control_helper.ControlOption` pairs."""
        logger.debug("Formatting Config for GUI")
        config_dicts: dict[str, dict[str, Any]] = {}
        for section in self._config.config.sections():
            if section.startswith("writer."):
                continue
            for key, val in self._config.defaults[section].items():
                if key == "helptext":
                    config_dicts.setdefault(section, {})[key] = val
                    continue
                cp_option = ControlPanelOption(
                    title=key,
                    dtype=val["type"],
                    group=val["group"],
                    default=val["default"],
                    initial_value=self._config.get(section, key),
                    choices=val["choices"],
                    is_radio=val["gui_radio"],
                    rounding=val["rounding"],
                    min_max=val["min_max"],
                    helptext=val["helptext"],
                )
                self.tk_vars.setdefault(section, {})[key] = cp_option.tk_var
                config_dicts.setdefault(section, {})[key] = cp_option
        logger.debug("Formatted Config for GUI: %s", config_dicts)
        return config_dicts

    def reset_config_to_saved(self, section: str | None = None) -> None:
        """Reset the GUI parameters to their saved values within the configuration file.

        Parameters
        ----------
        section: str, optional
            The configuration section to reset the values for, If ``None`` provided then all
            sections are reset. Default: ``None``
        """
        logger.debug("Resetting to saved config: %s", section)
        sections = [section] if section is not None else list(self.tk_vars.keys())
        for config_section in sections:
            for item, options in self._config_dicts[config_section].items():
                if item == "helptext":
                    continue
                val = options.value
                if val != self.tk_vars[config_section][item].get():
                    self.tk_vars[config_section][item].set(val)
                    logger.debug(
                        "Setting %s - %s to saved value %s", config_section, item, val
                    )
        logger.debug("Reset to saved config: %s", section)

    def reset_config_to_default(self, section: str | None = None) -> None:
        """Reset the GUI parameters to their default configuration values.

        Parameters
        ----------
        section: str, optional
            The configuration section to reset the values for, If ``None`` provided then all
            sections are reset. Default: ``None``
        """
        logger.debug("Resetting to default: %s", section)
        sections = [section] if section is not None else list(self.tk_vars.keys())
        for config_section in sections:
            for item, options in self._config_dicts[config_section].items():
                if item == "helptext":
                    continue
                default = options.default
                if default != self.tk_vars[config_section][item].get():
                    self.tk_vars[config_section][item].set(default)
                    logger.debug(
                        "Setting %s - %s to default value %s",
                        config_section,
                        item,
                        default,
                    )
        logger.debug("Reset to default: %s", section)

    def save_config(self, section: str | None = None) -> None:
        """Save the configuration ``.ini`` file with the currently stored values.

        Notes
        -----
        We cannot edit the existing saved config as comments tend to get removed, so we create
        a new config and populate that.

        Parameters
        ----------
        section: str, optional
            The configuration section to save, If ``None`` provided then all sections are saved.
            Default: ``None``
        """
        logger.debug("Saving %s config", section)

        new_config = ConfigParser(allow_no_value=True)

        for config_section, items in self._config.defaults.items():
            logger.debug("Adding section: '%s')", config_section)
            self._config.insert_config_section(
                config_section, items["helptext"], config=new_config
            )
            for item, options in items.items():
                if item == "helptext":
                    continue  # helptext already written at top
                if (
                    section is not None and config_section != section
                ) or config_section not in self.tk_vars:
                    # retain saved values that have not been updated
                    new_opt = self._config.get(config_section, item)
                    logger.debug(
                        "Retaining option: (item: '%s', value: '%s')", item, new_opt
                    )
                else:
                    new_opt = self.tk_vars[config_section][item].get()
                    logger.debug(
                        "Setting option: (item: '%s', value: '%s')", item, new_opt
                    )

                    # Set config_dicts value to new saved value
                    self._config_dicts[config_section][item].set_initial_value(new_opt)

                helptext = self._config.format_help(
                    options["helptext"], is_section=False
                )
                new_config.set(config_section, helptext)
                new_config.set(config_section, item, str(new_opt))

        self._config.config = new_config
        self._config.save_config()
        logger.info("Saved config: '%s'", self._config.configfile)


class ImagesCanvas(ttk.Frame):  # pylint:disable=too-many-ancestors
    """tkinter Canvas that holds the preview images.

    Parameters
    ----------
    parent: tkinter object
        The parent tkinter object that holds the canvas
    tk_vars: dict
        Global tkinter variables. `Refresh` and `Busy` :class:`tkinter.BooleanVar`
    """

    def __init__(
        self,
        parent: ttk.PanedWindow,
        tk_vars: dict[Literal["refresh", "busy"], tk.BooleanVar],
    ) -> None:
        logger.debug(
            "Initializing %s: (parent: %s,  tk_vars: %s)",
            self.__class__.__name__,
            parent,
            tk_vars,
        )
        super().__init__(parent)
        self.pack(expand=True, fill=tk.BOTH, padx=2, pady=2)

        self._refresh_display_trigger = tk_vars["refresh"]
        self._refresh_display_trigger.trace("w", self._refresh_display_callback)
        self._display: FacesDisplay = parent.preview_display  # type: ignore
        self._canvas = tk.Canvas(self, bd=0, highlightthickness=0)
        self._canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._displaycanvas = self._canvas.create_image(
            0, 0, image=self._display.tk_image, anchor=tk.NW
        )
        self.bind("<Configure>", self._resize)
        logger.debug("Initialized %s", self.__class__.__name__)

    def _refresh_display_callback(self, *args) -> None:
        """Add a trace to refresh display on callback"""
        if not self._refresh_display_trigger.get():
            return
        logger.trace("Refresh display trigger received: %s", args)  # type: ignore
        self._reload()

    def _resize(self, event: tk.Event) -> None:
        """Resize the image to fit the frame, maintaining aspect ratio"""
        logger.trace("Resizing preview image")  # type: ignore
        framesize = (event.width, event.height)
        self._display.set_display_dimensions(framesize)
        self._reload()

    def _reload(self) -> None:
        """Reload the preview image"""
        logger.trace("Reloading preview image")  # type: ignore
        self._display.update_tk_image()
        self._canvas.itemconfig(self._displaycanvas, image=self._display.tk_image)


class ActionFrame(ttk.Frame):  # pylint: disable=too-many-ancestors
    """Frame that holds the left hand side options panel containing the command line options.

    Parameters
    ----------
    parent: tkinter object
        The parent tkinter object that holds the Action Frame
    available_masks: list
        The available masks that exist within the alignments file
    has_predicted_mask: bool
        Whether the model was trained with a mask
    selected_color: str
        The selected color adjustment type
    selected_mask_type: str
        The selected mask type
    config_tools: :class:`ConfigTools`
        Tools for loading and saving configuration files
    patch_callback: python function
        The function to execute when a patch callback is received
    refresh_callback: python function
        The function to execute when a refresh callback is received
    tk_vars: dict
        Global tkinter variables. `Refresh` and `Busy` :class:`tkinter.BooleanVar`
    """

    def __init__(
        self,
        parent: ttk.Frame,
        available_masks: list[str],
        has_predicted_mask: bool,
        selected_color: str,
        selected_mask_type: str,
        config_tools: ConfigTools,
        patch_callback: Callable[[], None],
        refresh_callback: Callable[[], None],
        tk_vars: dict[Literal["refresh", "busy"], tk.BooleanVar],
    ) -> None:
        logger.debug(
            "Initializing %s: (available_masks: %s, has_predicted_mask: %s, "
            "selected_color: %s, selected_mask_type: %s, patch_callback: %s, "
            "refresh_callback: %s, tk_vars: %s)",
            self.__class__.__name__,
            available_masks,
            has_predicted_mask,
            selected_color,
            selected_mask_type,
            patch_callback,
            refresh_callback,
            tk_vars,
        )
        self._config_tools = config_tools

        super().__init__(parent)
        self.pack(side=tk.LEFT, anchor=tk.N, fill=tk.Y)
        self._options = ["color", "mask_type"]
        self._busy_tkvar = tk_vars["busy"]
        self._tk_vars: dict[str, tk.StringVar] = {}

        d_locals = locals()
        defaults = {
            opt: self._format_to_display(d_locals[f"selected_{opt}"])
            for opt in self._options
        }
        self._busy_indicator = self._build_frame(
            defaults,
            refresh_callback,
            patch_callback,
            available_masks,
            has_predicted_mask,
        )

    @property
    def convert_args(self) -> dict[str, Any]:
        """dict: Currently selected Command line arguments from the :class:`ActionFrame`."""
        return {
            opt
            if opt != "color"
            else "color_adjustment": self._format_from_display(self._tk_vars[opt].get())
            for opt in self._options
        }

    @staticmethod
    def _format_from_display(var: str) -> str:
        """Format a variable from the display version to the command line action version.

        Parameters
        ----------
        var: str
            The variable name to format

        Returns
        -------
        str
            The formatted variable name
        """
        return var.replace(" ", "_").lower()

    @staticmethod
    def _format_to_display(var: str) -> str:
        """Format a variable from the command line action version to the display version.
        Parameters
        ----------
        var: str
            The variable name to format

        Returns
        -------
        str
            The formatted variable name
        """
        return var.replace("_", " ").replace("-", " ").title()

    def _build_frame(
        self,
        defaults: dict[str, Any],
        refresh_callback: Callable[[], None],
        patch_callback: Callable[[], None],
        available_masks: list[str],
        has_predicted_mask: bool,
    ) -> ttk.Progressbar:
        """Build the :class:`ActionFrame`.

        Parameters
        ----------
        defaults: dict
            The default command line options
        patch_callback: python function
            The function to execute when a patch callback is received
        refresh_callback: python function
            The function to execute when a refresh callback is received
        available_masks: list
            The available masks that exist within the alignments file
        has_predicted_mask: bool
            Whether the model was trained with a mask

        Returns
        -------
        ttk.Progressbar
            A Progress bar to indicate that the Preview tool is busy
        """
        logger.debug("Building Action frame")

        bottom_frame = ttk.Frame(self)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, anchor=tk.S)
        top_frame = ttk.Frame(self)
        top_frame.pack(side=tk.TOP, fill=tk.BOTH, anchor=tk.N, expand=True)

        self._add_cli_choices(top_frame, defaults, available_masks, has_predicted_mask)

        busy_indicator = self._add_busy_indicator(bottom_frame)
        self._add_refresh_button(bottom_frame, refresh_callback)
        self._add_patch_callback(patch_callback)
        self._add_actions(bottom_frame)
        logger.debug("Built Action frame")
        return busy_indicator

    def _add_cli_choices(
        self,
        parent: ttk.Frame,
        defaults: dict[str, Any],
        available_masks: list[str],
        has_predicted_mask: bool,
    ) -> None:
        """Create :class:`lib.gui.control_helper.ControlPanel` object for the command
        line options.

        parent: :class:`ttk.Frame`
            The frame to hold the command line choices
        defaults: dict
            The default command line options
        available_masks: list
            The available masks that exist within the alignments file
        has_predicted_mask: bool
            Whether the model was trained with a mask
        """
        cp_options = self._get_control_panel_options(
            defaults, available_masks, has_predicted_mask
        )
        panel_kwargs = dict(blank_nones=False, label_width=10, style="CPanel")
        ControlPanel(parent, cp_options, header_text=None, **panel_kwargs)

    def _get_control_panel_options(
        self,
        defaults: dict[str, Any],
        available_masks: list[str],
        has_predicted_mask: bool,
    ) -> list[ControlPanelOption]:
        """Create :class:`lib.gui.control_helper.ControlPanelOption` objects for the command
        line options.

        defaults: dict
            The default command line options
        available_masks: list
            The available masks that exist within the alignments file
        has_predicted_mask: bool
            Whether the model was trained with a mask

        Returns
        -------
        list
            The list of `lib.gui.control_helper.ControlPanelOption` objects for the Action Frame
        """
        cp_options: list[ControlPanelOption] = []
        for opt in self._options:
            if opt == "mask_type":
                choices = self._create_mask_choices(
                    defaults, available_masks, has_predicted_mask
                )
            else:
                choices = PluginLoader.get_available_convert_plugins(opt, True)
            cp_option = ControlPanelOption(
                title=opt,
                dtype=str,
                default=defaults[opt],
                initial_value=defaults[opt],
                choices=choices,
                group="Command Line Choices",
                is_radio=False,
            )
            self._tk_vars[opt] = cp_option.tk_var
            cp_options.append(cp_option)
        return cp_options

    @classmethod
    def _create_mask_choices(
        cls,
        defaults: dict[str, Any],
        available_masks: list[str],
        has_predicted_mask: bool,
    ) -> list[str]:
        """Set the mask choices and default mask based on available masks.

        Parameters
        ----------
        defaults: dict
            The default command line options
        available_masks: list
            The available masks that exist within the alignments file
        has_predicted_mask: bool
            Whether the model was trained with a mask

        Returns
        -------
        list
            The masks that are available to use from the alignments file
        """
        logger.debug("Initial mask choices: %s", available_masks)
        if has_predicted_mask:
            available_masks += ["predicted"]
        if "none" not in available_masks:
            available_masks += ["none"]
        if defaults["mask_type"] not in available_masks:
            logger.debug(
                "Setting default mask to first available: %s", available_masks[0]
            )
            defaults["mask_type"] = available_masks[0]
        logger.debug("Final mask choices: %s", available_masks)
        return available_masks

    @classmethod
    def _add_refresh_button(
        cls, parent: ttk.Frame, refresh_callback: Callable[[], None]
    ) -> None:
        """Add a button to refresh the images.

        Parameters
        ----------
        refresh_callback: python function
            The function to execute when the refresh button is pressed
        """
        btn = ttk.Button(parent, text="Update Samples", command=refresh_callback)
        btn.pack(padx=5, pady=5, side=tk.TOP, fill=tk.X, anchor=tk.N)

    def _add_patch_callback(self, patch_callback: Callable[[], None]) -> None:
        """Add callback to re-patch images on action option change.

        Parameters
        ----------
        patch_callback: python function
            The function to execute when the images require patching
        """
        for tk_var in self._tk_vars.values():
            tk_var.trace("w", patch_callback)

    def _add_busy_indicator(self, parent: ttk.Frame) -> ttk.Progressbar:
        """Place progress bar into bottom bar to indicate when processing.

        Parameters
        ----------
        parent: tkinter object
            The tkinter object that holds the busy indicator

        Returns
        -------
        ttk.Progressbar
            A Progress bar to indicate that the Preview tool is busy
        """
        logger.debug("Placing busy indicator")
        pbar = ttk.Progressbar(parent, mode="indeterminate")
        pbar.pack(side=tk.LEFT)
        pbar.pack_forget()
        self._busy_tkvar.trace("w", self._busy_indicator_trace)
        return pbar

    def _busy_indicator_trace(self, *args) -> None:
        """Show or hide busy indicator based on whether the preview is updating.

        Parameters
        ----------
        args: unused
            Required for tkinter event, but unused
        """
        logger.trace("Busy indicator trace: %s", args)  # type: ignore
        if self._busy_tkvar.get():
            self._start_busy_indicator()
        else:
            self._stop_busy_indicator()

    def _stop_busy_indicator(self) -> None:
        """Stop and hide progress bar"""
        logger.debug("Stopping busy indicator")
        self._busy_indicator.stop()
        self._busy_indicator.pack_forget()

    def _start_busy_indicator(self) -> None:
        """Start and display progress bar"""
        logger.debug("Starting busy indicator")
        self._busy_indicator.pack(
            side=tk.LEFT, padx=5, pady=(5, 10), fill=tk.X, expand=True
        )
        self._busy_indicator.start()

    def _add_actions(self, parent: ttk.Frame) -> None:
        """Add Action Buttons to the :class:`ActionFrame`

        Parameters
        ----------
        parent: tkinter object
            The tkinter object that holds the action buttons
        """
        logger.debug("Adding util buttons")
        frame = ttk.Frame(parent)
        frame.pack(padx=5, pady=(5, 10), side=tk.RIGHT, fill=tk.X, anchor=tk.E)

        for utl in ("save", "clear", "reload"):
            logger.debug("Adding button: '%s'", utl)
            img = get_images().icons[utl]
            if utl == "save":
                text = _("Save full config")
                action = self._config_tools.save_config
            elif utl == "clear":
                text = _("Reset full config to default values")
                action = self._config_tools.reset_config_to_default
            elif utl == "reload":
                text = _("Reset full config to saved values")
                action = self._config_tools.reset_config_to_saved

            btnutl = ttk.Button(frame, image=img, command=action)
            btnutl.pack(padx=2, side=tk.RIGHT)
            Tooltip(btnutl, text=text, wrap_length=200)
        logger.debug("Added util buttons")


class OptionsBook(ttk.Notebook):  # pylint:disable=too-many-ancestors
    """The notebook that holds the Convert configuration options.

    Parameters
    ----------
    parent: tkinter object
        The parent tkinter object that holds the Options book
    config_tools: :class:`ConfigTools`
        Tools for loading and saving configuration files
    patch_callback: python function
        The function to execute when a patch callback is received

    Attributes
    ----------
    config_tools: :class:`ConfigTools`
        Tools for loading and saving configuration files
    """

    def __init__(
        self,
        parent: ttk.Frame,
        config_tools: ConfigTools,
        patch_callback: Callable[[], None],
    ) -> None:
        logger.debug(
            "Initializing %s: (parent: %s, config: %s)",
            self.__class__.__name__,
            parent,
            config_tools,
        )
        super().__init__(parent)
        self.pack(side=tk.RIGHT, anchor=tk.N, fill=tk.BOTH, expand=True)
        self.config_tools = config_tools

        self._tabs: dict[str, dict[str, ttk.Notebook | ConfigFrame]] = {}
        self._build_tabs()
        self._build_sub_tabs()
        self._add_patch_callback(patch_callback)
        logger.debug("Initialized %s", self.__class__.__name__)

    def _build_tabs(self) -> None:
        """Build the notebook tabs for the each configuration section."""
        logger.debug("Build Tabs")
        for section in self.config_tools.sections:
            tab = ttk.Notebook(self)
            self._tabs[section] = {"tab": tab}
            self.add(tab, text=section.replace("_", " ").title())

    def _build_sub_tabs(self) -> None:
        """Build the notebook sub tabs for each convert section's plugin."""
        for section, plugins in self.config_tools.plugins_dict.items():
            for plugin in plugins:
                config_key = ".".join((section, plugin))
                config_dict = self.config_tools.config_dicts[config_key]
                tab = ConfigFrame(self, config_key, config_dict)
                self._tabs[section][plugin] = tab
                text = plugin.replace("_", " ").title()
                cast(ttk.Notebook, self._tabs[section]["tab"]).add(tab, text=text)

    def _add_patch_callback(self, patch_callback: Callable[[], None]) -> None:
        """Add callback to re-patch images on configuration option change.

        Parameters
        ----------
        patch_callback: python function
            The function to execute when the images require patching
        """
        for plugins in self.config_tools.tk_vars.values():
            for tk_var in plugins.values():
                tk_var.trace("w", patch_callback)


class ConfigFrame(ttk.Frame):  # pylint: disable=too-many-ancestors
    """Holds the configuration options for a convert plugin inside the :class:`OptionsBook`.

    Parameters
    ----------
    parent: tkinter object
        The tkinter object that will hold this configuration frame
    config_key: str
        The section/plugin key for these configuration options
    options: dict
        The options for this section/plugin
    """

    def __init__(self, parent: OptionsBook, config_key: str, options: dict[str, Any]):
        logger.debug("Initializing %s", self.__class__.__name__)
        super().__init__(parent)
        self.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self._options = options

        self._action_frame = ttk.Frame(self)
        self._action_frame.pack(
            padx=0, pady=(0, 5), side=tk.BOTTOM, fill=tk.X, anchor=tk.E
        )
        self._add_frame_separator()

        self._build_frame(parent, config_key)
        logger.debug("Initialized %s", self.__class__.__name__)

    def _build_frame(self, parent: OptionsBook, config_key: str) -> None:
        """Build the options frame for this command

        Parameters
        ----------
        parent: tkinter object
            The tkinter object that will hold this configuration frame
        config_key: str
            The section/plugin key for these configuration options
        """
        logger.debug("Add Config Frame")
        panel_kwargs = dict(
            columns=2, option_columns=2, blank_nones=False, style="CPanel"
        )
        frame = ttk.Frame(self)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        cp_options = [opt for key, opt in self._options.items() if key != "helptext"]
        ControlPanel(frame, cp_options, header_text=None, **panel_kwargs)
        self._add_actions(parent, config_key)
        logger.debug("Added Config Frame")

    def _add_frame_separator(self) -> None:
        """Add a separator between top and bottom frames."""
        logger.debug("Add frame seperator")
        sep = ttk.Frame(self._action_frame, height=2, relief=tk.RIDGE)
        sep.pack(fill=tk.X, pady=5, side=tk.TOP)
        logger.debug("Added frame seperator")

    def _add_actions(self, parent: OptionsBook, config_key: str) -> None:
        """Add Action Buttons.

        Parameters
        ----------
        parent: tkinter object
            The tkinter object that will hold this configuration frame
        config_key: str
            The section/plugin key for these configuration options
        """
        logger.debug("Adding util buttons")

        title = config_key.split(".")[1].replace("_", " ").title()
        btn_frame = ttk.Frame(self._action_frame)
        btn_frame.pack(padx=5, side=tk.BOTTOM, fill=tk.X)
        for utl in ("save", "clear", "reload"):
            logger.debug("Adding button: '%s'", utl)
            img = get_images().icons[utl]
            if utl == "save":
                text = _(f"Save {title} config")
                action = parent.config_tools.save_config
            elif utl == "clear":
                text = _(f"Reset {title} config to default values")
                action = parent.config_tools.reset_config_to_default
            elif utl == "reload":
                text = _(f"Reset {title} config to saved values")
                action = parent.config_tools.reset_config_to_saved

            btnutl = ttk.Button(
                btn_frame, image=img, command=lambda cmd=action: cmd(config_key)
            )  # type: ignore
            btnutl.pack(padx=2, side=tk.RIGHT)
            Tooltip(btnutl, text=text, wrap_length=200)
        logger.debug("Added util buttons")
