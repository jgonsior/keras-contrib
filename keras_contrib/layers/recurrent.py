# -*- coding: utf-8 -*-
"""Recurrent layers and their base classes.
"""
from __future__ import absolute_import, division, print_function

import warnings
from pprint import pprint

import numpy as np
from keras import activations
from keras import backend as K
from keras import constraints, initializers, regularizers
from keras.engine.base_layer import InputSpec, Layer
from keras.layers import LSTM
from keras.layers.recurrent import RNN
from keras.legacy import interfaces
# Legacy support.
from keras.legacy.layers import Recurrent
from keras.models import Sequential
from keras.utils.generic_utils import has_arg, to_list
from tensorflow.python.ops import logging_ops


def print_tensor_long(x, message=""):
    return logging_ops.Print(x, [x], message, summarize=100)


class InsideLSTMCell(Layer):

    activationGate = None
    name = ""

    def __init__(self,
                 units,
                 activation='tanh',
                 recurrent_activation='hard_sigmoid',
                 use_bias=True,
                 kernel_initializer='glorot_uniform',
                 recurrent_initializer='orthogonal',
                 bias_initializer='zeros',
                 unit_forget_bias=True,
                 kernel_regularizer=None,
                 recurrent_regularizer=None,
                 bias_regularizer=None,
                 kernel_constraint=None,
                 recurrent_constraint=None,
                 bias_constraint=None,
                 dropout=0.,
                 recurrent_dropout=0.,
                 implementation=1,
                 activationGate=None,
                 name="",
                 **kwargs):

        self.activationGate = activationGate
        self.name = name

        super(InsideLSTMCell, self).__init__(**kwargs)
        self.units = units
        self.activation = activations.get(activation)
        self.recurrent_activation = activations.get(recurrent_activation)
        self.use_bias = use_bias

        self.kernel_initializer = initializers.get(kernel_initializer)
        self.recurrent_initializer = initializers.get(recurrent_initializer)
        self.bias_initializer = initializers.get(bias_initializer)
        self.unit_forget_bias = unit_forget_bias

        self.kernel_regularizer = regularizers.get(kernel_regularizer)
        self.recurrent_regularizer = regularizers.get(recurrent_regularizer)
        self.bias_regularizer = regularizers.get(bias_regularizer)

        self.kernel_constraint = constraints.get(kernel_constraint)
        self.recurrent_constraint = constraints.get(recurrent_constraint)
        self.bias_constraint = constraints.get(bias_constraint)

        self.dropout = min(1., max(0., dropout))
        self.recurrent_dropout = min(1., max(0., recurrent_dropout))
        self.implementation = implementation
        self.state_size = (self.units, self.units)
        self.output_size = self.units
        self._dropout_mask = None
        self._recurrent_dropout_mask = None

    def build(self, input_shape):
        input_dim = input_shape[-1]

        if type(self.recurrent_initializer).__name__ == 'Identity':

            def recurrent_identity(shape, gain=1.):
                return gain * np.concatenate(
                    [np.identity(shape[0])] * (shape[1] // shape[0]), axis=1)

            self.recurrent_initializer = recurrent_identity

        self.kernel = self.add_weight(
            shape=(input_dim, self.units * 4),
            name='kernel',
            initializer=self.kernel_initializer,
            regularizer=self.kernel_regularizer,
            constraint=self.kernel_constraint)
        self.recurrent_kernel = self.add_weight(
            shape=(self.units, self.units * 4),
            name='recurrent_kernel',
            initializer=self.recurrent_initializer,
            regularizer=self.recurrent_regularizer,
            constraint=self.recurrent_constraint)
        #  self.kernel = print_tensor_long(self.kernel, message="kernel = ")
        #  self.recurrent_kernel = print_tensor_long(
        #  self.recurrent_kernel, message="rernel = ")

        if self.use_bias:
            if self.unit_forget_bias:

                def bias_initializer(_, *args, **kwargs):
                    return K.concatenate([
                        self.bias_initializer((self.units, ), *args, **kwargs),
                        initializers.Ones()((self.units, ), *args, **kwargs),
                        self.bias_initializer((self.units * 2, ), *args,
                                              **kwargs),
                    ])
            else:
                bias_initializer = self.bias_initializer
            self.bias = self.add_weight(
                shape=(self.units * 4, ),
                name='bias',
                initializer=bias_initializer,
                regularizer=self.bias_regularizer,
                constraint=self.bias_constraint)
        else:
            self.bias = None
        self.kernel_i = self.kernel[:, :self.units]
        self.kernel_f = self.kernel[:, self.units:self.units * 2]
        self.kernel_c = self.kernel[:, self.units * 2:self.units * 3]
        self.kernel_o = self.kernel[:, self.units * 3:]

        self.recurrent_kernel_i = self.recurrent_kernel[:, :self.units]
        self.recurrent_kernel_f = (
            self.recurrent_kernel[:, self.units:self.units * 2])
        self.recurrent_kernel_c = (
            self.recurrent_kernel[:, self.units * 2:self.units * 3])
        self.recurrent_kernel_o = self.recurrent_kernel[:, self.units * 3:]

        if self.use_bias:
            self.bias_i = self.bias[:self.units]
            self.bias_f = self.bias[self.units:self.units * 2]
            self.bias_c = self.bias[self.units * 2:self.units * 3]
            self.bias_o = self.bias[self.units * 3:]
        else:
            self.bias_i = None
            self.bias_f = None
            self.bias_c = None
            self.bias_o = None
        self.built = True

    def call(self, inputs, states, training=None):
        if 0 < self.dropout < 1 and self._dropout_mask is None:
            self._dropout_mask = _generate_dropout_mask(
                K.ones_like(inputs), self.dropout, training=training, count=4)
        if (0 < self.recurrent_dropout < 1
                and self._recurrent_dropout_mask is None):
            self._recurrent_dropout_mask = _generate_dropout_mask(
                K.ones_like(states[0]),
                self.recurrent_dropout,
                training=training,
                count=4)

        # dropout matrices for input units
        dp_mask = self._dropout_mask
        # dropout matrices for recurrent units
        rec_dp_mask = self._recurrent_dropout_mask

        h_tm1 = states[0]  # previous memory state
        c_tm1 = states[1]  # previous carry state

        if self.implementation == 1:
            #  if self.activationGate is not None:
            #  inputs = K.print_tensor(inputs, message="inputs = ")
            if 0 < self.dropout < 1.:
                inputs_i = inputs * dp_mask[0]
                inputs_f = inputs * dp_mask[1]
                inputs_c = inputs * dp_mask[2]
                inputs_o = inputs * dp_mask[3]
            else:
                inputs_i = inputs
                inputs_f = inputs
                inputs_c = inputs
                inputs_o = inputs

            x_i = K.dot(inputs_i, self.kernel_i)
            x_f = K.dot(inputs_f, self.kernel_f)
            x_c = K.dot(inputs_c, self.kernel_c)
            x_o = K.dot(inputs_o, self.kernel_o)

            if self.activationGate is not None:
                x_i = print_tensor_long(x_i, message=self.name + "x_i" + ": ")

            if self.use_bias:
                x_i = K.bias_add(x_i, self.bias_i)
                x_f = K.bias_add(x_f, self.bias_f)
                x_c = K.bias_add(x_c, self.bias_c)
                x_o = K.bias_add(x_o, self.bias_o)

            if 0 < self.recurrent_dropout < 1.:
                h_tm1_i = h_tm1 * rec_dp_mask[0]
                h_tm1_f = h_tm1 * rec_dp_mask[1]
                h_tm1_c = h_tm1 * rec_dp_mask[2]
                h_tm1_o = h_tm1 * rec_dp_mask[3]
            else:
                h_tm1_i = h_tm1
                h_tm1_f = h_tm1
                h_tm1_c = h_tm1
                h_tm1_o = h_tm1

            i = self.recurrent_activation(
                x_i + K.dot(h_tm1_i, self.recurrent_kernel_i))

            #i = K.print_tensor(i, message='i = ')
            f = self.recurrent_activation(
                x_f + K.dot(h_tm1_f, self.recurrent_kernel_f))
            #f = K.print_tensor(i, message='f = ')
            c = f * c_tm1 + i * self.activation(
                x_c + K.dot(h_tm1_c, self.recurrent_kernel_c))
            #c = K.print_tensor(i, message='c = ')
            o = self.recurrent_activation(
                x_o + K.dot(h_tm1_o, self.recurrent_kernel_o))
            #o = K.print_tensor(i, message='o = ')
        else:
            if 0. < self.dropout < 1.:
                inputs *= dp_mask[0]
            z = K.dot(inputs, self.kernel)
            if 0. < self.recurrent_dropout < 1.:
                h_tm1 *= rec_dp_mask[0]
            z += K.dot(h_tm1, self.recurrent_kernel)
            if self.use_bias:
                z = K.bias_add(z, self.bias)

            z0 = z[:, :self.units]
            z1 = z[:, self.units:2 * self.units]
            z2 = z[:, 2 * self.units:3 * self.units]
            z3 = z[:, 3 * self.units:]

            i = self.recurrent_activation(z0)
            f = self.recurrent_activation(z1)
            c = f * c_tm1 + i * self.activation(z2)
            o = self.recurrent_activation(z3)

        h = o * self.activation(c)
        if 0 < self.dropout + self.recurrent_dropout:
            if training is None:
                h._uses_learning_phase = True

        return h, [h, c]

    def get_config(self):
        config = {
            'units':
            self.units,
            'activation':
            activations.serialize(self.activation),
            'recurrent_activation':
            activations.serialize(self.recurrent_activation),
            'use_bias':
            self.use_bias,
            'kernel_initializer':
            initializers.serialize(self.kernel_initializer),
            'recurrent_initializer':
            initializers.serialize(self.recurrent_initializer),
            'bias_initializer':
            initializers.serialize(self.bias_initializer),
            'unit_forget_bias':
            self.unit_forget_bias,
            'kernel_regularizer':
            regularizers.serialize(self.kernel_regularizer),
            'recurrent_regularizer':
            regularizers.serialize(self.recurrent_regularizer),
            'bias_regularizer':
            regularizers.serialize(self.bias_regularizer),
            'kernel_constraint':
            constraints.serialize(self.kernel_constraint),
            'recurrent_constraint':
            constraints.serialize(self.recurrent_constraint),
            'bias_constraint':
            constraints.serialize(self.bias_constraint),
            'dropout':
            self.dropout,
            'recurrent_dropout':
            self.recurrent_dropout,
            'implementation':
            self.implementation
        }
        base_config = super(InsideLSTMCell, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class InsideLSTM(RNN):
    @interfaces.legacy_recurrent_support
    def __init__(self,
                 units,
                 activation='tanh',
                 recurrent_activation='hard_sigmoid',
                 use_bias=True,
                 kernel_initializer='glorot_uniform',
                 recurrent_initializer='orthogonal',
                 bias_initializer='zeros',
                 unit_forget_bias=True,
                 kernel_regularizer=None,
                 recurrent_regularizer=None,
                 bias_regularizer=None,
                 activity_regularizer=None,
                 kernel_constraint=None,
                 recurrent_constraint=None,
                 bias_constraint=None,
                 dropout=0.,
                 recurrent_dropout=0.,
                 implementation=1,
                 return_sequences=False,
                 return_state=False,
                 go_backwards=False,
                 stateful=False,
                 unroll=False,
                 activationGate=None,
                 **kwargs):
        if implementation == 0:
            warnings.warn('`implementation=0` has been deprecated, '
                          'and now defaults to `implementation=1`.'
                          'Please update your layer call.')
        if K.backend() == 'theano' and (dropout or recurrent_dropout):
            warnings.warn(
                'RNN dropout is no longer supported with the Theano backend '
                'due to technical limitations. '
                'You can either set `dropout` and `recurrent_dropout` to 0, '
                'or use the TensorFlow backend.')
            dropout = 0.
            recurrent_dropout = 0.
        cell = InsideLSTMCell(
            units,
            activation=activation,
            recurrent_activation=recurrent_activation,
            use_bias=use_bias,
            kernel_initializer=kernel_initializer,
            recurrent_initializer=recurrent_initializer,
            unit_forget_bias=unit_forget_bias,
            bias_initializer=bias_initializer,
            kernel_regularizer=kernel_regularizer,
            recurrent_regularizer=recurrent_regularizer,
            bias_regularizer=bias_regularizer,
            kernel_constraint=kernel_constraint,
            recurrent_constraint=recurrent_constraint,
            bias_constraint=bias_constraint,
            dropout=dropout,
            recurrent_dropout=recurrent_dropout,
            implementation=implementation,
            activationGate=activationGate,
            name=kwargs['name'])
        super(InsideLSTM, self).__init__(
            cell,
            return_sequences=return_sequences,
            return_state=return_state,
            go_backwards=go_backwards,
            stateful=stateful,
            unroll=unroll,
            **kwargs)

        self.activity_regularizer = regularizers.get(activity_regularizer)


def _generate_dropout_mask(ones, rate, training=None, count=1):
    def dropped_inputs():
        return K.dropout(ones, rate)

    if count > 1:
        return [
            K.in_train_phase(dropped_inputs, ones, training=training)
            for _ in range(count)
        ]
    return K.in_train_phase(dropped_inputs, ones, training=training)


def _standardize_args(inputs, initial_state, constants, num_constants):
    """Standardize `__call__` to a single list of tensor inputs.
    When running a model loaded from file, the input tensors
    `initial_state` and `constants` can be passed to `RNN.__call__` as part
    of `inputs` instead of by the dedicated keyword arguments. This method
    makes sure the arguments are separated and that `initial_state` and
    `constants` are lists of tensors (or None).
    # Arguments
        inputs: tensor or list/tuple of tensors
        initial_state: tensor or list of tensors or None
        constants: tensor or list of tensors or None
    # Returns
        inputs: tensor
        initial_state: list of tensors or None
        constants: list of tensors or None
    """
    if isinstance(inputs, list):
        assert initial_state is None and constants is None
        if num_constants is not None:
            constants = inputs[-num_constants:]
            inputs = inputs[:-num_constants]
        if len(inputs) > 1:
            initial_state = inputs[1:]
        inputs = inputs[0]

    def to_list_or_none(x):
        if x is None or isinstance(x, list):
            return x
        if isinstance(x, tuple):
            return list(x)
        return [x]

    initial_state = to_list_or_none(initial_state)
    constantn = to_list_or_none(constants)
    return inputs, initial_state, constants
