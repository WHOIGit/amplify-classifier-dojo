import os
import argparse
import random
import warnings

from tqdm import tqdm
import coolname
from dotenv import load_dotenv
from torchvision.models import InceptionOutputs, GoogLeNetOutputs

from torchvision.transforms import v2
import torch
import lightning as L
import lightning.pytorch as pl
from lightning.pytorch.tuner import Tuner
from lightning.pytorch.callbacks import EarlyStopping, ModelCheckpoint, StochasticWeightAveraging
import torchmetrics as tm

from aim.pytorch_lightning import AimLogger
from aim.storage.artifacts.s3_storage import S3ArtifactStorage_clientconfig

# if file is called directly, must set import paths to project root
if __name__ == '__main__':
    import sys, pathlib

    PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent.absolute()
    if sys.path[0] != str(PROJECT_ROOT): sys.path.insert(0, str(PROJECT_ROOT))

from src.train import setup_aimlogger
from src.multiclass.callbacks import BarPlotMetricAim, PlotConfusionMetricAim, PlotPerclassDropdownAim
from src.multiclass.datasets import ImageListsWithLabelIndex, parse_listfile
from src.multiclass.models import MulticlassClassifier, get_model_base_transforms, check_model_name, get_model_resize


def argparse_init(parser=None):
    if parser is None:
        parser = argparse.ArgumentParser(description='Train an image classifier!')

    # DATASET #
    dataset = parser.add_argument_group(title='Dataset', description=None)
    dataset.add_argument('--classlist', required=True,
                         help='A text file, each line is a class label (the label order is significant)')
    dataset.add_argument('--vallist', required=True,
                         help='A text file, one sample per line, each sample has a class-index and image path')
    dataset.add_argument('--testlist', help='Like trainlist, but for final test metrics. Optional')

    # TRACKING #
    aimstack = parser.add_argument_group(title='AimLogger', description=None)
    aimstack.add_argument('--run', help='The name of this run. A run name is automatically generated by default')
    aimstack.add_argument('--experiment', help='The broader category/grouping this RUN belongs to')
    aimstack.add_argument('--note',
                          help='Add any kind of note or description to the trained model. Make sure to use quotes "around your message."')
    aimstack.add_argument('--repo', help='Aim repo path. Also see: Aim environment variables.')
    aimstack.add_argument('--artifacts-location', help='Aim Artifacts location. Also see: Aim environment variables.')

    # HYPER PARAMETERS #
    model = parser.add_argument_group(title='Model Parameters')
    model.add_argument('--models', help='Listfile of ckpt paths or aim run hashes', required=True)
    model.add_argument('--seed', type=int, help='Set a specific seed for deterministic output')

    # UTILITIES #
    parser.add_argument('--checkpoints-path', default='./experiments')
    parser.add_argument('--workers', dest='num_workers', metavar='N', type=int,
                        help='Total number of dataloader worker threads. If set, overrides --workers-per-gpu')
    parser.add_argument('--workers_per_gpu', metavar='N', default=4, type=int,
                        help='Number of data-loading threads per GPU. 4 per GPU is typical. Default is 4')
    parser.add_argument('--fast-dev-run', default=False, action='store_true')
    parser.add_argument('--env', metavar='FILE', nargs='?', const=True,
                        help='Environment Variables file. If set but not specified, attempts to find a parent .env file')
    parser.add_argument('--gpus', nargs='+', type=int, help=argparse.SUPPRESS)  # CUDA_VISIBLE_DEVICES

    return parser


def argparse_runtime_args(args):
    # Record GPUs
    if not args.gpus:
        args.gpus = [int(gpu) for gpu in os.environ.get('CUDA_VISIBLE_DEVICES', 'UNSET').split(',') if gpu not in ['', 'UNSET']]

    if args.env:
        load_dotenv(override=True) if args.env is True else load_dotenv(args.env, override=True)
    os.environ['CUDA_VISIBLE_DEVICES'] = ','.join(map(str, args.gpus))  # reset if not included in .env

    if not args.num_workers:
        args.num_workers = len(args.gpus) * args.workers_per_gpu

    if not args.run:
        args.run = coolname.generate_slug(2)
        print(f'RUN: {args.run}')

    # Set Seed. If args.seed is 0 ie None, a random seed value is used and stored
    if args.seed is None:
        args.seed = random.randint(0, 2 ** 32 - 1)
    args.seed = pl.seed_everything(args.seed)

    if args.artifacts_location and os.path.isdir(args.artifacts_location):
        args.artifacts_location = f'file://{os.path.abspath(args.artifacts_location)}'
    if 'AIM_ARTIFACTS_URI' in os.environ and os.environ['AIM_ARTIFACTS_URI']:
        if os.path.isdir(os.environ['AIM_ARTIFACTS_URI']):
            os.environ['AIM_ARTIFACTS_URI'] = f'file://{os.path.abspath(os.environ["AIM_ARTIFACTS_URI"])}'

    with open(args.models) as f:
        args.models = f.read().splitlines()

def load_model(model_designator):
    # get checkpoint file
    print('LOAD MODEL:', model_designator)
    if os.path.isfile(model_designator) and model_designator.endswith('.ckpt'):
        ckpt_path = model_designator
    else:
        ...  # download model from Aim or S3 to local
        ckpt_path = ...

    # load checkpoint file
    try:
        checkpoint_module = MulticlassClassifier.load_from_checkpoint(ckpt_path, map_location='cpu')
    except Exception as e:
        #print(type(e), e)
        checkpoint = torch.load(ckpt_path, map_location='cpu')
        if 'model_name' not in checkpoint['hyper_parameters']:
            checkpoint['hyper_parameters']['model_name'] = checkpoint['hyper_parameters'].pop('model')
            checkpoint['hyper_parameters']['model_weights'] = checkpoint['hyper_parameters'].pop('weights')
            for key in list(checkpoint['hyper_parameters'].keys()):
                if key not in 'model_name num_classes model_weights model_freeze loss_function loss_kwargs optimizer optimizer_kwargs'.split():
                    checkpoint['hyper_parameters'].pop(key)
        checkpoint_module = MulticlassClassifier(**checkpoint['hyper_parameters'])
        checkpoint_module.eval()
        checkpoint_module.load_state_dict(checkpoint['state_dict'])
    model = checkpoint_module.model

    # determine base-transforms
    model_name = checkpoint_module.hparams['model_name']
    resize = get_model_resize(model_name)
    tf_key = ((resize, resize), torch.float32)
    transforms = [v2.Resize(tf_key[0]), v2.ToImage(), v2.ToDtype(tf_key[1], scale=True)]
    #transform = v2.Compose(transforms)
    return model, transforms


def main(args):
    torch.set_float32_matmul_precision('medium')

    ## Setup Epoch Logger ##
    contexts = dict(averaging={'macro': '_macro', 'micro': '_micro', 'weighted': '_weighted',
                               'none': '_perclass'},  # f1, precision, recall
                    normalized={'no': '_summed', 'yes': '_normalized'},  # confusion matrix, loss?
                    )
    # val_/train_ already handled by default
    logger = setup_aimlogger(args, context_postfixes=contexts)
    assert logger is not None, 'Aim logger is None. Did you forget to set --repo, --env, or AIM_REPO env variable?'

    scores_per_model = []
    labels_per_model = []
    datamodule = ImageListsWithLabelIndex(train_src=None,
        val_src=args.vallist, classlist=args.classlist,
        base_transforms=[],
        batch_size=100, num_workers=args.num_workers)
    for model_designator in tqdm(args.models):
        model, transforms = load_model(model_designator)
        datamodule.base_transforms = transforms
        datamodule.setup('validation', force=True)
        model_preds = []
        true_labels = []
        for batch in datamodule.val_dataloader():
            samples, labels = batch[0], batch[1]
            outputs = model(samples)
            if isinstance(outputs, (InceptionOutputs, GoogLeNetOutputs)):
                outputs = outputs.logits
            preds = torch.nn.functional.softmax(outputs, dim=1)
            model_preds.append(preds.detach().cpu())
            true_labels.append(labels.detach().cpu())
        model_preds = torch.concat(model_preds)
        true_labels = torch.concat(true_labels)
        scores_per_model.append(model_preds)
        labels_per_model.append(true_labels)
    scores_per_model = torch.stack(scores_per_model)
    assert torch.equal(labels_per_model[0],labels_per_model[-1]), 'labels_are_different'
    true_labels = labels_per_model[0]
    classes = datamodule.validation_dataset.classes
    perclass_count = datamodule.validation_dataset.count_perclass.values()
    print('TRUE_LABELS.SHAPE:', true_labels.shape)
    print('SCORES_PER_MODEL.SHAPE:', scores_per_model.shape, '(MODELS,SAMPLES,CLASSES)')

    # SOFTVOTING
    averaged_scores = torch.mean(scores_per_model, dim=0, keepdim=True)
    print('AVERAGED_SCORES.SHAPE:', averaged_scores.shape)
    softvoting_classes = torch.argmax(averaged_scores, dim=2, keepdim=True)
    softvoting_classes = torch.squeeze(softvoting_classes)
    #print('SOFTVOTING:', softvoting_classes)
    #print()

    # HARDVOTING
    argmaxed_scores = torch.argmax(scores_per_model, dim=2, keepdim=True)
    print('ARGMAXED_SCORES.SHAPE:', argmaxed_scores.shape)
    hardvoting_classes,_ = torch.mode(argmaxed_scores, dim=0, keepdim=True)
    hardvoting_classes = torch.squeeze(hardvoting_classes)
    #print('HARDVOTING:', hardvoting_classes)

    # METRICS
    metrics = torch.nn.ModuleDict()
    num_classes = len(classes)
    for mode in ['weighted','micro','macro',None]:
        for stat,MetricClass in zip(['f1','recall','accuracy','precision'],
                                    [tm.F1Score,tm.Recall,tm.Accuracy,tm.Precision]):
            key_hard = f'{stat}_{mode or "perclass"}_HARD'
            key_soft = f'{stat}_{mode or "perclass"}_SOFT'
            metrics[key_hard] = MetricClass(task='multiclass', num_classes=num_classes, average=mode)
            metrics[key_soft] = MetricClass(task='multiclass', num_classes=num_classes, average=mode)
            #hard = metrics[key_hard].update(hardvoting_classes, true_labels).cpu()
            #soft = metrics[key_soft].update(softvoting_classes, true_labels).cpu()
            metrics[key_soft].update(torch.squeeze(averaged_scores), true_labels)
            soft = metrics[key_soft].compute().cpu()
            if mode:
                #logger.experiment.track(hard, name=stat, epoch=0, context=dict(subset='val', averaging=mode, voting_ensemble='hard'))
                logger.experiment.track(soft, name=stat, epoch=0, context=dict(subset='val', averaging=mode, voting_ensemble='soft'))
            else:
                if stat=='f1':
                    #f1perclass_fig_hard = BarPlotMetricAim.plot(hard, classes, perclass_count, title='F1 Perclass (HARD)')
                    f1perclass_fig_soft = BarPlotMetricAim.plot(soft, classes, perclass_count, title='F1 Perclass (SOFT)', xaxis_title=stat)
                    #BarPlotMetricAim.fig_log2aim(f1perclass_fig_hard, 'f1_perclass', [logger],
                    #    context=dict(figure_order='classes', subset='val', voting_ensemble='hard'))
                    BarPlotMetricAim.fig_log2aim(f1perclass_fig_soft, 'f1_perclass', [logger],
                        context=dict(figure_order='classes', subset='val', voting_ensemble='soft'))

    #metrics['confusion_matrix_HARD'] = tm.ConfusionMatrix(task='multiclass', num_classes=num_classes).cpu()
    metrics['confusion_matrix_SOFT'] = tm.ConfusionMatrix(task='multiclass', num_classes=num_classes).cpu()
    #matrix_counts_hard = metrics['confusion_matrix_HARD'].update(hardvoting_classes, true_labels)
    metrics['confusion_matrix_SOFT'].update(softvoting_classes, true_labels)
    matrix_counts_soft = metrics['confusion_matrix_SOFT'].compute().cpu()
    #matrix_fig_hard = PlotConfusionMetricAim.plot(matrix_counts_hard, classes, perclass_count, normalize=True,
    #                                     title='Normalized Ensemble Confusion Matrix (HARD)', metrics=metrics)
    matrix_fig_soft = PlotConfusionMetricAim.plot(matrix_counts_soft, classes, perclass_count, normalize=True,
                                         title='Normalized Ensemble Confusion Matrix (HARD)', metrics=metrics)
    #PlotConfusionMetricAim.fig_log2aim(matrix_fig_hard, 'confusion_matrix', [logger],
    #    context=dict(figure_order='classes', subset='val', voting_ensemble='hard', normalize='true'))
    PlotConfusionMetricAim.fig_log2aim(matrix_fig_soft, 'confusion_matrix', [logger],
        context=dict(figure_order='classes', subset='val', voting_ensemble='soft', normalize='true'))

    print('DONE!')


if __name__ == '__main__':
    parser = argparse_init()
    args = parser.parse_args()
    argparse_runtime_args(args)
    main(args)