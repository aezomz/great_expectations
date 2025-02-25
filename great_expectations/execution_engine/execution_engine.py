import copy
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import great_expectations.exceptions as ge_exceptions
from great_expectations.core.batch import (
    BatchData,
    BatchDataType,
    BatchMarkers,
    BatchSpec,
)
from great_expectations.core.batch_manager import BatchManager
from great_expectations.core.metric_domain_types import MetricDomainTypes
from great_expectations.core.util import AzureUrl, DBFSPath, GCSUrl, S3Url
from great_expectations.execution_engine.bundled_metric_configuration import (
    BundledMetricConfiguration,
)
from great_expectations.expectations.registry import get_metric_provider
from great_expectations.expectations.row_conditions import (
    RowCondition,
    RowConditionParserType,
)
from great_expectations.util import filter_properties_dict
from great_expectations.validator.metric_configuration import MetricConfiguration

logger = logging.getLogger(__name__)


try:
    import pandas as pd
except ImportError:
    pd = None

    logger.debug(
        "Unable to load pandas; install optional pandas dependency for support."
    )


class NoOpDict:
    def __getitem__(self, item):
        return None

    def __setitem__(self, key, value):
        return None

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def update(self, value):
        return None


class MetricFunctionTypes(Enum):
    VALUE = "value"
    MAP_VALUES = "value"  # "map_values"
    WINDOW_VALUES = "value"  # "window_values"
    AGGREGATE_VALUE = "value"  # "aggregate_value"


class MetricPartialFunctionTypes(Enum):
    MAP_FN = "map_fn"
    MAP_SERIES = "map_series"
    MAP_CONDITION_FN = "map_condition_fn"
    MAP_CONDITION_SERIES = "map_condition_series"
    WINDOW_FN = "window_fn"
    WINDOW_CONDITION_FN = "window_condition_fn"
    AGGREGATE_FN = "aggregate_fn"

    @property
    def metric_suffix(self) -> str:
        if self.name in ["MAP_FN", "MAP_SERIES", "WINDOW_FN"]:
            return "map"

        if self.name in [
            "MAP_CONDITION_FN",
            "MAP_CONDITION_SERIES",
            "WINDOW_CONDITION_FN",
        ]:
            return "condition"

        if self.name in ["AGGREGATE_FN"]:
            return "aggregate_fn"

        return ""


class DataConnectorStorageDataReferenceResolver:
    DATA_CONNECTOR_NAME_TO_STORAGE_NAME_MAP: Dict[str, str] = {
        "InferredAssetS3DataConnector": "S3",
        "ConfiguredAssetS3DataConnector": "S3",
        "InferredAssetGCSDataConnector": "GCS",
        "ConfiguredAssetGCSDataConnector": "GCS",
        "InferredAssetAzureDataConnector": "ABS",
        "ConfiguredAssetAzureDataConnector": "ABS",
        "InferredAssetDBFSDataConnector": "DBFS",
        "ConfiguredAssetDBFSDataConnector": "DBFS",
    }
    STORAGE_NAME_EXECUTION_ENGINE_NAME_PATH_RESOLVERS: Dict[
        Tuple[str, str], Callable
    ] = {
        (
            "S3",
            "PandasExecutionEngine",
        ): lambda template_arguments: S3Url.OBJECT_URL_TEMPLATE.format(
            **template_arguments
        ),
        (
            "S3",
            "SparkDFExecutionEngine",
        ): lambda template_arguments: S3Url.OBJECT_URL_TEMPLATE.format(
            **template_arguments
        ),
        (
            "GCS",
            "PandasExecutionEngine",
        ): lambda template_arguments: GCSUrl.OBJECT_URL_TEMPLATE.format(
            **template_arguments
        ),
        (
            "GCS",
            "SparkDFExecutionEngine",
        ): lambda template_arguments: GCSUrl.OBJECT_URL_TEMPLATE.format(
            **template_arguments
        ),
        (
            "ABS",
            "PandasExecutionEngine",
        ): lambda template_arguments: AzureUrl.AZURE_BLOB_STORAGE_HTTPS_URL_TEMPLATE.format(
            **template_arguments
        ),
        (
            "ABS",
            "SparkDFExecutionEngine",
        ): lambda template_arguments: AzureUrl.AZURE_BLOB_STORAGE_WASBS_URL_TEMPLATE.format(
            **template_arguments
        ),
        (
            "DBFS",
            "SparkDFExecutionEngine",
        ): lambda template_arguments: DBFSPath.convert_to_protocol_version(
            **template_arguments
        ),
        (
            "DBFS",
            "PandasExecutionEngine",
        ): lambda template_arguments: DBFSPath.convert_to_file_semantics_version(
            **template_arguments
        ),
    }

    @staticmethod
    def resolve_data_reference(
        data_connector_name: str,
        execution_engine_name: str,
        template_arguments: dict,
    ):
        """Resolve file path for a (data_connector_name, execution_engine_name) combination."""
        storage_name: str = DataConnectorStorageDataReferenceResolver.DATA_CONNECTOR_NAME_TO_STORAGE_NAME_MAP[
            data_connector_name
        ]
        return DataConnectorStorageDataReferenceResolver.STORAGE_NAME_EXECUTION_ENGINE_NAME_PATH_RESOLVERS[
            (storage_name, execution_engine_name)
        ](
            template_arguments
        )


@dataclass
class SplitDomainKwargs:
    """compute_domain_kwargs, accessor_domain_kwargs when split from domain_kwargs

    The union of compute_domain_kwargs, accessor_domain_kwargs is the input domain_kwargs
    """

    compute: dict
    accessor: dict


class ExecutionEngine(ABC):
    recognized_batch_spec_defaults = set()

    def __init__(
        self,
        name=None,
        caching=True,
        batch_spec_defaults=None,
        batch_data_dict=None,
        validator=None,
    ) -> None:
        self.name = name
        self._validator = validator

        # NOTE: using caching makes the strong assumption that the user will not modify the core data store
        # (e.g. self.spark_df) over the lifetime of the dataset instance
        self._caching = caching
        # NOTE: 20200918 - this is a naive cache; update.
        if self._caching:
            self._metric_cache = {}
        else:
            self._metric_cache = NoOpDict()

        if batch_spec_defaults is None:
            batch_spec_defaults = {}

        batch_spec_defaults_keys = set(batch_spec_defaults.keys())
        if not batch_spec_defaults_keys <= self.recognized_batch_spec_defaults:
            logger.warning(
                f"""Unrecognized batch_spec_default(s): \
{str(batch_spec_defaults_keys - self.recognized_batch_spec_defaults)}
"""
            )

        self._batch_spec_defaults = {
            key: value
            for key, value in batch_spec_defaults.items()
            if key in self.recognized_batch_spec_defaults
        }

        self._batch_manager = BatchManager(execution_engine=self)

        if batch_data_dict is None:
            batch_data_dict = {}

        self._load_batch_data_from_dict(batch_data_dict=batch_data_dict)

        # Gather the call arguments of the present function (and add the "class_name"), filter out the Falsy values, and
        # set the instance "_config" variable equal to the resulting dictionary.
        self._config = {
            "name": name,
            "caching": caching,
            "batch_spec_defaults": batch_spec_defaults,
            "batch_data_dict": batch_data_dict,
            "validator": validator,
            "module_name": self.__class__.__module__,
            "class_name": self.__class__.__name__,
        }
        filter_properties_dict(properties=self._config, clean_falsy=True, inplace=True)

    def configure_validator(self, validator) -> None:
        """Optionally configure the validator as appropriate for the execution engine."""
        pass

    @property
    def config(self) -> dict:
        return self._config

    @property
    def dialect(self):
        return None

    @property
    def batch_manager(self) -> BatchManager:
        """Getter for batch_manager"""
        return self._batch_manager

    def _load_batch_data_from_dict(
        self, batch_data_dict: Dict[str, BatchDataType]
    ) -> None:
        """
        Loads all data in batch_data_dict using cache_batch_data
        """
        batch_id: str
        batch_data: BatchDataType
        for batch_id, batch_data in batch_data_dict.items():
            self.load_batch_data(batch_id=batch_id, batch_data=batch_data)

    def load_batch_data(self, batch_id: str, batch_data: BatchDataType) -> None:
        self._batch_manager.save_batch_data(batch_id=batch_id, batch_data=batch_data)

    def get_batch_data(
        self,
        batch_spec: BatchSpec,
    ) -> Any:
        """Interprets batch_data and returns the appropriate data.

        This method is primarily useful for utility cases (e.g. testing) where
        data is being fetched without a DataConnector and metadata like
        batch_markers is unwanted

        Note: this method is currently a thin wrapper for get_batch_data_and_markers.
        It simply suppresses the batch_markers.
        """
        batch_data, _ = self.get_batch_data_and_markers(batch_spec)
        return batch_data

    @abstractmethod
    def get_batch_data_and_markers(self, batch_spec) -> Tuple[BatchData, BatchMarkers]:
        raise NotImplementedError

    def resolve_metrics(
        self,
        metrics_to_resolve: Iterable[MetricConfiguration],
        metrics: Optional[Dict[Tuple[str, str, str], MetricConfiguration]] = None,
        runtime_configuration: Optional[dict] = None,
    ) -> Dict[Tuple[str, str, str], Any]:
        """resolve_metrics is the main entrypoint for an execution engine. The execution engine will compute the value
        of the provided metrics.

        Args:
            metrics_to_resolve: the metrics to evaluate
            metrics: already-computed metrics currently available to the engine
            runtime_configuration: runtime configuration information

        Returns:
            resolved_metrics (Dict): a dictionary with the values for the metrics that have just been resolved.
        """
        if metrics is None:
            metrics = {}

        resolved_metrics: Dict[Tuple[str, str, str], Any] = {}

        metric_fn_bundle: List[BundledMetricConfiguration] = []

        metric_fn_type: MetricFunctionTypes
        metric_class: "MetricProvider"  # noqa: F821
        metric_fn: Any
        compute_domain_kwargs: dict
        accessor_domain_kwargs: dict
        metric_provider_kwargs: dict
        metric_to_resolve: MetricConfiguration
        metric_dependencies: dict
        k: Tuple[str, str, str]
        v: MetricConfiguration
        for metric_to_resolve in metrics_to_resolve:
            metric_dependencies = {}
            for k, v in metric_to_resolve.metric_dependencies.items():
                if v.id in metrics:
                    metric_dependencies[k] = metrics[v.id]
                elif self._caching and v.id in self._metric_cache:
                    metric_dependencies[k] = self._metric_cache[v.id]
                else:
                    raise ge_exceptions.MetricError(
                        message=f'Missing metric dependency: {str(k)} for metric "{metric_to_resolve.metric_name}".'
                    )

            metric_class, metric_fn = get_metric_provider(
                metric_name=metric_to_resolve.metric_name, execution_engine=self
            )
            metric_provider_kwargs = {
                "cls": metric_class,
                "execution_engine": self,
                "metric_domain_kwargs": metric_to_resolve.metric_domain_kwargs,
                "metric_value_kwargs": metric_to_resolve.metric_value_kwargs,
                "metrics": metric_dependencies,
                "runtime_configuration": runtime_configuration,
            }
            if metric_fn is None:
                try:
                    (
                        metric_fn,
                        compute_domain_kwargs,
                        accessor_domain_kwargs,
                    ) = metric_dependencies.pop("metric_partial_fn")
                except KeyError as e:
                    raise ge_exceptions.MetricError(
                        message=f'Missing metric dependency: {str(e)} for metric "{metric_to_resolve.metric_name}".'
                    )

                metric_fn_bundle.append(
                    BundledMetricConfiguration(
                        metric_configuration=metric_to_resolve,
                        metric_fn=metric_fn,
                        compute_domain_kwargs=compute_domain_kwargs,
                        accessor_domain_kwargs=accessor_domain_kwargs,
                        metric_provider_kwargs=metric_provider_kwargs,
                    )
                )
                continue

            metric_fn_type = getattr(
                metric_fn, "metric_fn_type", MetricFunctionTypes.VALUE
            )
            if metric_fn_type not in [
                MetricPartialFunctionTypes.MAP_FN,
                MetricPartialFunctionTypes.MAP_CONDITION_FN,
                MetricPartialFunctionTypes.WINDOW_FN,
                MetricPartialFunctionTypes.WINDOW_CONDITION_FN,
                MetricPartialFunctionTypes.AGGREGATE_FN,
                MetricFunctionTypes.VALUE,
                MetricPartialFunctionTypes.MAP_SERIES,
                MetricPartialFunctionTypes.MAP_CONDITION_SERIES,
            ]:
                logger.warning(
                    f"Unrecognized metric function type while trying to resolve {str(metric_to_resolve.id)}"
                )

            try:
                # NOTE: DH 20220328: This is where we can introduce the Batch Metrics Store (BMS)
                resolved_metrics[metric_to_resolve.id] = metric_fn(
                    **metric_provider_kwargs
                )
            except Exception as e:
                raise ge_exceptions.MetricResolutionError(
                    message=str(e),
                    failed_metrics=(metric_to_resolve,),
                ) from e

        if len(metric_fn_bundle) > 0:
            try:
                # an engine-specific way of computing metrics together
                # NOTE: DH 20220328: This is where we can introduce the Batch Metrics Store (BMS)
                new_resolved: Dict[
                    Tuple[str, str, str], Any
                ] = self.resolve_metric_bundle(metric_fn_bundle)
                resolved_metrics.update(new_resolved)
            except Exception as e:
                raise ge_exceptions.MetricResolutionError(
                    message=str(e),
                    failed_metrics=[x.metric_configuration for x in metric_fn_bundle],
                ) from e

        if self._caching:
            self._metric_cache.update(resolved_metrics)

        return resolved_metrics

    def resolve_metric_bundle(
        self, metric_fn_bundle
    ) -> Dict[Tuple[str, str, str], Any]:
        """Resolve a bundle of metrics with the same compute domain as part of a single trip to the compute engine."""
        raise NotImplementedError

    def get_domain_records(
        self,
        domain_kwargs: dict,
    ) -> Any:
        """
        get_domain_records computes the full-access data (dataframe or selectable) for computing metrics based on the
        given domain_kwargs and specific engine semantics.

        Returns:
            data corresponding to the compute domain
        """

        raise NotImplementedError

    def get_compute_domain(
        self,
        domain_kwargs: dict,
        domain_type: Union[str, MetricDomainTypes],
    ) -> Tuple[Any, dict, dict]:
        """get_compute_domain computes the optimal domain_kwargs for computing metrics based on the given domain_kwargs
        and specific engine semantics.

        Returns:
            A tuple consisting of three elements:

            1. data corresponding to the compute domain;
            2. a modified copy of domain_kwargs describing the domain of the data returned in (1);
            3. a dictionary describing the access instructions for data elements included in the compute domain
                (e.g. specific column name).

            In general, the union of the compute_domain_kwargs and accessor_domain_kwargs will be the same as the
            domain_kwargs provided to this method.
        """

        raise NotImplementedError

    def add_column_row_condition(
        self, domain_kwargs, column_name=None, filter_null=True, filter_nan=False
    ):
        """EXPERIMENTAL

        Add a row condition for handling null filter.

        Args:
            domain_kwargs: the domain kwargs to use as the base and to which to add the condition
            column_name: if provided, use this name to add the condition; otherwise, will use "column" key from
                table_domain_kwargs
            filter_null: if true, add a filter for null values
            filter_nan: if true, add a filter for nan values
        """
        if filter_null is False and filter_nan is False:
            logger.warning(
                "add_column_row_condition called with no filter condition requested"
            )
            return domain_kwargs

        if filter_nan:
            raise ge_exceptions.GreatExpectationsError(
                "Base ExecutionEngine does not support adding nan condition filters"
            )

        new_domain_kwargs = copy.deepcopy(domain_kwargs)
        assert (
            "column" in domain_kwargs or column_name is not None
        ), "No column provided: A column must be provided in domain_kwargs or in the column_name parameter"
        if column_name is not None:
            column = column_name
        else:
            column = domain_kwargs["column"]

        row_condition = RowCondition(
            condition=f'col("{column}").notnull()',
            condition_type=RowConditionParserType.GE,
        )
        new_domain_kwargs.setdefault("filter_conditions", []).append(row_condition)
        return new_domain_kwargs

    def resolve_data_reference(
        self, data_connector_name: str, template_arguments: dict
    ):
        """Resolve file path for a (data_connector_name, execution_engine_name) combination."""
        return DataConnectorStorageDataReferenceResolver.resolve_data_reference(
            data_connector_name=data_connector_name,
            execution_engine_name=self.__class__.__name__,
            template_arguments=template_arguments,
        )

    def _split_domain_kwargs(
        self,
        domain_kwargs: Dict,
        domain_type: Union[str, MetricDomainTypes],
        accessor_keys: Optional[Iterable[str]] = None,
    ) -> SplitDomainKwargs:
        """Split domain_kwargs for all domain types into compute and accessor domain kwargs.

        Args:
            domain_kwargs: A dictionary consisting of the domain kwargs specifying which data to obtain
            domain_type: an Enum value indicating which metric domain the user would
            like to be using, or a corresponding string value representing it. String types include "identity",
            "column", "column_pair", "table" and "other". Enum types include capitalized versions of these from the
            class MetricDomainTypes.
            accessor_keys: keys that are part of the compute domain but should be ignored when
            describing the domain and simply transferred with their associated values into accessor_domain_kwargs.

        Returns:
            compute_domain_kwargs, accessor_domain_kwargs from domain_kwargs
            The union of compute_domain_kwargs, accessor_domain_kwargs is the input domain_kwargs
        """
        # Extracting value from enum if it is given for future computation
        domain_type = MetricDomainTypes(domain_type)

        # Warning user if accessor keys are in any domain that is not of type table, will be ignored
        if (
            domain_type != MetricDomainTypes.TABLE
            and accessor_keys is not None
            and len(list(accessor_keys)) > 0
        ):
            logger.warning(
                'Accessor keys ignored since Metric Domain Type is not "table"'
            )

        split_domain_kwargs: SplitDomainKwargs
        if domain_type == MetricDomainTypes.TABLE:
            split_domain_kwargs = self._split_table_metric_domain_kwargs(
                domain_kwargs, domain_type, accessor_keys
            )

        elif domain_type == MetricDomainTypes.COLUMN:
            split_domain_kwargs = self._split_column_metric_domain_kwargs(
                domain_kwargs,
                domain_type,
            )

        elif domain_type == MetricDomainTypes.COLUMN_PAIR:
            split_domain_kwargs = self._split_column_pair_metric_domain_kwargs(
                domain_kwargs,
                domain_type,
            )

        elif domain_type == MetricDomainTypes.MULTICOLUMN:
            split_domain_kwargs = self._split_multi_column_metric_domain_kwargs(
                domain_kwargs,
                domain_type,
            )
        else:
            compute_domain_kwargs = copy.deepcopy(domain_kwargs)
            accessor_domain_kwargs = {}
            split_domain_kwargs = SplitDomainKwargs(
                compute_domain_kwargs, accessor_domain_kwargs
            )

        return split_domain_kwargs

    @staticmethod
    def _split_table_metric_domain_kwargs(
        domain_kwargs: Dict,
        domain_type: MetricDomainTypes,
        accessor_keys: Optional[Iterable[str]] = None,
    ) -> SplitDomainKwargs:
        """Split domain_kwargs for table domain types into compute and accessor domain kwargs.

        Args:
            domain_kwargs: A dictionary consisting of the domain kwargs specifying which data to obtain
            domain_type: an Enum value indicating which metric domain the user would
            like to be using.
            accessor_keys: keys that are part of the compute domain but should be ignored when
            describing the domain and simply transferred with their associated values into accessor_domain_kwargs.

        Returns:
            compute_domain_kwargs, accessor_domain_kwargs from domain_kwargs
            The union of compute_domain_kwargs, accessor_domain_kwargs is the input domain_kwargs
        """
        assert (
            domain_type == MetricDomainTypes.TABLE
        ), "This method only supports MetricDomainTypes.TABLE"

        compute_domain_kwargs: Dict = copy.deepcopy(domain_kwargs)
        accessor_domain_kwargs: Dict = {}

        if accessor_keys is not None and len(list(accessor_keys)) > 0:
            for key in accessor_keys:
                accessor_domain_kwargs[key] = compute_domain_kwargs.pop(key)
        if len(domain_kwargs.keys()) > 0:
            # Warn user if kwarg not "normal".
            unexpected_keys: set = set(compute_domain_kwargs.keys()).difference(
                {
                    "batch_id",
                    "table",
                    "row_condition",
                    "condition_parser",
                }
            )
            if len(unexpected_keys) > 0:
                unexpected_keys_str: str = ", ".join(
                    map(lambda element: f'"{element}"', unexpected_keys)
                )
                logger.warning(
                    f"""Unexpected key(s) {unexpected_keys_str} found in domain_kwargs for domain type "{domain_type.value}"."""
                )

        return SplitDomainKwargs(compute_domain_kwargs, accessor_domain_kwargs)

    @staticmethod
    def _split_column_metric_domain_kwargs(
        domain_kwargs: Dict,
        domain_type: MetricDomainTypes,
    ) -> SplitDomainKwargs:
        """Split domain_kwargs for column domain types into compute and accessor domain kwargs.

        Args:
            domain_kwargs: A dictionary consisting of the domain kwargs specifying which data to obtain
            domain_type: an Enum value indicating which metric domain the user would
            like to be using.

        Returns:
            compute_domain_kwargs, accessor_domain_kwargs from domain_kwargs
            The union of compute_domain_kwargs, accessor_domain_kwargs is the input domain_kwargs
        """
        assert (
            domain_type == MetricDomainTypes.COLUMN
        ), "This method only supports MetricDomainTypes.COLUMN"

        compute_domain_kwargs: Dict = copy.deepcopy(domain_kwargs)
        accessor_domain_kwargs: Dict = {}

        if "column" not in compute_domain_kwargs:
            raise ge_exceptions.GreatExpectationsError(
                "Column not provided in compute_domain_kwargs"
            )

        accessor_domain_kwargs["column"] = compute_domain_kwargs.pop("column")

        return SplitDomainKwargs(compute_domain_kwargs, accessor_domain_kwargs)

    @staticmethod
    def _split_column_pair_metric_domain_kwargs(
        domain_kwargs: Dict,
        domain_type: MetricDomainTypes,
    ) -> SplitDomainKwargs:
        """Split domain_kwargs for column pair domain types into compute and accessor domain kwargs.

        Args:
            domain_kwargs: A dictionary consisting of the domain kwargs specifying which data to obtain
            domain_type: an Enum value indicating which metric domain the user would
            like to be using.

        Returns:
            compute_domain_kwargs, accessor_domain_kwargs from domain_kwargs
            The union of compute_domain_kwargs, accessor_domain_kwargs is the input domain_kwargs
        """
        assert (
            domain_type == MetricDomainTypes.COLUMN_PAIR
        ), "This method only supports MetricDomainTypes.COLUMN_PAIR"

        compute_domain_kwargs: Dict = copy.deepcopy(domain_kwargs)
        accessor_domain_kwargs: Dict = {}

        if not ("column_A" in domain_kwargs and "column_B" in domain_kwargs):
            raise ge_exceptions.GreatExpectationsError(
                "column_A or column_B not found within domain_kwargs"
            )

        accessor_domain_kwargs["column_A"] = compute_domain_kwargs.pop("column_A")
        accessor_domain_kwargs["column_B"] = compute_domain_kwargs.pop("column_B")

        return SplitDomainKwargs(compute_domain_kwargs, accessor_domain_kwargs)

    @staticmethod
    def _split_multi_column_metric_domain_kwargs(
        domain_kwargs: Dict,
        domain_type: MetricDomainTypes,
    ) -> SplitDomainKwargs:
        """Split domain_kwargs for multicolumn domain types into compute and accessor domain kwargs.

        Args:
            domain_kwargs: A dictionary consisting of the domain kwargs specifying which data to obtain
            domain_type: an Enum value indicating which metric domain the user would
            like to be using.

        Returns:
            compute_domain_kwargs, accessor_domain_kwargs from domain_kwargs
            The union of compute_domain_kwargs, accessor_domain_kwargs is the input domain_kwargs
        """
        assert (
            domain_type == MetricDomainTypes.MULTICOLUMN
        ), "This method only supports MetricDomainTypes.MULTICOLUMN"

        compute_domain_kwargs: Dict = copy.deepcopy(domain_kwargs)
        accessor_domain_kwargs: Dict = {}

        if "column_list" not in domain_kwargs:
            raise ge_exceptions.GreatExpectationsError(
                "column_list not found within domain_kwargs"
            )

        column_list = compute_domain_kwargs.pop("column_list")

        if len(column_list) < 2:
            raise ge_exceptions.GreatExpectationsError(
                "column_list must contain at least 2 columns"
            )

        accessor_domain_kwargs["column_list"] = column_list

        return SplitDomainKwargs(compute_domain_kwargs, accessor_domain_kwargs)
