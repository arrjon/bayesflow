from collections.abc import Mapping, Sequence
import keras
from keras.saving import (
    deserialize_keras_object as deserialize,
    register_keras_serializable as serializable,
    serialize_keras_object as serialize,
)

from bayesflow.data_adapters import ConcatenateKeysDataAdapter, DataAdapter
from bayesflow.data_adapters.transforms import Standardize, Transform
from bayesflow.networks import InferenceNetwork, SummaryNetwork
from bayesflow.types import Shape, Tensor
from bayesflow.utils import logging, expand_tile

from .approximator import Approximator


@serializable(package="bayesflow.approximators")
class ContinuousApproximator(Approximator):
    """
    Defines a workflow for performing fast posterior or likelihood inference.
    The distribution is approximated with an inference network and an optional summary network.
    """

    def __init__(
        self,
        *,
        data_adapter: DataAdapter,
        inference_network: InferenceNetwork,
        summary_network: SummaryNetwork = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.data_adapter = data_adapter
        self.inference_network = inference_network
        self.summary_network = summary_network

    @classmethod
    def build_data_adapter(
        cls,
        inference_variables: Sequence[str],
        inference_conditions: Sequence[str] = None,
        summary_variables: Sequence[str] = None,
        transforms: Sequence[Transform] | None = "default",
    ) -> DataAdapter:
        variables = {
            "inference_variables": inference_variables,
            "inference_conditions": inference_conditions,
            "summary_variables": summary_variables,
        }
        variables = {key: value for key, value in variables.items() if value is not None}

        if transforms == "default":
            transforms = [Standardize()]

        return ConcatenateKeysDataAdapter(**variables, transforms=transforms)

    def compile(
        self,
        *args,
        inference_metrics: Sequence[keras.Metric] = None,
        summary_metrics: Sequence[keras.Metric] = None,
        **kwargs,
    ):
        if inference_metrics:
            self.inference_network._metrics = inference_metrics

        if summary_metrics:
            if self.summary_network is None:
                logging.warning("Ignoring summary metrics because there is no summary network.")
            else:
                self.summary_network._metrics = summary_metrics

        return super().compile(*args, **kwargs)

    def compute_metrics(
        self,
        inference_variables: Tensor,
        inference_conditions: Tensor = None,
        summary_variables: Tensor = None,
        stage: str = "training",
    ) -> dict[str, Tensor]:
        if self.summary_network is None:
            summary_metrics = {}
        else:
            summary_metrics = self.summary_network.compute_metrics(summary_variables, stage=stage)
            summary_outputs = summary_metrics.pop("outputs")

            # append summary outputs to inference conditions
            if inference_conditions is None:
                inference_conditions = summary_outputs
            else:
                inference_conditions = keras.ops.concatenate([inference_conditions, summary_outputs], axis=-1)

        inference_metrics = self.inference_network.compute_metrics(
            inference_variables, conditions=inference_conditions, stage=stage
        )

        loss = inference_metrics.get("loss", keras.ops.zeros(())) + summary_metrics.get("loss", keras.ops.zeros(()))

        inference_metrics = {f"{key}/inference_{key}": value for key, value in inference_metrics.items()}
        summary_metrics = {f"{key}/summary_{key}": value for key, value in summary_metrics.items()}

        metrics = {"loss": loss} | inference_metrics | summary_metrics

        return metrics

    def fit(self, *args, **kwargs):
        return super().fit(*args, **kwargs, data_adapter=self.data_adapter)

    @classmethod
    def from_config(cls, config, custom_objects=None):
        config["data_adapter"] = deserialize(config["data_adapter"], custom_objects=custom_objects)
        config["inference_network"] = deserialize(config["inference_network"], custom_objects=custom_objects)
        config["summary_network"] = deserialize(config["summary_network"], custom_objects=custom_objects)

        return super().from_config(config, custom_objects=custom_objects)

    def get_config(self):
        base_config = super().get_config()
        config = {
            "data_adapter": serialize(self.data_adapter),
            "inference_network": serialize(self.inference_network),
            "summary_network": serialize(self.summary_network),
        }

        return base_config | config

    def sample(
        self,
        *,
        conditions: Mapping[str, Tensor],
        num_samples: int = None,
        batch_shape: Shape = None,
    ) -> dict[str, Tensor]:
        if num_samples is None and batch_shape is None:
            num_samples = 1
        elif batch_shape is not None and num_samples is not None:
            raise ValueError("Please specify either `num_samples` or `batch_shape`, not both.")

        conditions = self.data_adapter.configure(conditions)
        conditions = keras.tree.map_structure(keras.ops.convert_to_tensor, conditions)
        conditions = {
            "inference_variables": self._sample(num_samples=num_samples, batch_shape=batch_shape, **conditions)
        }
        conditions = keras.tree.map_structure(keras.ops.convert_to_numpy, conditions)
        conditions = self.data_adapter.deconfigure(conditions)

        return conditions

    def _sample(
        self,
        num_samples: int = None,
        batch_shape: Shape = None,
        inference_conditions: Tensor = None,
        summary_variables: Tensor = None,
    ) -> Tensor:
        if self.summary_network is not None:
            summary_outputs = self.summary_network(summary_variables)

            if inference_conditions is None:
                inference_conditions = summary_outputs
            else:
                inference_conditions = keras.ops.concatenate([inference_conditions, summary_outputs], axis=-1)

        if batch_shape is None:
            if inference_conditions is not None:
                batch_shape = (keras.ops.shape(inference_conditions)[0], num_samples)
                inference_conditions = expand_tile(inference_conditions, num_samples, axis=1)
            else:
                batch_shape = (num_samples,)

        return self.inference_network.sample(batch_shape, conditions=inference_conditions)

    def log_prob(self, data: Mapping[str, Tensor], numpy: bool = True) -> Tensor:
        data = self.data_adapter.configure(data)
        log_prob = self._log_prob(**data)

        if numpy:
            log_prob = keras.ops.convert_to_numpy(log_prob)

        return log_prob

    def _log_prob(
        self, inference_variables: Tensor, inference_conditions: Tensor = None, summary_variables: Tensor = None
    ) -> Tensor:
        if self.summary_network is not None:
            summary_outputs = self.summary_network(summary_variables)

            if inference_conditions is None:
                inference_conditions = summary_outputs
            else:
                inference_conditions = keras.ops.concatenate([inference_conditions, summary_outputs], axis=-1)

        return self.inference_network.log_prob(inference_variables, conditions=inference_conditions)
