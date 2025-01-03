import os
import argparse
import random
import warnings

import coolname
from dotenv import load_dotenv

from torchvision.transforms import v2
import torch
import lightning as L
import lightning.pytorch as pl
from lightning.pytorch.tuner import Tuner
from lightning.pytorch.loggers.csv_logs import CSVLogger
from lightning.pytorch.callbacks import EarlyStopping, ModelCheckpoint, StochasticWeightAveraging

from aim.pytorch_lightning import AimLogger
from aim.storage.artifacts.s3_storage import S3ArtifactStorage_clientconfig

# if file is called directly, must set import paths to project root
if __name__ == '__main__':
    import sys, pathlib
    PROJECT_ROOT = pathlib.Path(__file__).parent.parent.absolute()
    if sys.path[0] != str(PROJECT_ROOT): sys.path.insert(0, str(PROJECT_ROOT))

from src.patches.model_summary_patch import ModelSummaryWithGradCallback
from src.multiclass.callbacks import BarPlotMetricAim, PlotConfusionMetricAim, PlotPerclassDropdownAim, \
    LogNormalizedLoss
from src.multiclass.datasets import ImageListsWithLabelIndex
from src.multiclass.models import MulticlassClassifier, get_model_base_transforms, check_model_name

def argparse_init(parser=None):
    if parser is None:
        parser = argparse.ArgumentParser(description='Train an image classifier!')

    # DATASET #
    dataset = parser.add_argument_group(title='Dataset', description=None)
    dataset.add_argument('--classlist', required=True, help='A text file, each line is a class label (the label order is significant)')
    dataset.add_argument('--trainlist', required=True, help='A text file, one sample per line, each sample has a class-index and image path')
    dataset.add_argument('--vallist', required=True, help='Like trainlist, but for validation metrics and early-stopping/overfit-prevention')
    dataset.add_argument('--testlist', help='Like trainlist, but for final test metrics. Optional')
    #dataset.add_argument('--sampler', metavar='MODULE.CLASS', default=None)
    #dataset.add_argument('--is-batch-sampler', action='store_true')
    #dataset.add_argument('--feature-store')
    #dataset.add_argument('--dataset')

    # AUGMENTATION #
    base_augs = parser.add_argument_group(title='Base Augmentation', description=None)
    # TODO this and utils to calculate img-norm, and a way to save expected imgnorm values with model base-transform params like resize
    #base_augs.add_argument('--img-norm', nargs=2, metavar=('MEAN', 'STD'),
    #                   help='Normalize images by MEAN and STD. This is like whitebalancing. '
    #                        'eg1: "0.667 0.161", eg2: "0.056,0.058,0.051 0.067,0.071,0.057"')

    # TRAINING TRANSFORMS #
    train_augs = parser.add_argument_group(title='Training Augmentation', description=None)
    train_augs.add_argument('--flip', choices=['x', 'y', 'xy'],
        help='Training images have 50%% chance of being flipped along the designated axis: (x) vertically, (y) horizontally, (xy) either/both. May optionally specify "+V" to include Validation dataset')

    # TRACKING #
    aimstack = parser.add_argument_group(title='AimLogger', description=None)
    aimstack.add_argument('--run', help='The name of this run. A run name is automatically generated by default')
    aimstack.add_argument('--experiment', help='The broader category/grouping this RUN belongs to')
    aimstack.add_argument('--note', help='Add any kind of note or description to the trained model. Make sure to use quotes "around your message."')
    aimstack.add_argument('--repo', help='Aim repo path. Also see: Aim environment variables.')
    aimstack.add_argument('--artifacts-location', help='Aim Artifacts location. Also see: Aim environment variables.')
    #aimstack.add_argument('--plot', nargs='+', action='append', ...)
    #aimstack.add_argument('--callback', nargs='+', action='append', ...)
    #aimstack.add_argument('--metric', nargs='+', action='append', ...)

    # HYPER PARAMETERS #
    model = parser.add_argument_group(title='Model Parameters')
    #model.add_argument('--module-class', nargs=2, metavar='MODULE.CLASS', default=f'{SupervisedModel}.{SupervisedModel.__name__}')
    model.add_argument('--model', help='Model Class/Module Name or torch model checkpoint file', required=True)  # TODO checkopint file, also check loading from s3
    model.add_argument('--weights', default='DEFAULT', help='''Specify a model's weights. Either "DEFAULT", some specific identifier, or "None" for no-pretrained-weights''')
    model.add_argument('--seed', type=int, help='Set a specific seed for deterministic output')
    model.add_argument('--batch', dest='batch_size', metavar='SIZE', default=256, type=int, help='Number of images per batch. Defaults is 256')
    model.add_argument('--num-classes', type=int, help=argparse.SUPPRESS)
    model.add_argument('--freeze', metavar='LAYERS', help='Freezes a models leading feature layers. '
        'Positive int freezes the first N layers/features/blocks. A negative int like "-1" freezes all but the last feature/layer/block. '
        'A positive float like "0.8" freezes the leading 80%% of features/layers/blocks. fc or final classifier layers are never frozen.')
    model.add_argument('--loss-function', default='CrossEntropyLoss', choices=('CrossEntropyLoss','FocalLoss'), help='Loss Function. ')
    model.add_argument('--loss-weights', default=False, help='If "normalize", rare class instances will be boosted. Else a filepath to a perclass list of loss weights. Default is None')
    model.add_argument('--loss-weights-tensor', help=argparse.SUPPRESS)
    model.add_argument('--loss-smoothing', nargs='?', default=0.0, const=0.1, type=float, help='Label Smoothing Regularization arg. Range is 0-1. Default is 0. Const is 0.1')
    model.add_argument('--loss-gamma', default=1.0, type=float, help='For FocalLoss, rate at which easy examples are down-weighted')
    #model.add_argument('--ensemble', metavar='MODE', choices=..., help='Model Ensembling')
    model.add_argument('--optimizer', default='Adam', choices=('Adam','AdamW','SGD'), help='Optimizer. Eg: "AdamW". Default is "Adam"')
    model.add_argument('--lr', default=0.001, type=float, help='Initial Learning Rate. Default is 0.001')
    #model.add_argument('--precision', default='32')
    model.add_argument('--swa', metavar='START', type=int, help='swa_epoch_start') #TODO const='best_epoch' nargs='?' behavior: reloads best_epoch after early stopping and starts there
    model.add_argument('--swa-lr', type=float, default=0.02)
    model.add_argument('--swa-annealing', type=int, default=10)

    epochs = parser.add_argument_group(title='Epoch Parameters')
    epochs.add_argument('--epoch-max', metavar='MAX', default=100, type=int, help='Maximum number of training epochs. Default is 100')
    epochs.add_argument('--epoch-min', metavar='MIN', default=10, type=int, help='Minimum number of training epochs. Default is 10')
    epochs.add_argument('--epoch-stop', metavar='STOP', default=10, type=int, help='Early Stopping: Number of epochs following a best-epoch after-which to stop training. Set STOP=0 to disable. Default is 10')

    # UTILITIES #
    parser.add_argument('--checkpoints-path', default='./experiments')
    parser.add_argument('--autobatch', nargs='?', default=False, const='power', choices=['power','binsearch'], help='Auto-Tunes batch_size prior to training/inference.')
    parser.add_argument('--autobatch-max', type=int, help='Disallow autobatch for setting ')
    parser.add_argument('--workers', dest='num_workers', metavar='N', type=int, help='Total number of dataloader worker threads. If set, overrides --workers-per-gpu')
    parser.add_argument('--workers_per_gpu', metavar='N', default=4, type=int, help='Number of data-loading threads per GPU. 4 per GPU is typical. Default is 4')
    parser.add_argument('--fast-dev-run', default=False, action='store_true')
    parser.add_argument('--env', metavar='FILE', nargs='?', const=True, help='Environment Variables file. If set but not specified, attempts to find a parent .env file')
    parser.add_argument('--gpus', nargs='+', type=int, help=argparse.SUPPRESS) # CUDA_VISIBLE_DEVICES
    parser.add_argument('--version', help=argparse.SUPPRESS)
    parser.add_argument('--onnx', help=argparse.SUPPRESS)

    return parser


def argparse_runtime_args(args):
    # Record GPUs
    if not args.gpus:
        args.gpus = [int(gpu) for gpu in os.environ.get('CUDA_VISIBLE_DEVICES','UNSET').split(',') if gpu not in ['','UNSET']]

    if args.env:
        load_dotenv(override=True) if args.env is True else load_dotenv(args.env, override=True)
    os.environ['CUDA_VISIBLE_DEVICES'] = ','.join(map(str,args.gpus))  # reset if not included in .env

    if not args.num_workers:
        args.num_workers = len(args.gpus)*args.workers_per_gpu

    # Record Version
    try:
        with open('VERSION') as f:
            args.version = f.read().strip()
    except FileNotFoundError:
        args.version = None

    if not args.run:
        args.run = coolname.generate_slug(2)
        print(f'RUN: {args.run}')

    # Set Seed. If args.seed is 0 ie None, a random seed value is used and stored
    if args.seed is None:
        args.seed = random.randint(0,2**32-1)
    args.seed = pl.seed_everything(args.seed)

    # format Freeze to int or float
    if args.freeze:
        args.freeze = float(args.freeze) if '.' in args.freeze else int(args.freeze)

    if args.weights.lower() == 'none':
        args.weights = None

    if args.artifacts_location and os.path.isdir(args.artifacts_location):
        args.artifacts_location = f'file://{os.path.abspath(args.artifacts_location)}'
    if 'AIM_ARTIFACTS_URI' in os.environ and os.environ['AIM_ARTIFACTS_URI']:
        if os.path.isdir(os.environ['AIM_ARTIFACTS_URI']):
            os.environ['AIM_ARTIFACTS_URI'] = f'file://{os.path.abspath(os.environ["AIM_ARTIFACTS_URI"])}'

def parse_training_transforms(args):
    training_transforms = []
    if args.flip:
        flip_tforms = []
        if 'x' in args.flip:
            flip_tforms.append(v2.RandomVerticalFlip(p=0.5))
        if 'y' in args.flip:
            flip_tforms.append(v2.RandomHorizontalFlip(p=0.5))
        training_transforms.extend(flip_tforms)
    return training_transforms


def setup_model_and_datamodule(args):
    # Training Augmentation Setup
    training_transforms = parse_training_transforms(args)

    # Model and Datamodule
    args.model = check_model_name(args.model)
    model_base_transforms = get_model_base_transforms(args.model)
    datamodule = ImageListsWithLabelIndex(args.trainlist, args.vallist, args.classlist,
        base_transforms=model_base_transforms, training_transforms=training_transforms,
        batch_size=args.batch_size, num_workers=args.num_workers)
    args.num_classes = len(datamodule.classes)

    if 'loss_weights' in args:
        if args.loss_weights == 'normalize':
            datamodule.setup('fit')
            class_counts = torch.bincount(torch.IntTensor(datamodule.training_dataset.targets + datamodule.validation_dataset.targets))
            class_weights = 1.0 / class_counts.float()
            args.loss_weights_tensor = class_weights / class_weights.sum()
        elif os.path.isfile(args.loss_weights):
            with open(args.loss_weight) as f:
                args.loss_weights_tensor = torch.Tensor([float(line) for line in f.read().splitlines()])

    loss_kwargs = {}
    if args.loss_function == 'CrossEntropyLoss':
         loss_kwargs['label_smoothing'] = args.loss_smoothing
         if args.loss_weights_tensor:
             loss_kwargs['weights'] = args.loss_weights_tensor
    elif args.loss_function == 'FocalLoss':
        loss_kwargs['gamma'] = args.loss_gamma
        if any(args.loss_weights_tensor):
            loss_kwargs['alpha'] = args.loss_weights_tensor

    optimizer_kwargs = dict(lr = args.lr)

    lightning_module = MulticlassClassifier(
                model_name = args.model,
                num_classes = args.num_classes,
                model_weights = args.weights,
                model_freeze = args.freeze,
                loss_function = args.loss_function,
                loss_kwargs = loss_kwargs,
                optimizer = args.optimizer,
                optimizer_kwargs = optimizer_kwargs,
    )
    return lightning_module, datamodule


def setup_aimlogger(args, context_postfixes:dict=None, context_prefixes:dict=None):
    args_logger, env_logger = None,None
    logger_kwargs = dict(experiment=args.experiment, run_name=args.run)
    logger_kwargs['context_postfixes'] = context_postfixes
    logger_kwargs['context_prefixes'] = context_prefixes

    if 'AIM_ARTIFACTS_S3_ENDPOINT' in os.environ and os.environ['AIM_ARTIFACTS_S3_ENDPOINT']:
        S3ArtifactStorage_clientconfig(endpoint_url=os.environ['AIM_ARTIFACTS_S3_ENDPOINT'],
                                 aws_access_key_id=os.environ['AIM_ARTIFACTS_S3_ACCESSKEY'],
                                 aws_secret_access_key=os.environ['AIM_ARTIFACTS_S3_SECRETKEY'])

    if 'AIM_REPO' in os.environ and os.environ['AIM_REPO']:
        env_logger = AimLogger(repo=os.environ['AIM_REPO'], **logger_kwargs)
        if 'AIM_ARTIFACTS_URI' in os.environ and os.environ['AIM_ARTIFACTS_URI']:
            env_logger.experiment.set_artifacts_uri(os.environ['AIM_ARTIFACTS_URI'])
        if args.note: env_logger.experiment.props.description = args.note

    if args.repo:
        args_logger = AimLogger(repo=args.repo, **logger_kwargs)
        if args.artifacts_location:
            args_logger.experiment.set_artifacts_uri(args.artifacts_location)
        if args.note: args_logger.experiment.props.description = args.note

    if args_logger and env_logger:
        loggers = [args_logger, env_logger]
    else:
        loggers = args_logger or env_logger

    if loggers is None:
        warnings.warn('NO AIM LOGGERS CREATED')
    elif isinstance(loggers,AimLogger):
        return loggers
    else:
        return loggers


def main(args):
    torch.set_float32_matmul_precision('medium')

    ## Setup Model & Data Module ##
    model, datamodule = setup_model_and_datamodule(args)

    ## Setup Epoch Logger ##
    contexts = dict(averaging={'macro': '_macro', 'micro': '_micro', 'weighted': '_weighted',
                               'none': '_perclass'},  # f1, precision, recall
                    normalized={'no': '_summed', 'yes': '_normalized'},  # confusion matrix, loss?
                    rebalanced={'yes': '_rebalanced', 'no': '_unbalanced'})  # lossfunction loss_rebalanced option?
    # val_/train_ already handled by default
    logger = setup_aimlogger(args, context_postfixes=contexts)
    assert logger is not None, 'Aim logger is None. Did you forget to set --repo, --env, or AIM_REPO env variable?'

    ## Setup Callbacks ##
    callbacks=[]

    validation_results_callbacks = [
        LogNormalizedLoss(),
    ]
    callbacks.extend(validation_results_callbacks)


    plotting_callbacks = [
        #BarPlotMetricAim('f1_perclass', order_reverse=True),
        BarPlotMetricAim('f1_perclass', order_by='f1_perclass'),
        #BarPlotMetricAim('f1_perclass', title='{METRIC} by {ORDER} (ep{EPOCH})', order_by='class-counts'),

        #BarPlotMetricAim('recall_perclass', order_reverse=True),
        #BarPlotMetricAim('recall_perclass', order_by='recall_perclass'),
        #BarPlotMetricAim('recall_perclass', title='{METRIC} by {ORDER} (ep{EPOCH})', order_by='class-counts'),

        #BarPlotMetricAim('precision_perclass', order_reverse=True),
        #BarPlotMetricAim('precision_perclass', order_by='precision_perclass'),
        #BarPlotMetricAim('precision_perclass', title='{METRIC} by {ORDER} (ep{EPOCH})', order_by='class-counts'),

        #PlotConfusionMetricAim(order_by='classes'),
        PlotConfusionMetricAim(order_by='classes', normalize=True),
        PlotConfusionMetricAim(order_by='f1_perclass', normalize=True),
        #PlotConfusionMetricAim(order_by='recall_perclass', normalize=True),

        PlotPerclassDropdownAim(),
    ]
    callbacks.extend(plotting_callbacks)

    if args.epoch_stop:  # Early Stopping
        callbacks.append( EarlyStopping('val_loss', mode='min', patience=args.epoch_stop) )

    if args.freeze:  # custom show-grad model summary callback, overwrites default
        callbacks.append( ModelSummaryWithGradCallback(max_depth=2) )

    # Checkpointing
    # https://lightning.ai/docs/pytorch/stable/api/lightning.pytorch.callbacks.ModelCheckpoint.html
    # https://lightning.ai/docs/pytorch/stable/common/checkpointing_advanced.html
    #hashid = logger.experiment.hash if isinstance(logger,AimLogger) else logger[0].experiment.hash
    chkpt_path = os.path.join(args.checkpoints_path, args.experiment, args.run)
    ckpt_callback = ModelCheckpoint(
        dirpath=chkpt_path, filename='loss-{val_normloss:3.3f}_ep-{epoch:03.0f}',
        monitor='val_loss', mode='min', save_last='link', save_top_k=3,
        auto_insert_metric_name=False)
    callbacks.append(ckpt_callback)

    if args.swa:
        callbacks.append(StochasticWeightAveraging(args.swa_lr, swa_epoch_start=args.swa, annealing_epochs=args.swa_annealing))

    ## Setup Trainer  ##
    trainer = pl.Trainer(num_sanity_val_steps=0,
                         deterministic=True,
                         accelerator='auto', devices='auto', num_nodes=1,
                         max_epochs=args.epoch_max, min_epochs=args.epoch_min,
                         precision='32',
                         logger=logger,
                         log_every_n_steps=-1,
                         callbacks=callbacks,
                         fast_dev_run=args.fast_dev_run,
                         default_root_dir='/tmp/classifier',
                        )

    # auto-tune batch-size
    if args.autobatch:
        tuner = Tuner(trainer)
        found_batch_size = tuner.scale_batch_size(model, datamodule=datamodule,
            mode=args.autobatch, method='fit', max_trials=10, init_val=args.batch_size)
        args.batch_size_init, args.batch_size = args.batch_size, min([found_batch_size, args.autobatch_max or float('inf')])
        model.save_hyperparameters(just_hparams(args))

    # save training artifacts
    if trainer.logger.experiment.artifacts_uri:
        if os.path.isfile(args.classlist):
            trainer.logger.experiment.log_artifact(args.classlist, name=os.path.basename(args.classlist))
        if os.path.isfile(args.vallist):
            trainer.logger.experiment.log_artifact(args.trainlist, name=os.path.basename(args.vallist))
        if os.path.isfile(args.trainlist):
            trainer.logger.experiment.log_artifact(args.trainlist, name=os.path.basename(args.trainlist))

    # Do Training
    trainer.fit(model, datamodule=datamodule)

    # Do Testing
    if args.testlist:
        trainer.test(model, datamodule=datamodule)

    # Copy best model
    # TODO do this as a callback
    if trainer.logger.experiment.artifacts_uri:
        model_path = trainer.checkpoint_callback.best_model_path
        trainer.logger.experiment.log_artifact(model_path, name=os.path.basename(model_path))

    print('DONE!')


def args_subsetter_factory(parser: argparse.ArgumentParser):
    def args_subset(args, arg_names, group_titles, exclude=[]):
        arguments = []
        if arg_names is not None:
            arguments = arg_names.copy()
        groups = [group for group in parser._action_groups if group.title in group_titles]
        group_args = [action.dest for group in groups for action in group._group_actions]
        arguments.extend(group_args)

        subset = argparse.Namespace(**{k:v for k,v in vars(args).items() if k in arguments and k not in exclude})
        return subset
    return args_subset


if __name__ == '__main__':
    parser = argparse_init()
    args_subsetter = args_subsetter_factory(parser)
    def just_hparams(args_namespace: argparse.Namespace):
        subparsers = ['Model Parameters', 'Epoch Parameters']
        return args_subsetter(args_namespace, [], subparsers)
    args = parser.parse_args()
    argparse_runtime_args(args)
    main(args)