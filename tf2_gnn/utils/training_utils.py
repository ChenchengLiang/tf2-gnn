import json
import os
import pickle
import random
import sys
import time
from typing import Dict, Any, Optional, Callable, Set, Type

import numpy as np
import tensorflow as tf
from dpu_utils.utils import run_and_debug, RichPath

from ..azure_ml.utils import override_model_params_with_hyperdrive_params
from tf2_gnn import DataFold, GraphDataset, GraphTaskModel, get_known_message_passing_classes
from .task_utils import get_known_tasks, task_name_to_dataset_class, task_name_to_model_class


def make_run_id(model_name: str, task_name: str, run_name: Optional[str] = None) -> str:
    """Choose a run ID, based on the --run-name parameter and the current time."""
    if run_name is not None:
        return run_name
    else:
        return "%s_%s__%s" % (model_name, task_name, time.strftime("%Y-%m-%d_%H-%M-%S"))


def save_model(save_file, model: GraphTaskModel, dataset: GraphDataset) -> None:
    data_to_store = {
        "model_class": model.__class__,
        "model_params": model._params,
        "dataset_class": dataset.__class__,
        "dataset_params": dataset._params,
    }
    with open(save_file, "wb") as out_file:
        pickle.dump(data_to_store, out_file, pickle.HIGHEST_PROTOCOL)
    model.save_weights(save_file, save_format="tf")
    print(f"   (Stored model to {save_file})")


def get_dataset(
    task_name: Optional[str],
    dataset_cls: Optional[Type[GraphDataset]],
    dataset_model_optimised_default_hyperparameters: Dict[str, Any],
    loaded_data_hyperparameters: Dict[str, Any],
    cli_data_hyperparameter_overrides: Dict[str, Any],
) -> GraphDataset:
    if not dataset_cls:
        dataset_cls, dataset_default_hyperparameter_overrides = task_name_to_dataset_class(task_name)
        dataset_params = dataset_cls.get_default_hyperparameters()
        dataset_params.update(dataset_default_hyperparameter_overrides)
        dataset_params.update(dataset_model_optimised_default_hyperparameters)
    else:
        dataset_params = loaded_data_hyperparameters
    dataset_params.update(cli_data_hyperparameter_overrides)
    return dataset_cls(dataset_params)


def get_model(
    msg_passing_implementation: str,
    task_name: str,
    model_cls: Type[GraphTaskModel],
    dataset: GraphDataset,
    dataset_model_optimised_default_hyperparameters: Dict[str, Any],
    loaded_model_hyperparameters: Dict[str, Any],
    cli_model_hyperparameter_overrides: Dict[str, Any],
    hyperdrive_hyperparameter_overrides: Dict[str, str],
) -> GraphTaskModel:
    if not model_cls:
        model_cls, model_default_hyperparameter_overrides = task_name_to_model_class(task_name)
        model_params = model_cls.get_default_hyperparameters(msg_passing_implementation)
        model_params["gnn_message_calculation_class"] = msg_passing_implementation
        model_params.update(model_default_hyperparameter_overrides)
        model_params.update(dataset_model_optimised_default_hyperparameters)
    else:
        model_params = loaded_model_hyperparameters
    model_params.update(cli_model_hyperparameter_overrides)
    override_model_params_with_hyperdrive_params(model_params, hyperdrive_hyperparameter_overrides)
    return model_cls(model_params, num_edge_types=dataset.num_edge_types)


def get_model_and_dataset(
    task_name: Optional[str],
    msg_passing_implementation: Optional[str],
    data_path: RichPath,
    trained_model_file: Optional[str],
    cli_data_hyperparameter_overrides: Optional[str],
    cli_model_hyperparameter_overrides: Optional[str],
    hyperdrive_hyperparameter_overrides: Dict[str, str] = {},
    folds_to_load: Optional[Set[DataFold]] = None,
):
    if trained_model_file:
        with open(trained_model_file, "rb") as in_file:
            data_to_load = pickle.load(in_file)
        model_class = data_to_load["model_class"]
        dataset_class = data_to_load["dataset_class"]
        default_task_model_hypers = {}
    else:
        data_to_load = {}
        model_class, dataset_class = None, None

        # Load potential task-specific defaults:
        default_task_model_hypers = {}
        task_model_default_hypers_file = os.path.join(
            os.path.dirname(__file__), "default_hypers", "%s_%s.json" % (task_name, msg_passing_implementation)
        )
        print(
            f"Trying to load task/model-specific default parameters from {task_model_default_hypers_file} ... ",
            end="",
        )
        if os.path.exists(task_model_default_hypers_file):
            print("File found.")
            with open(task_model_default_hypers_file, "rt") as f:
                default_task_model_hypers = json.load(f)
        else:
            print("File not found, using global defaults.")

    dataset = get_dataset(
        task_name,
        dataset_class,
        default_task_model_hypers.get("task_params", {}),
        data_to_load.get("dataset_params", {}),
        json.loads(cli_data_hyperparameter_overrides or "{}"),
    )
    model = get_model(
        msg_passing_implementation,
        task_name,
        model_class,
        dataset,
        dataset_model_optimised_default_hyperparameters=default_task_model_hypers.get(
            "model_params", {}
        ),
        loaded_model_hyperparameters=data_to_load.get("model_params", {}),
        cli_model_hyperparameter_overrides=json.loads(cli_model_hyperparameter_overrides or "{}"),
        hyperdrive_hyperparameter_overrides=hyperdrive_hyperparameter_overrides or {},
    )

    # Actually load data:
    print(f"Loading data from {data_path}.")
    dataset.load_data(data_path, folds_to_load)

    return dataset, model


def log_line(log_file: str, msg: str):
    with open(log_file, "a") as log_fh:
        log_fh.write(msg + "\n")
    print(msg)


def train(
    model: GraphTaskModel,
    dataset: GraphDataset,
    log_fun: Callable[[str], None],
    run_id: str,
    max_epochs: int,
    patience: int,
    save_dir: str,
    quiet: bool = False,
    aml_run=None,
):
    train_data = dataset.get_tensorflow_dataset(DataFold.TRAIN).prefetch(3)
    valid_data = dataset.get_tensorflow_dataset(DataFold.VALIDATION).prefetch(3)

    save_file = os.path.join(save_dir, f"{run_id}_best.pkl")

    _, _, initial_valid_results = model.run_one_epoch(valid_data, training=False, quiet=quiet)
    best_valid_metric, best_val_str = model.compute_epoch_metrics(initial_valid_results)
    log_fun(f"Initial valid metric: {best_val_str}.")
    save_model(save_file, model, dataset)
    best_valid_epoch = 0
    train_time_start = time.time()
    for epoch in range(1, max_epochs + 1):
        log_fun(f"== Epoch {epoch}")
        train_loss, train_speed, train_results = model.run_one_epoch(
            train_data, training=True, quiet=quiet
        )
        train_metric, train_metric_string = model.compute_epoch_metrics(train_results)
        log_fun(
            f" Train:  {train_loss:.4f} loss | {train_metric_string} | {train_speed:.2f} graphs/s",
        )
        valid_loss, valid_speed, valid_results = model.run_one_epoch(
            valid_data, training=False, quiet=quiet
        )
        valid_metric, valid_metric_string = model.compute_epoch_metrics(valid_results)
        log_fun(
            f" Valid:  {valid_loss:.4f} loss | {valid_metric_string} | {valid_speed:.2f} graphs/s",
        )

        if aml_run is not None:
            aml_run.log("task_train_metric", float(train_metric))
            aml_run.log("train_speed", float(train_speed))
            aml_run.log("task_valid_metric", float(valid_metric))
            aml_run.log("valid_speed", float(valid_speed))

        # Save if good enough.
        if valid_metric < best_valid_metric:
            log_fun(
                f"  (Best epoch so far, target metric decreased to {valid_metric:.5f} from {best_valid_metric:.5f}.)",
            )
            save_model(save_file, model, dataset)
            best_valid_metric = valid_metric
            best_valid_epoch = epoch
        elif epoch - best_valid_epoch >= patience:
            total_time = time.time() - train_time_start
            log_fun(
                f"Stopping training after {patience} epochs without "
                f"improvement on validation metric.",
            )
            log_fun(f"Training took {total_time}s. Best validation metric: {best_valid_metric}",)
            break
    return save_file

def run_train_from_args(args, hyperdrive_hyperparameter_overrides: Dict[str, str] = {}) -> None:
    data_path = RichPath.create(args.data_path, args.azure_info)
    dataset, model = get_model_and_dataset(
        msg_passing_implementation=args.model,
        task_name=args.task,
        data_path=data_path,
        trained_model_file=args.load_saved_model,
        cli_data_hyperparameter_overrides=args.data_param_override,
        cli_model_hyperparameter_overrides=args.model_param_override,
        hyperdrive_hyperparameter_overrides=hyperdrive_hyperparameter_overrides,
        folds_to_load={DataFold.TRAIN, DataFold.VALIDATION},
    )

    # Get the housekeeping going and start logging:
    os.makedirs(args.save_dir, exist_ok=True)
    run_id = make_run_id(args.model, args.task)
    log_file = os.path.join(args.save_dir, f"{run_id}.log")

    def log(msg):
        log_line(log_file, msg)

    log(f"Setting random seed {args.random_seed}.")
    random.seed(args.random_seed)
    np.random.seed(args.random_seed)
    tf.random.set_seed(args.random_seed)
    log(f"Dataset parameters: {json.dumps(dict(dataset._params))}")
    log(f"Model parameters: {json.dumps(dict(model._params))}")

    # Build & train model:
    data_description = dataset.get_batch_tf_data_description()
    model.build(data_description.batch_features_shapes)

    # If needed, load weights for model:
    if args.load_saved_model:
        model.load_weights(args.load_saved_model)

    if args.azureml_logging:
        from azureml.core.run import Run

        aml_run = Run.get_context()
    else:
        aml_run = None

    trained_model_path = train(
        model,
        dataset,
        log_fun=log,
        run_id=run_id,
        max_epochs=args.max_epochs,
        patience=args.patience,
        save_dir=args.save_dir,
        quiet=args.quiet,
        aml_run=aml_run,
    )

    if args.run_test:
        data_path = RichPath.create(args.data_path, args.azure_info)
        log("== Running on test dataset")
        log(f"Loading data from {data_path}.")
        dataset.load_data(data_path, {DataFold.TEST})
        log(f"Restoring best model state from {trained_model_path}.")
        model.load_weights(trained_model_path)
        test_data = dataset.get_tensorflow_dataset(DataFold.TEST)
        _, _, test_results = model.run_one_epoch(test_data, training=False, quiet=args.quiet)
        test_metric, test_metric_string = model.compute_epoch_metrics(test_results)
        log(test_metric_string)
        if aml_run is not None:
            aml_run.log("task_test_metric", float(test_metric))


def get_train_cli_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(description="Train a GNN model.")
    # We use a somewhat horrible trick to support both
    #  train.py --model MODEL --task TASK --data_path DATA_PATH
    # as well as
    #  train.py model task data_path
    # The former is useful because of limitations in AzureML; the latter is nicer to type.
    if "--model" in sys.argv:
        model_param_name, task_param_name, data_path_param_name = "--model", "--task", "--data_path"
    else:
        model_param_name, task_param_name, data_path_param_name = "model", "task", "data_path"

    parser.add_argument(
        model_param_name,
        type=str,
        choices=sorted(get_known_message_passing_classes()),
        help="GNN model type to train.",
    )
    parser.add_argument(
        task_param_name,
        type=str,
        choices=sorted(get_known_tasks()),
        help="Task to train model for.",
    )
    parser.add_argument(data_path_param_name, type=str, help="Directory containing the task data.")
    parser.add_argument(
        "--save-dir",
        dest="save_dir",
        type=str,
        default="trained_model",
        help="Path in which to store the trained model and log.",
    )
    parser.add_argument(
        "--model-params-override",
        dest="model_param_override",
        type=str,
        help="JSON dictionary overriding model hyperparameter values.",
    )
    parser.add_argument(
        "--data-params-override",
        dest="data_param_override",
        type=str,
        help="JSON dictionary overriding data hyperparameter values.",
    )
    parser.add_argument(
        "--max-epochs",
        dest="max_epochs",
        type=int,
        default=10000,
        help="Maximal number of epochs to train for.",
    )
    parser.add_argument(
        "--patience",
        dest="patience",
        type=int,
        default=25,
        help="Maximal number of epochs to continue training without improvement.",
    )
    parser.add_argument(
        "--seed", dest="random_seed", type=int, default=0, help="Random seed to use.",
    )
    parser.add_argument(
        "--run-name", dest="run_name", type=str, help="A human-readable name for this run.",
    )
    parser.add_argument(
        "--azure-info",
        dest="azure_info",
        type=str,
        default="azure_auth.json",
        help="Azure authentication information file (JSON).",
    )
    parser.add_argument(
        "--load-saved-model",
        dest="load_saved_model",
        help="Optional location to load initial model weights from. Should be model stored in earlier run.",
    )
    parser.add_argument(
        "--quiet", dest="quiet", action="store_true", help="Generate less output during training.",
    )
    parser.add_argument(
        "--run-test",
        dest="run_test",
        action="store_true",
        default=True,
        help="Run on testset after training.",
    )
    parser.add_argument(
        "--azureml_logging",
        dest="azureml_logging",
        action="store_true",
        help="Log task results using AML run context.",
    )
    parser.add_argument("--debug", dest="debug", action="store_true", help="Enable debug routines")

    parser.add_argument(
        "--hyperdrive-arg-parse",
        dest="hyperdrive_arg_parse",
        action="store_true",
        help='Enable hyperdrive argument parsing, in which unknown options "--key val" are interpreted as hyperparameter "key" with value "val".',
    )

    return parser