# -*- coding: utf-8 -*-

# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import pickle
import tempfile
from typing import Dict, Optional, Sequence, Union

from google.auth import credentials as auth_credentials
from google.cloud import storage
from google.cloud import aiplatform
from google.cloud.aiplatform import base
from google.cloud.aiplatform import explain
from google.cloud.aiplatform import helpers
from google.cloud.aiplatform import initializer
from google.cloud.aiplatform import models
from google.cloud.aiplatform import utils
from google.cloud.aiplatform.metadata.schema import utils as schema_utils
from google.cloud.aiplatform.metadata.schema.google import (
    artifact_schema as google_artifact_schema,
)
from google.cloud.aiplatform.utils import gcs_utils


_LOGGER = base.Logger(__name__)

_PICKLE_PROTOCOL = 4
_MAX_INPUT_EXAMPLE_ROWS = 5
_FRAMEWORK_SPECS = {
    "sklearn": {
        "save_method": "_save_sklearn_model",
        "load_method": "_load_sklearn_model",
        "model_file": "model.pkl",
    }
}


def save_model(
    model: "sklearn.base.BaseEstimator",  # noqa: F821
    artifact_id: Optional[str] = None,
    *,
    uri: Optional[str] = None,
    input_example: Union[list, dict, "pd.DataFrame", "np.ndarray"] = None,  # noqa: F821
    display_name: Optional[str] = None,
    metadata_store_id: Optional[str] = "default",
    project: Optional[str] = None,
    location: Optional[str] = None,
    credentials: Optional[auth_credentials.Credentials] = None,
) -> google_artifact_schema.ExperimentModel:
    """Saves a ML model into a MLMD artifact.

    Supported model frameworks: sklearn.

    Example usage:
        aiplatform.init(project="my-project", location="my-location", staging_bucket="gs://my-bucket")
        model = LinearRegression()
        model.fit(X, y)
        aiplatform.save_model(model, "my-sklearn-model")

    Args:
        model (sklearn.base.BaseEstimator):
            Required. A machine learning model.
        artifact_id (str):
            Optional. The resource id of the artifact. This id must be globally unique
            in a metadataStore. It may be up to 63 characters, and valid characters
            are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
        uri (str):
            Optional. A gcs directory to save the model file. If not provided,
            `gs://default-bucket/timestamp-uuid-frameworkName-model` will be used.
            If default staging bucket is not set, a new bucket will be created.
        input_example (Union[list, dict, pd.DataFrame, np.ndarray]):
            Optional. An example of a valid model input. Will be stored as a yaml file
            in the gcs uri. Accepts list, dict, pd.DataFrame, and np.ndarray
            The value inside a list must be a scalar or list. The value inside
            a dict must be a scalar, list, or np.ndarray.
        display_name (str):
            Optional. The display name of the artifact.
        metadata_store_id (str):
            Optional. The <metadata_store_id> portion of the resource name with
            the format:
            projects/123/locations/us-central1/metadataStores/<metadata_store_id>/artifacts/<resource_id>
            If not provided, the MetadataStore's ID will be set to "default".
        project (str):
            Optional. Project used to create this Artifact. Overrides project set in
            aiplatform.init.
        location (str):
            Optional. Location used to create this Artifact. Overrides location set in
            aiplatform.init.
        credentials (auth_credentials.Credentials):
            Optional. Custom credentials used to create this Artifact. Overrides
            credentials set in aiplatform.init.

    Returns:
        An ExperimentModel instance.

    Raises:
        ValueError: if model type is not supported.
    """
    framework_name = framework_version = ""
    try:
        import sklearn
    except ImportError:
        pass
    else:
        if isinstance(model, sklearn.base.BaseEstimator):
            framework_name = "sklearn"
            framework_version = sklearn.__version__

    if framework_name not in _FRAMEWORK_SPECS:
        raise ValueError(
            f"Model type {model.__class__.__module__}.{model.__class__.__name__} not supported."
        )

    save_method = globals()[_FRAMEWORK_SPECS[framework_name]["save_method"]]
    model_file = _FRAMEWORK_SPECS[framework_name]["model_file"]

    if not uri:
        staging_bucket = initializer.global_config.staging_bucket
        # TODO(b/264196887)
        if not staging_bucket:
            project = project or initializer.global_config.project
            location = location or initializer.global_config.location
            credentials = credentials or initializer.global_config.credentials

            staging_bucket_name = project + "-vertex-staging-" + location
            client = storage.Client(project=project, credentials=credentials)
            staging_bucket = storage.Bucket(client=client, name=staging_bucket_name)
            if not staging_bucket.exists():
                _LOGGER.info(f'Creating staging bucket "{staging_bucket_name}"')
                staging_bucket = client.create_bucket(
                    bucket_or_name=staging_bucket,
                    project=project,
                    location=location,
                )
            staging_bucket = f"gs://{staging_bucket_name}"

        unique_name = utils.timestamped_unique_name()
        uri = f"{staging_bucket}/{unique_name}-{framework_name}-model"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_model_file = os.path.join(temp_dir, model_file)
        save_method(model, temp_model_file)

        if input_example is not None:
            _save_input_example(input_example, temp_dir)
            predict_schemata = schema_utils.PredictSchemata(
                instance_schema_uri=os.path.join(uri, "instance.yaml")
            )
        else:
            predict_schemata = None
        gcs_utils.upload_to_gcs(temp_dir, uri)

    model_artifact = google_artifact_schema.ExperimentModel(
        framework_name=framework_name,
        framework_version=framework_version,
        model_file=model_file,
        model_class=f"{model.__class__.__module__}.{model.__class__.__name__}",
        predict_schemata=predict_schemata,
        artifact_id=artifact_id,
        uri=uri,
        display_name=display_name,
    )
    model_artifact.create(
        metadata_store_id=metadata_store_id,
        project=project,
        location=location,
        credentials=credentials,
    )

    return model_artifact


def _save_input_example(
    input_example: Union[list, dict, "pd.DataFrame", "np.ndarray"],  # noqa: F821
    path: str,
):
    """Saves an input example into a yaml file in the given path.

    Supported example formats: list, dict, np.ndarray, pd.DataFrame.

    Args:
        input_example (Union[list, dict, np.ndarray, pd.DataFrame]):
            Required. An input example to save. The value inside a list must be
            a scalar or list. The value inside a dict must be a scalar, list, or
            np.ndarray.
        path (str):
            Required. The directory that the example is saved to.

    Raises:
        ImportError: if PyYAML or numpy is not installed.
        ValueError: if input_example is in a wrong format.
    """
    try:
        import numpy as np
    except ImportError:
        raise ImportError(
            "numpy is not installed and is required for saving input examples. "
            "Please install google-cloud-aiplatform[metadata]."
        ) from None

    try:
        import yaml
    except ImportError:
        raise ImportError(
            "PyYAML is not installed and is required for saving input examples."
        ) from None

    example = {}
    if isinstance(input_example, list):
        if all(isinstance(x, list) for x in input_example):
            example = {
                "type": "list",
                "data": input_example[:_MAX_INPUT_EXAMPLE_ROWS],
            }
        elif all(np.isscalar(x) for x in input_example):
            example = {
                "type": "list",
                "data": input_example,
            }
        else:
            raise ValueError("The value inside a list must be a scalar or list.")

    if isinstance(input_example, dict):
        if all(isinstance(x, list) for x in input_example.values()):
            example = {
                "type": "dict",
                "data": {
                    k: v[:_MAX_INPUT_EXAMPLE_ROWS] for k, v in input_example.items()
                },
            }
        elif all(isinstance(x, np.ndarray) for x in input_example.values()):
            example = {
                "type": "dict",
                "data": {
                    k: v[:_MAX_INPUT_EXAMPLE_ROWS].tolist()
                    for k, v in input_example.items()
                },
            }
        elif all(np.isscalar(x) for x in input_example.values()):
            example = {"type": "dict", "data": input_example}
        else:
            raise ValueError(
                "The value inside a dictionary must be a scalar, list, or np.ndarray"
            )

    if isinstance(input_example, np.ndarray):
        example = {
            "type": "numpy.ndarray",
            "data": input_example[:_MAX_INPUT_EXAMPLE_ROWS].tolist(),
        }

    try:
        import pandas as pd

        if isinstance(input_example, pd.DataFrame):
            example = {
                "type": "pandas.DataFrame",
                "data": input_example.head(_MAX_INPUT_EXAMPLE_ROWS).to_dict("list"),
            }
    except ImportError:
        pass

    if not example:
        raise ValueError(
            (
                "Input example type not supported. "
                "Valid example must be a list, dict, np.ndarray, or pd.DataFrame."
            )
        )

    example_file = os.path.join(path, "instance.yaml")
    with open(example_file, "w") as file:
        yaml.dump(
            {"input_example": example}, file, default_flow_style=None, sort_keys=False
        )


def _save_sklearn_model(
    model: "sklearn.base.BaseEstimator",  # noqa: F821
    path: str,
) -> google_artifact_schema.ExperimentModel:
    """Saves a sklearn model.

    Args:
        model (sklearn.base.BaseEstimator):
            Required. A sklearn model.
        path (str):
            Required. The local path to save the model.
    """
    with open(path, "wb") as f:
        pickle.dump(model, f, protocol=_PICKLE_PROTOCOL)


def load_model(
    model: Union[str, google_artifact_schema.ExperimentModel]
) -> "sklearn.base.BaseEstimator":  # noqa: F821
    """Retrieves the original ML model from an ExperimentModel resource.

    Args:
        model (Union[str, google_artifact_schema.ExperimentModel]):
            Required. The id or ExperimentModel instance for the model.

    Returns:
        The original ML model.

    Raises:
        ValueError: if model type is not supported.
    """
    if isinstance(model, str):
        model = aiplatform.get_experiment_model(model)
    framework_name = model.framework_name

    if framework_name not in _FRAMEWORK_SPECS:
        raise ValueError(f"Model type {framework_name} not supported.")

    load_method = globals()[_FRAMEWORK_SPECS[framework_name]["load_method"]]
    model_file = _FRAMEWORK_SPECS[framework_name]["model_file"]

    with tempfile.TemporaryDirectory() as temp_dir:
        source_file_uri = os.path.join(model.uri, model_file)
        destination_file_path = os.path.join(temp_dir, model_file)
        gcs_utils.download_file_from_gcs(source_file_uri, destination_file_path)
        loaded_model = load_method(destination_file_path, model)

    return loaded_model


def _load_sklearn_model(
    model_file: str,
    model_artifact: google_artifact_schema.ExperimentModel,
) -> "sklearn.base.BaseEstimator":  # noqa: F821
    """Loads a sklearn model from local path.

    Args:
        model_file (str):
            Required. A local model file to load.
        model_artifact (google_artifact_schema.ExperimentModel):
            Required. The artifact that saved the model.
    Returns:
        The sklearn model instance.

    Raises:
        ImportError: if sklearn is not installed.
    """
    try:
        import sklearn
    except ImportError:
        raise ImportError(
            "sklearn is not installed and is required for loading models."
        ) from None

    if sklearn.__version__ < model_artifact.framework_version:
        _LOGGER.warning(
            f"The original model was saved via sklearn {model_artifact.framework_version}. "
            f"You are using sklearn {sklearn.__version__}."
            "Attempting to load model..."
        )
    with open(model_file, "rb") as f:
        sk_model = pickle.load(f)

    return sk_model


# TODO(b/264893283)
def register_model(
    model: Union[str, google_artifact_schema.ExperimentModel],
    *,
    model_id: Optional[str] = None,
    parent_model: Optional[str] = None,
    use_gpu: bool = False,
    is_default_version: bool = True,
    version_aliases: Optional[Sequence[str]] = None,
    version_description: Optional[str] = None,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    labels: Optional[Dict[str, str]] = None,
    serving_container_image_uri: Optional[str] = None,
    serving_container_predict_route: Optional[str] = None,
    serving_container_health_route: Optional[str] = None,
    serving_container_command: Optional[Sequence[str]] = None,
    serving_container_args: Optional[Sequence[str]] = None,
    serving_container_environment_variables: Optional[Dict[str, str]] = None,
    serving_container_ports: Optional[Sequence[int]] = None,
    instance_schema_uri: Optional[str] = None,
    parameters_schema_uri: Optional[str] = None,
    prediction_schema_uri: Optional[str] = None,
    explanation_metadata: Optional[explain.ExplanationMetadata] = None,
    explanation_parameters: Optional[explain.ExplanationParameters] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    credentials: Optional[auth_credentials.Credentials] = None,
    encryption_spec_key_name: Optional[str] = None,
    staging_bucket: Optional[str] = None,
    sync: Optional[bool] = True,
    upload_request_timeout: Optional[float] = None,
) -> models.Model:
    """Register an ExperimentModel to Model Registry and returns a Model representing the registered Model resource.

    Args:
        model (Union[str, google_artifact_schema.ExperimentModel]):
            Required. The id or ExperimentModel instance for the model.
        model_id (str):
            Optional. The ID to use for the registered Model, which will
            become the final component of the model resource name.
            This value may be up to 63 characters, and valid characters
            are `[a-z0-9_-]`. The first character cannot be a number or hyphen.
        parent_model (str):
            Optional. The resource name or model ID of an existing model that the
            newly-registered model will be a version of.
            Only set this field when uploading a new version of an existing model.
        use_gpu (str):
            Optional. Whether or not to use GPUs for the serving container. Only
            specify this argument when registering a Tensorflow model and
            'serving_container_image_uri' is not specified.
        is_default_version (bool):
            Optional. When set to True, the newly registered model version will
            automatically have alias "default" included. Subsequent uses of
            this model without a version specified will use this "default" version.

            When set to False, the "default" alias will not be moved.
            Actions targeting the newly-registered model version will need
            to specifically reference this version by ID or alias.

            New model uploads, i.e. version 1, will always be "default" aliased.
        version_aliases (Sequence[str]):
            Optional. User provided version aliases so that a model version
            can be referenced via alias instead of auto-generated version ID.
            A default version alias will be created for the first version of the model.

            The format is [a-z][a-zA-Z0-9-]{0,126}[a-z0-9]
        version_description (str):
            Optional. The description of the model version being uploaded.
        display_name (str):
            Optional. The display name of the Model. The name can be up to 128
            characters long and can be consist of any UTF-8 characters.
        description (str):
            Optional. The description of the model.
        labels (Dict[str, str]):
            Optional. The labels with user-defined metadata to
            organize your Models.
            Label keys and values can be no longer than 64
            characters (Unicode codepoints), can only
            contain lowercase letters, numeric characters,
            underscores and dashes. International characters
            are allowed.
            See https://goo.gl/xmQnxf for more information
            and examples of labels.
        serving_container_image_uri (str):
            Optional. The URI of the Model serving container. A pre-built container
            <https://cloud.google.com/vertex-ai/docs/predictions/pre-built-containers>
            is automatically chosen based on the model's framwork. Set this field to
            override the default pre-built container.
        serving_container_predict_route (str):
            Optional. An HTTP path to send prediction requests to the container, and
            which must be supported by it. If not specified a default HTTP path will
            be used by Vertex AI.
        serving_container_health_route (str):
            Optional. An HTTP path to send health check requests to the container, and which
            must be supported by it. If not specified a standard HTTP path will be
            used by Vertex AI.
        serving_container_command (Sequence[str]):
            Optional. The command with which the container is run. Not executed within a
            shell. The Docker image's ENTRYPOINT is used if this is not provided.
            Variable references $(VAR_NAME) are expanded using the container's
            environment. If a variable cannot be resolved, the reference in the
            input string will be unchanged. The $(VAR_NAME) syntax can be escaped
            with a double $$, ie: $$(VAR_NAME). Escaped references will never be
            expanded, regardless of whether the variable exists or not.
        serving_container_args (Sequence[str]):
            Optional. The arguments to the command. The Docker image's CMD is used if this is
            not provided. Variable references $(VAR_NAME) are expanded using the
            container's environment. If a variable cannot be resolved, the reference
            in the input string will be unchanged. The $(VAR_NAME) syntax can be
            escaped with a double $$, ie: $$(VAR_NAME). Escaped references will
            never be expanded, regardless of whether the variable exists or not.
        serving_container_environment_variables (Dict[str, str]):
            Optional. The environment variables that are to be present in the container.
            Should be a dictionary where keys are environment variable names
            and values are environment variable values for those names.
        serving_container_ports (Sequence[int]):
            Optional. Declaration of ports that are exposed by the container. This field is
            primarily informational, it gives Vertex AI information about the
            network connections the container uses. Listing or not a port here has
            no impact on whether the port is actually exposed, any port listening on
            the default "0.0.0.0" address inside a container will be accessible from
            the network.
        instance_schema_uri (str):
            Optional. Points to a YAML file stored on Google Cloud
            Storage describing the format of a single instance, which
            are used in
            ``PredictRequest.instances``,
            ``ExplainRequest.instances``
            and
            ``BatchPredictionJob.input_config``.
            The schema is defined as an OpenAPI 3.0.2 `Schema
            Object <https://tinyurl.com/y538mdwt#schema-object>`__.
            AutoML Models always have this field populated by AI
            Platform. Note: The URI given on output will be immutable
            and probably different, including the URI scheme, than the
            one given on input. The output URI will point to a location
            where the user only has a read access.
        parameters_schema_uri (str):
            Optional. Points to a YAML file stored on Google Cloud
            Storage describing the parameters of prediction and
            explanation via
            ``PredictRequest.parameters``,
            ``ExplainRequest.parameters``
            and
            ``BatchPredictionJob.model_parameters``.
            The schema is defined as an OpenAPI 3.0.2 `Schema
            Object <https://tinyurl.com/y538mdwt#schema-object>`__.
            AutoML Models always have this field populated by AI
            Platform, if no parameters are supported it is set to an
            empty string. Note: The URI given on output will be
            immutable and probably different, including the URI scheme,
            than the one given on input. The output URI will point to a
            location where the user only has a read access.
        prediction_schema_uri (str):
            Optional. Points to a YAML file stored on Google Cloud
            Storage describing the format of a single prediction
            produced by this Model, which are returned via
            ``PredictResponse.predictions``,
            ``ExplainResponse.explanations``,
            and
            ``BatchPredictionJob.output_config``.
            The schema is defined as an OpenAPI 3.0.2 `Schema
            Object <https://tinyurl.com/y538mdwt#schema-object>`__.
            AutoML Models always have this field populated by AI
            Platform. Note: The URI given on output will be immutable
            and probably different, including the URI scheme, than the
            one given on input. The output URI will point to a location
            where the user only has a read access.
        explanation_metadata (aiplatform.explain.ExplanationMetadata):
            Optional. Metadata describing the Model's input and output for explanation.
            `explanation_metadata` is optional while `explanation_parameters` must be
            specified when used.
            For more details, see `Ref docs <http://tinyurl.com/1igh60kt>`
        explanation_parameters (aiplatform.explain.ExplanationParameters):
            Optional. Parameters to configure explaining for Model's predictions.
            For more details, see `Ref docs <http://tinyurl.com/1an4zake>`
        project (str)
            Project to upload this model to. Overrides project set in
            aiplatform.init.
        location (str)
            Location to upload this model to. Overrides location set in
            aiplatform.init.
        credentials (auth_credentials.Credentials)
            Custom credentials to use to upload this model. Overrides credentials
            set in aiplatform.init.
        encryption_spec_key_name (Optional[str]):
            Optional. The Cloud KMS resource identifier of the customer
            managed encryption key used to protect the model. Has the
            form
            ``projects/my-project/locations/my-region/keyRings/my-kr/cryptoKeys/my-key``.
            The key needs to be in the same region as where the compute
            resource is created.

            If set, this Model and all sub-resources of this Model will be secured by this key.

            Overrides encryption_spec_key_name set in aiplatform.init.
        staging_bucket (str):
            Optional. Bucket to stage local model artifacts. Overrides
            staging_bucket set in aiplatform.init.
        sync (bool):
            Optional. Whether to execute this method synchronously. If False,
            this method will unblock and it will be executed in a concurrent Future.
        upload_request_timeout (float):
            Optional. The timeout for the upload request in seconds.

    Returns:
        model (aiplatform.Model):
            Instantiated representation of the registered model resource.

    Raises:
        ValueError: If the model doesn't have a pre-built container that is
                    suitable for its framework and 'serving_container_image_uri'
                    is not set.
    """
    if isinstance(model, str):
        model = aiplatform.get_experiment_model(model)

    project = project or model.project
    location = location or model.location
    credentials = credentials or model.credentials

    artifact_uri = model.uri
    framework_name = model.framework_name
    framework_version = model.framework_version

    if not serving_container_image_uri:
        if framework_name == "tensorflow" and use_gpu:
            accelerator = "gpu"
        else:
            accelerator = "cpu"
        serving_container_image_uri = helpers._get_closest_match_prebuilt_container_uri(
            framework=framework_name,
            framework_version=framework_version,
            region=location,
            accelerator=accelerator,
        )

    if not display_name:
        display_name = models.Model._generate_display_name(f"{framework_name} model")

    return models.Model.upload(
        serving_container_image_uri=serving_container_image_uri,
        artifact_uri=artifact_uri,
        model_id=model_id,
        parent_model=parent_model,
        is_default_version=is_default_version,
        version_aliases=version_aliases,
        version_description=version_description,
        display_name=display_name,
        description=description,
        labels=labels,
        serving_container_predict_route=serving_container_predict_route,
        serving_container_health_route=serving_container_health_route,
        serving_container_command=serving_container_command,
        serving_container_args=serving_container_args,
        serving_container_environment_variables=serving_container_environment_variables,
        serving_container_ports=serving_container_ports,
        instance_schema_uri=instance_schema_uri,
        parameters_schema_uri=parameters_schema_uri,
        prediction_schema_uri=prediction_schema_uri,
        explanation_metadata=explanation_metadata,
        explanation_parameters=explanation_parameters,
        project=project,
        location=location,
        credentials=credentials,
        encryption_spec_key_name=encryption_spec_key_name,
        staging_bucket=staging_bucket,
        sync=sync,
        upload_request_timeout=upload_request_timeout,
    )
