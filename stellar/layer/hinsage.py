# -*- coding: utf-8 -*-
#
# Copyright 2018 Data61, CSIRO
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
Heterogeneous GraphSAGE and compatible aggregator layers

"""
import numpy as np

from keras.engine.topology import Layer
from keras import backend as K, Input
from keras.layers import Lambda, Dropout, Reshape
from keras import activations
from typing import List, Callable, Tuple, Dict, Union, AnyStr
from collections import defaultdict
import itertools as it
import operator as op

from stellar.data.stellargraph import StellarGraphBase


class MeanHinAggregator(Layer):
    """Mean Aggregator for HinSAGE implemented with Keras base layer

    Args:
        output_dim: (int)
        bias: (bool) Use bias in layer or not (Default False)
        act: (Callable) Activation function
    """

    def __init__(
        self,
        output_dim: int = 0,
        bias: bool = False,
        act: Union[Callable, AnyStr] = "relu",
        **kwargs
    ):
        """
        Args:
            output_dim: Output dimension
            bias: Optional bias
            act: name of the activation function to use (must be a Keras activation function),
                or alternatively, a TensorFlow operation.
        """

        self.output_dim = output_dim
        assert output_dim % 2 == 0
        self.half_output_dim = int(output_dim / 2)
        self.has_bias = bias
        self.act = activations.get(act)
        self.nr = None
        self.w_neigh = []
        self.w_self = None
        self.bias = None
        self._initializer = "glorot_uniform"
        super().__init__(**kwargs)

    def get_config(self):
        """Gets class configuration for Keras serialization"""
        config = {
            "output_dim": self.output_dim,
            "bias": self.has_bias,
            "act": activations.serialize(self.act),
        }
        base_config = super().get_config()
        return {**base_config, **config}

    def build(self, input_shape):
        """
        Creates

        Args:
          input_shape:

        Returns:

        """
        # Weight matrix for each type of neighbour
        self.nr = len(input_shape) - 1
        self.w_neigh = [
            self.add_weight(
                name="w_neigh_" + str(r),
                shape=(input_shape[1 + r][3], self.half_output_dim),
                initializer=self._initializer,
                trainable=True,
            )
            for r in range(self.nr)
        ]

        # Weight matrix for self
        self.w_self = self.add_weight(
            name="w_self",
            shape=(input_shape[0][2], self.half_output_dim),
            initializer=self._initializer,
            trainable=True,
        )

        # Optional bias
        if self.has_bias:
            self.bias = self.add_weight(
                name="bias",
                shape=[self.output_dim],
                initializer="zeros",
                trainable=True,
            )

        super().build(input_shape)

    def call(self, x, **kwargs):
        """
        Apply MeanAggregation on input tensors, x

        Args:
          x: Keras Tensor
          **kwargs:

        Returns:
            Keras Tensor representing the aggregated embeddings in the input.
        """
        neigh_means = [K.mean(z, axis=2) for z in x[1:]]

        from_self = K.dot(x[0], self.w_self)
        from_neigh = (
            sum([K.dot(neigh_means[r], self.w_neigh[r]) for r in range(self.nr)])
            / self.nr
        )
        total = K.concatenate(
            [from_self, from_neigh], axis=2
        )  # YT: this corresponds to concat=Partial
        # TODO: implement concat=Full and concat=False

        return self.act(total + self.bias if self.has_bias else total)

    def compute_output_shape(self, input_shape):
        """
        The output shape

        Args:
          input_shape:

        Returns:
            Tuple of the output shape
        """
        return input_shape[0][0], input_shape[0][1], self.output_dim


class HinSAGE:
    """Implementation of the GraphSAGE algorithm extended for heterogeneous graphs with Keras layers."""

    def __init__(
        self,
        output_dims: List[Union[Dict[str, int], int]],
        n_samples: List[int],
        mapper=None,
        target_node_type: AnyStr = None,
        input_neighbor_tree: List[Tuple[str, List[int]]] = None,
        input_dim: Dict[str, int] = None,
        aggregator: Layer = MeanHinAggregator,
        bias: bool = False,
        dropout: float = 0.,
    ):
        """
        Construct aggregator and other supporting layers for HinSAGE

        :param output_dims:         Output dimension at each layer
        :param n_samples:           Number of neighbours sampled for each hop/layer
        :param input_neighbor_tree     Tree structure describing the neighbourhood information of the input
        :param input_dim:           Feature vector dimension
        :param aggregator:          Aggregator class
        :param bias:                Optional bias
        :param dropout:             Optional dropout
        """

        # TODO: I feel that this needs refactoring.
        # Does this assume that the adjacency list is ordered?
        # What are the assumptions of this function, and can we move it to the schema?
        def eval_neigh_tree_per_layer(input_tree):
            """
            Function to evaluate the neighbourhood tree structure for every layer

            Args:
              input_tree: Neighbourhood tree for the input batch

            Returns:
              List of neighbourhood trees

            """

            reduced = [li for li in input_tree if li[1][-1] < len(input_tree)]
            return (
                [input_tree]
                if len(reduced) == 0
                else [input_tree] + eval_neigh_tree_per_layer(reduced)
            )

        assert len(n_samples) == len(output_dims)
        self.n_layers = len(n_samples)
        self.n_samples = n_samples
        self.bias = bias
        self.dropout = dropout

        # Get the sampling tree from the graph, if not given
        # TODO: Let's keep the schema in the graph and fix it when the `fit_attribute_spec` method is called.
        if input_neighbor_tree is None:
            assert mapper is not None

            node_type = (
                mapper.get_head_node_type()
                if target_node_type is None
                else target_node_type
            )

            self.subtree_schema = mapper.schema.get_type_adjacency_list(
                [node_type], len(n_samples)
            )
        else:
            self.subtree_schema = input_neighbor_tree

        # Set the input dimensions
        # TODO: I feel dirty using the graph through the mapper
        if input_dim is None:
            assert mapper is not None
            self.input_dims = mapper.graph.get_feature_sizes()
        else:
            self.input_dims = input_dim

        # Neighbourhood info per layer
        self.neigh_trees = eval_neigh_tree_per_layer(
            [li for li in self.subtree_schema if len(li[1]) > 0]
        )

        # Depth of each input i.e. number of hops from root nodes
        depth = [
            self.n_layers
            + 1
            - sum([1 for li in [self.subtree_schema] + self.neigh_trees if i < len(li)])
            for i in range(len(self.subtree_schema))
        ]

        # Dict of {node type: dimension} per layer
        self.dims = [
            dim
            if isinstance(dim, dict)
            else {k: dim for k, _ in ([self.subtree_schema] + self.neigh_trees)[layer]}
            for layer, dim in enumerate([self.input_dims] + output_dims)
        ]

        # Dict of {node type: aggregator} per layer
        self._aggs = [
            {
                node_type: aggregator(
                    output_dim,
                    bias=self.bias,
                    act="relu" if layer < self.n_layers - 1 else "linear",
                )
                for node_type, output_dim in self.dims[layer + 1].items()
            }
            for layer in range(self.n_layers)
        ]

        # Reshape object per neighbour per node per layer
        self._neigh_reshape = [
            [
                [
                    Reshape(
                        (
                            -1,
                            self.n_samples[depth[i]],
                            self.dims[layer][self.subtree_schema[neigh_index][0]],
                        )
                    )
                    for neigh_index in neigh_indices
                ]
                for i, (_, neigh_indices) in enumerate(self.neigh_trees[layer])
            ]
            for layer in range(self.n_layers)
        ]

        self._normalization = Lambda(lambda x: K.l2_normalize(x, axis=2))

    def __call__(self, x: List):
        """
        Apply aggregator layers

        :param x:       Batch input features
        :return:        Output tensor
        """

        def compose_layers(x: List, layer: int):
            """
            Function to recursively compose aggregation layers. When current layer is at final layer, then
            compose_layers(x, layer) returns x.

            Args:
              x: List of feature matrix tensors
              layer: Current layer index
              x: List:
              layer: int:

            Returns:
              x computed from current layer to output layer

            """

            def neigh_list(i, neigh_indices):
                """


                Args:
                  i:
                  neigh_indices:

                Returns:

                """
                return [
                    self._neigh_reshape[layer][i][ni](x[neigh_index])
                    for ni, neigh_index in enumerate(neigh_indices)
                ]

            def x_next(agg: Dict[str, Layer]):
                """


                Args:
                  agg: Dict[str:
                  Layer]:

                Returns:

                """
                return [
                    agg[node_type](
                        [
                            Dropout(self.dropout)(x[i]),
                            *[
                                Dropout(self.dropout)(ne)
                                for ne in neigh_list(i, neigh_indices)
                            ],
                        ],
                        name="{}_{}".format(node_type, layer),
                    )
                    for i, (node_type, neigh_indices) in enumerate(
                        self.neigh_trees[layer]
                    )
                ]

            return (
                compose_layers(x_next(self._aggs[layer]), layer + 1)
                if layer < self.n_layers
                else x
            )

        x = compose_layers(x, 0)
        return (
            self._normalization(x[0])
            if len(x) == 1
            else [self._normalization(xi) for xi in x]
        )

    def _input_shapes(self) -> List[Tuple[int, int]]:
        """
        Returns the input shapes for the tensors of the supplied neighbourhood type tree

        Returns:
            A list of tuples giving the shape (number of nodes, feature size) for
            the corresponding item in the neighbourhood type tree (self.subtree_schema)
        """
        neighbor_sizes = list(it.accumulate([1] + self.n_samples, op.mul))

        def get_shape(stree, cnode, level=0):
            adj = stree[cnode][1]
            size_dict = {
                cnode: (neighbor_sizes[level], self.input_dims[stree[cnode][0]])
            }
            if len(adj) > 0:
                size_dict.update(
                    {
                        k: s
                        for a in adj
                        for k, s in get_shape(stree, a, level + 1).items()
                    }
                )
            return size_dict

        input_shapes = get_shape(self.subtree_schema, 0)
        return [input_shapes[ii] for ii in range(len(self.subtree_schema))]

    def default_model(self, flatten_output=False):
        """
        Return model with default inputs

        Arg:
            flatten_output: The HinSAGE model returns an output tensor
                of form (batch_size, 1, feature_size) -
                if this flag is True, the output will be resized to
                (batch_size, feature_size)

        Returns:
            x_inp: Keras input tensors for specified HinSAGE model
            y_out: Keras tensor for GraphSAGE model output

        """
        # Create tensor inputs
        x_inp = [Input(shape=s) for s in self._input_shapes()]

        # Output from GraphSAGE model
        x_out = self(x_inp)

        if flatten_output:
            x_out = Reshape((-1,))(x_out)

        return x_inp, x_out
