#!/usr/bin/env python3
""" Tests for Faceswap Initializers.

Adapted from Keras tests.
"""
from __future__ import annotations

import numpy as np
import pytest
from numpy.testing import assert_allclose

from lib.model import optimizers
from lib.utils import get_backend
from tests.utils import generate_test_data
from tests.utils import to_categorical

if get_backend() == "amd":
    from keras import optimizers as k_optimizers
    from keras.layers import Dense, Activation
    from keras.models import Sequential
else:
    # Ignore linting errors from Tensorflow's thoroughly broken import system
    from tensorflow.keras import (
        optimizers as k_optimizers,
    )  # pylint:disable=import-error
    from tensorflow.keras.layers import (
        Dense,
        Activation,
    )  # noqa pylint:disable=import-error,no-name-in-module
    from tensorflow.keras.models import (
        Sequential,
    )  # pylint:disable=import-error,no-name-in-module


def get_test_data():
    """Obtain randomized test data for training"""
    np.random.seed(1337)
    (x_train, y_train), _ = generate_test_data(
        num_train=1000,
        num_test=200,
        input_shape=(10,),
        classification=True,
        num_classes=2,
    )
    y_train = to_categorical(y_train)
    return x_train, y_train


def _test_optimizer(optimizer, target=0.75):
    x_train, y_train = get_test_data()

    model = Sequential()
    model.add(Dense(10, input_shape=(x_train.shape[1],)))
    model.add(Activation("relu"))
    model.add(Dense(y_train.shape[1]))
    model.add(Activation("softmax"))
    model.compile(
        loss="categorical_crossentropy", optimizer=optimizer, metrics=["accuracy"]
    )

    history = model.fit(x_train, y_train, epochs=2, batch_size=16, verbose=0)
    accuracy = "acc" if get_backend() == "amd" else "accuracy"
    assert history.history[accuracy][-1] >= target
    config = k_optimizers.serialize(optimizer)
    optim = k_optimizers.deserialize(config)
    new_config = k_optimizers.serialize(optim)
    config["class_name"] = config["class_name"].lower()
    new_config["class_name"] = new_config["class_name"].lower()
    assert config == new_config

    # Test constraints.
    if get_backend() == "amd":
        # NB: PlaidML does not support constraints, so this test skipped for AMD backends
        return
    model = Sequential()
    dense = Dense(
        10,
        input_shape=(x_train.shape[1],),
        kernel_constraint=lambda x: 0.0 * x + 1.0,
        bias_constraint=lambda x: 0.0 * x + 2.0,
    )
    model.add(dense)
    model.add(Activation("relu"))
    model.add(Dense(y_train.shape[1]))
    model.add(Activation("softmax"))
    model.compile(
        loss="categorical_crossentropy", optimizer=optimizer, metrics=["accuracy"]
    )
    model.train_on_batch(x_train[:10], y_train[:10])
    kernel, bias = dense.get_weights()
    assert_allclose(kernel, 1.0)
    assert_allclose(bias, 2.0)


@pytest.mark.parametrize("dummy", [None], ids=[get_backend().upper()])
def test_adam(dummy):  # pylint:disable=unused-argument
    """Test for custom Adam optimizer"""
    _test_optimizer(k_optimizers.Adam(), target=0.45)
    _test_optimizer(k_optimizers.Adam(decay=1e-3), target=0.45)


@pytest.mark.parametrize("dummy", [None], ids=[get_backend().upper()])
def test_adabelief(dummy):  # pylint:disable=unused-argument
    """Test for custom Adam optimizer"""
    _test_optimizer(optimizers.AdaBelief(), target=0.20)
