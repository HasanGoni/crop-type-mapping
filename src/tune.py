import sys
sys.path.append("./models")

import ray.tune as tune
import argparse
import datetime
import os
import torch
from utils.trainer import Trainer
import ray.tune
from argparse import Namespace
import torch.optim as optim

from train import prepare_dataset, getModel

from ray.tune.schedulers import AsyncHyperBandScheduler, ASHAScheduler
from ray.tune.suggest.bayesopt import BayesOptSearch
from ray.tune.suggest.hyperopt import HyperOptSearch
from hyperopt import hp



def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'experiment', type=str, default="rnn",
        help='experiment name. defines hyperparameter search space and tune dataset function'
             "use 'rnn', 'test_rnn', 'conv1d', or 'test_conv1d'")
    parser.add_argument(
        '-b', '--batchsize', type=int, default=96, help='Batch Size')
    parser.add_argument(
        '-c', '--cpu', type=int, default=2, help='number of CPUs allocated per trial run (default 2)')
    parser.add_argument(
        '-w', '--workers', type=int, default=2, help='cpu workers')
    parser.add_argument(
        '-g', '--gpu', type=float, default=.2,
        help='number of GPUs allocated per trial run (can be float for multiple runs sharing one GPU, default 0.25)')
    parser.add_argument(
        '-r', '--local_dir', type=str, default=os.path.join(os.environ["HOME"],"ray_results"),
        help='ray local dir. defaults to $HOME/ray_results')
    args, _ = parser.parse_known_args()
    return args

def get_hyperparameter_search_space(experiment):
    """
    simple state function to hold the parameter search space definitions for experiments

    :param experiment: experiment name
    :return: ray config dictionary
    """
    if experiment == "rnn":

        space =  dict(
            epochs = 5,
            model = "rnn",
            dataset = "BavarianCrops",
            testids = None,
            classmapping = os.getenv("HOME") + "/data/BavarianCrops/classmapping.csv.gaf.v2",
            samplet=50,
            bidirectional = True,
            trainids=os.getenv("HOME") + "/data/BavarianCrops/ids/random/holl_2018_mt_pilot_train.txt",
            train_on="train",
            test_on="valid",
            trainregions = ["HOLL_2018_MT_pilot"],
            testregions = ["HOLL_2018_MT_pilot"],
            num_layers=hp.choice("num_layers", [1, 2, 3, 4, 5, 6, 7]),
            hidden_dims=hp.choice("hidden_dims", [2**4, 2**5, 2**6, 2**7, 2**8]),
            dropout=hp.uniform("dropout", 0, 1),
            weight_decay=hp.loguniform("weight_decay", -4,-8),
            learning_rate=hp.loguniform("learning_rate", -1,-5),
            )

        try:
            analysis = tune.Analysis(os.path.join(args.local_dir, args.experiment))
            top_runs = analysis.dataframe().sort_values(by="kappa", ascending=False).iloc[:3]
            top_runs.columns = [col.replace("config:","") for col in top_runs.columns]

            params = top_runs[["num_layers","dropout","weight_decay","learning_rate"]]

            points_to_evaluate = list(params.T.to_dict().values())
        except ValueError as e:
            print("could not extraction previous runs from "+os.path.join(args.local_dir, args.experiment))
            points_to_evaluate = None
            pass


        return space, points_to_evaluate

    if experiment == "transformer":

        return dict(
            epochs = 10,
            model = "transformer",
            dataset = "BavarianCrops",
            trainids=os.getenv("HOME") + "/data/BavarianCrops/ids/random/holl_2018_mt_pilot_train.txt",
            testids=None,
            classmapping = os.getenv("HOME") + "/data/BavarianCrops/classmapping.csv.gaf.v2",
            hidden_dims = tune.grid_search([2**7,2**8,2**6]),
            n_heads = tune.grid_search([2,4,6,8]),
            n_layers = tune.grid_search([8,4,2,1]),
            samplet=tune.grid_search([30,50,70]),
            bidirectional = True,
            dropout=tune.grid_search([.25,.50,.75]),
            train_on="train",
            test_on="valid",
            trainregions = ["HOLL_2018_MT_pilot"],
            testregions = ["HOLL_2018_MT_pilot"],
            )

def print_best(top, filename):
    """
    Takes best run from pandas dataframe <top> and writes parameter and accuracy info to a text file
    """
    time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # num_hidden, learning_rate, num_rnn_layers = top.iloc[0].name
    best_run = top.iloc[0]

    param_fmt = "hidden_dims:{hidden_dims}, learning_rate:{learning_rate}, num_layers:{num_layers}"
    param_string = param_fmt.format(hidden_dims=best_run.loc["hidden_dims"],
                                    learning_rate=best_run.loc["learning_rate"],
                                    num_layers=best_run["num_layers"])

    performance_fmt = "accuracy {accuracy:.2f} (+-{std:.2f}) in {folds:.0f} folds"
    perf_string = performance_fmt.format(accuracy=best_run.mean_accuracy,
                                         std=best_run.std_accuracy,
                                         folds=best_run.nfolds)

    print("{time} finished tuning dataset {dataset} {perf_string}, {param_string}".format(time=time,
                                                                                          dataset=best_run.dataset,
                                                                                          perf_string=perf_string,
                                                                                          param_string=param_string),
          file=open(filename, "a"))

class RayTrainer(ray.tune.Trainable):
    def _setup(self, config):

        self.epochs = config["epochs"]

        print(config)

        args = Namespace(**config)
        self.traindataloader, self.validdataloader = prepare_dataset(args)

        args.nclasses = self.traindataloader.dataset.nclasses
        args.seqlength = self.traindataloader.dataset.sequencelength
        args.input_dims = self.traindataloader.dataset.ndims

        self.model = getModel(args)

        if torch.cuda.is_available():
            self.model = self.model.cuda()

        if "model" in config.keys():
            config.pop('model', None)
        #trainer = Trainer(self.model, self.traindataloader, self.validdataloader, **config)

        optimizer = optim.Adam(
            filter(lambda x: x.requires_grad, self.model.parameters()),
            betas=(0.9, 0.999), eps=1e-08, weight_decay=args.weight_decay, lr=args.learning_rate)

        self.trainer = Trainer(self.model, self.traindataloader, self.validdataloader, optimizer=optimizer, **config)

    def _train(self):
        # epoch is used to distinguish training phases. epoch=None will default to (first) cross entropy phase

        # train five epochs and then infer once. to avoid overhead on these small datasets
        for i in range(self.epochs):
            trainstats = self.trainer.train_epoch(epoch=None)

        stats = self.trainer.test_epoch(self.validdataloader, epoch=None)
        stats.pop("inputs")
        stats.pop("ids")
        stats.pop("confusion_matrix")
        stats.pop("probas")

        stats["lossdelta"] = trainstats["loss"] - stats["loss"]
        stats["trainloss"] = trainstats["loss"]

        return stats

    def _save(self, path):
        path = path + ".pth"
        torch.save(self.model.state_dict(), path)
        return path

    def _restore(self, path):
        state_dict = torch.load(path, map_location="cpu")
        self.model.load_state_dict(state_dict)

if __name__=="__main__":
    if not ray.is_initialized():
        ray.init(include_webui=False)

    args = parse_args()

    config, points_to_evaluate = get_hyperparameter_search_space(args.experiment)

    args_dict = vars(args)
    config = {**config, **args_dict}
    args = Namespace(**config)

    algo = HyperOptSearch(
        config,
        max_concurrent=4,
        metric="kappa",
        mode="max",
        points_to_evaluate=points_to_evaluate
    )


    scheduler = AsyncHyperBandScheduler(metric="kappa", mode="max",max_t=60,
        grace_period=2,
        reduction_factor=3,
        brackets=4)


    analysis = tune.run(
        RayTrainer,
        config=config,
        name=args.experiment,
        num_samples=300,
        local_dir=args.local_dir,
        search_alg=algo,
        scheduler=scheduler,
        verbose=True,
        reuse_actors=True,
        resume=True,
        checkpoint_at_end=True,
        global_checkpoint_period=360,
        checkpoint_score_attr="kappa",
        keep_checkpoints_num=5,
        resources_per_trial=dict(cpu=args.cpu, gpu=args.gpu))

    """
        {
            args.experiment: {
                "resources_per_trial": {
                    "cpu": args.cpu,
                    "gpu": args.gpu,
                },
                'stop': {
                    'training_iteration': 1,
                    'time_total_s':3600,
                },
                "run": RayTrainer,
                "num_samples": 1,
                "checkpoint_at_end": False,
                "config": config,
                "local_dir":args.local_dir
            }
        },
        search_alg=algo,
        #scheduler=scheduler,
        verbose=True,)
    """
    print("Best config is", analysis.get_best_config(metric="kappa"))
    analysis.dataframe().to_csv("/tmp/result.csv")

