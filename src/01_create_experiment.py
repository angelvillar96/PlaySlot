"""
Creating experiment directory and initalizing it with defauls
"""

import os
import argparse
import traceback
from lib.config import Config
from lib.logger import Logger
from lib.utils import \
        create_directory, \
        delete_directory, \
        clear_cmd, \
        process_experiment_directory_argument

from CONFIG import CONFIG



def create_experiment_arguments():
    """
    Processing arguments for 01_create_experiment.py
    """
    from configs import get_available_configs
    DATASETS = get_available_configs("datasets")

    parser = argparse.ArgumentParser()
    parser.add_argument(
            "-d", "--exp_directory",
            help="Directory where the experiment folder will be created",
            required=True,
        )
    parser.add_argument(
            "--name",
            help="Name to give to the experiment",
            required=True
        )
    parser.add_argument(
            "--dataset_name",
            help=f"Dataset name to keep in the exp_params: {DATASETS}"
        )
    args = parser.parse_args()
    args.exp_directory = process_experiment_directory_argument(args.exp_directory, create=True)
    return args



def initialize_experiment():
    """
    Creating experiment directory and initalizing it with defauls
    """
    # reading command line args
    args = create_experiment_arguments()
    exp_dir = args.exp_directory
    exp_name = args.name
    exp_path = os.path.join(CONFIG["paths"]["experiments_path"], exp_dir, exp_name)
    if os.path.exists(exp_path):
        print(f"Experiment {exp_name} already exists in path {exp_path}...")
        print("Aborting...")
        exit()
    print(f"Creating experiment {exp_name} in path {exp_path}")

    # creating directories
    create_directory(exp_path)
    _ = Logger(exp_path)  # initialize logger once exp_dir is created
    create_directory(dir_path=exp_path, dir_name="plots")
    create_directory(dir_path=exp_path, dir_name="tboard_logs")
    
    try:
        cfg = Config(exp_path=exp_path)
        cfg.create_exp_config_file(
            model_name="SAVi",
            dataset_name=args.dataset_name,
        )
        print("Experiment created successfully...")
    except Exception:
        print("An error has occurred...\n Removing experiment directory")
        delete_directory(dir_path=exp_path)
        exception = traceback.format_exc()
        print(exception)

    return


if __name__ == "__main__":
    clear_cmd()
    initialize_experiment()

#
