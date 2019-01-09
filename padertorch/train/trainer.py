import contextlib
import itertools
import operator
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import padertorch as pts
import torch
from padertorch.configurable import Configurable
from paderbox.utils.nested import flatten, nested_op
from padertorch.train.optimizer import Optimizer, Adam
from padertorch.train.run_time_tests import test_run
from tensorboardX import SummaryWriter

__all__ = [
    'Trainer',
]


class ContextTimerDict:
    """
    To be able to keep the measurements, we need to create the object before.
    Then each measurement can be started with a context manager.

    >>> np.set_printoptions(precision=2)
    >>> timer = ContextTimerDict()
    >>> with timer['test']:
    ...     time.sleep(0.1)
    >>> with timer['test']:
    ...     time.sleep(0.1)
    >>> with timer['test_2']:
    ...     time.sleep(0.1)

    Ignore timing, when an exception is raised
    >>> with contextlib.suppress(Exception), timer['test_2']:
    ...     raise Exception
    >>> timer
    ContextTimerDict: {'test': array([0.1, 0.1]), 'test_2': array([0.1])}
    >>> timer.as_dict
    {'test': array([0.1, 0.1]), 'test_2': array([0.1])}

    """
    def __init__(self):
        self.timings = defaultdict(list)
        self.timestamp = time.perf_counter  # time.process_time

    @contextlib.contextmanager
    def __getitem__(self, item):
        assert isinstance(item, str), item
        start = self.timestamp()
        yield
        end = self.timestamp()
        self.timings[item].append(end - start)

    @property
    def as_dict(self):
        return {k: np.array(time) for k, time in self.timings.items()}

    def __repr__(self):
        return f'{self.__class__.__name__}: ' + repr(self.as_dict)

    def __str__(self):
        return str(self.as_dict)


class Trainer(Configurable):

    @classmethod
    def get_signature(cls):
        default_dict = super().get_signature()
        default_dict['optimizer'] = {'cls': Adam}
        return default_dict

    def __init__(
            self,
            model,
            storage_dir,
            optimizer=None,
            loss_weights=None,
            summary_step=(1, 'epoch'),
            checkpoint_step=(1, 'epoch'),
            validation_step=(1, 'epoch'),
            gpu=0 if torch.cuda.is_available() else None,
            max_epochs=None,
            max_iterations=None,
            init_checkpoint=None,
            seed=0,
    ):
        self.model = model
        self.use_cuda = gpu is not None
        self.gpu_device = None
        if self.use_cuda:
            self.gpu_device = int(gpu)
            self.model = nested_op(
                lambda m: m.cuda(self.gpu_device), self.model
            )
        else:
            self.gpu_device = None
        self.optimizer = optimizer

        nested_op(
            lambda model, opti: opti.set_parameters(model.parameters())
            if opti is not None else None,
            self.model, self.optimizer
        )

        self.storage_dir = Path(storage_dir).expanduser().absolute()
        self.reset_summary()
        self.iteration = 0
        self.epoch = 0
        self.writer = SummaryWriter(str(self.storage_dir))
        if init_checkpoint is not None:
            self.load_checkpoint(
                Path(init_checkpoint).expanduser().absolute(),
            )
        self.seed = seed
        assert operator.xor(
            max_iterations is None,
            max_epochs is None,
        ), (max_iterations, max_epochs)
        if max_iterations is not None:
            self.max_iterations = EndTrigger(max_iterations, unit='iteration')
        elif max_epochs is not None:
            self.max_iterations = EndTrigger(max_epochs, unit='epoch')
        else:
            raise Exception(max_epochs, max_iterations)

        self.summary_trigger = IntervalTrigger.new(summary_step)
        self.checkpoint_trigger = IntervalTrigger.new(checkpoint_step)
        self.validation_trigger = IntervalTrigger.new(validation_step)

        self.loss_weights = loss_weights

    def reset_summary(self):
        # Todo: add figures
        self.summary = dict(
            losses=defaultdict(list),
            scalars=defaultdict(list),
            histograms=defaultdict(list),
            audios=dict(),
            images=dict()
        )
        self.timer = ContextTimerDict()

    def test_run(self, train_iterator, validation_iterator):
        """
        Run a test on the trainer instance (i.e. model test).

        Tests:
         - forward (train and validate)
         - deterministic output in eval
         - simple review dict test

        """
        test_run(
            self,
            train_iterator,
            validation_iterator,
        )

    def train(self, train_iterator, validation_iterator):
        os.makedirs(str(self.storage_dir / 'checkpoints'), exist_ok=True)

        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.benchmark = False

        torch.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)

        # Change model to train mode (e.g. activate dropout)
        nested_op(lambda m: m.train(), self.model)

        # For training continue set the correct last value
        self.summary_trigger.set_last(self.iteration, self.epoch)
        self.checkpoint_trigger.set_last(self.iteration, self.epoch)
        self.validation_trigger.set_last(self.iteration, self.epoch)

        # ================ MAIN TRAINING LOOP! ===================
        try:
            for self.epoch in itertools.count(self.epoch):  # infinite loop
                data_iterator = iter(train_iterator)
                for self.iteration in itertools.count(self.iteration):
                    # Because of last validation, validation must be before
                    # "max_iterations".
                    if self.validation_trigger(
                            iteration=self.iteration, epoch=self.epoch
                    ):
                        # Todo: allow continuous evaluation
                        self.add_summary('training')
                        self.validate(validation_iterator)
                        nested_op(lambda m: m.train(), self.model)
                    if self.max_iterations(
                            iteration=self.iteration, epoch=self.epoch
                    ):
                        return
                    if self.summary_trigger(
                            iteration=self.iteration, epoch=self.epoch
                    ) or self.iteration == 1:
                        self.add_summary('training')
                    if self.checkpoint_trigger(
                            iteration=self.iteration, epoch=self.epoch
                    ) or self.iteration == 1:
                        self.save_checkpoint()

                    with self.timer['time_per_step']:
                        try:
                            with self.timer['time_per_data_loading']:
                                batch = next(data_iterator)
                        except StopIteration:
                            if self.iteration > 0:
                                break
                            else:
                                raise Exception('Zero length train iterator')

                        batch = pts.data.batch_to_device(
                            batch, self.use_cuda, self.gpu_device
                        )
                        # Todo: backup OOM
                        with self.timer['time_per_train_step']:
                            self.train_step(batch)

        finally:
            self.add_summary('training')
            self.save_checkpoint()

    def validate(self, validation_iterator):
        print('Starting Validation')

        train_end_time = self.timer.timestamp()

        if hasattr(self, '_train_start_time'):
            self.timer.timings['non_validation_time'].append(
                train_end_time - self._train_start_time
            )

        with self.timer['validation_time'], torch.no_grad():
            # Change model to eval mode (e.g. deactivate dropout)
            nested_op(lambda m: m.eval(), self.model)
            for i, batch in enumerate(validation_iterator):
                batch = pts.data.batch_to_device(
                    batch, self.use_cuda, self.gpu_device
                )
                self.validation_step(batch)

        self._train_start_time = self.timer.timestamp()

        self.add_summary('validation')
        print('Finished Validation')

    def train_step(self, batch):
        msg = 'Overwrite the train_step and validation_step, ' \
              'when you have multiple models.'
        assert isinstance(self.model, torch.nn.Module), (self.model, msg)
        assert isinstance(self.optimizer, Optimizer), (self.optimizer, msg)

        self.optimizer.zero_grad()
        model_out = self.model(batch)
        review = self.model.review(batch, model_out)
        self.backward(review)
        self.clip_grad()
        self.optimizer.step()
        self.update_summary(review)

    def validation_step(self, batch):
        assert isinstance(self.model, torch.nn.Module), (
            self.model, 'Overwrite the train_step and validation_step, when you have multiple models.'
        )
        model_out = self.model(batch)
        review = self.model.review(batch, model_out)
        self.update_summary(review)

    def backward(self, review, retain_graph=False):
        loss = 0.
        loss_weights = self.loss_weights
        if loss_weights is None and len(review['losses']) != 1:
            raise Exception(
                'You can not have multiple losses without specifying '
                f'loss_weights. losses: {review["losses"]}'
            )
        for key, value in review['losses'].items():
            weight = loss_weights[key] if loss_weights is not None else 1.
            loss += weight * value
        loss.backward(retain_graph=retain_graph)

    def clip_grad(self, prefix: str = None):
        # Todo: report clipped and unclipped
        # Todo: allow clip=None but still report grad_norm
        if prefix is None:
            prefix_ = ''
        else:
            prefix_ = f'{prefix}_'
        grad_norm = nested_op(
            lambda model, opti: opti.clip_grad(model.parameters(), prefix)
            if opti is not None else 0.,
            self.model, self.optimizer
        )
        if isinstance(grad_norm, dict):
            for key, value in flatten(grad_norm).items():
                self.summary['scalars'][f'{prefix_}grad_norm_{key}'].append(
                    value)
                # underscore was necessary to obtain unique keys to prevent
                # tensorboard error
                self.summary['histograms'][
                    f'{prefix_}grad_norm_{key}_'].append(value)
        if isinstance(grad_norm, (list, tuple)):
            for i, value in enumerate(grad_norm):
                self.summary['scalars'][f'{prefix_}grad_norm_{i}'].append(
                    value)
                self.summary['histograms'][f'{prefix_}grad_norm_{i}_'].append(
                    value)
        else:
            self.summary['scalars'][f'{prefix_}grad_norm'].append(grad_norm)
            self.summary['histograms'][f'{prefix_}grad_norm_'].append(
                grad_norm)

    def update_summary(self, review):
        for key, loss in review.get('losses', dict()).items():
            self.summary['losses'][key].append(loss.item())
        for key, scalar in review.get('scalars', dict()).items():
            self.summary['scalars'][key].append(
                scalar.item() if torch.is_tensor(scalar) else scalar)
        for key, histogram in review.get('histograms', dict()).items():
            self.summary['histograms'][key] = np.concatenate(
                [self.summary['histograms'].get(key, np.zeros(0)),
                 histogram.clone().cpu().data.numpy().flatten()]
            )[-10000:]  # do not hold more than 10K values in memory
        for key, audio in review.get('audios', dict()).items():
            self.summary['audios'][key] = audio  # snapshot
        for key, image in review.get('images', dict()).items():
            self.summary['images'][key] = image  # snapshot

    def add_summary(self, prefix):
        for key, loss in self.summary['losses'].items():
            self.writer.add_scalar(
                f'{prefix}/{key}', np.mean(loss), self.iteration)
        for key, scalar in self.summary['scalars'].items():
            self.writer.add_scalar(
                f'{prefix}/{key}', np.mean(scalar), self.iteration)
        for key, scalar in self.timer.as_dict.items():
            if key in ['time_per_data_loading', 'time_per_train_step']:
                if 'time_per_step' in self.timer.as_dict.keys():
                    time_per_step = self.timer.as_dict['time_per_step']
                    if len(time_per_step) != len(scalar):
                        print(
                            'Warning: padertorch.Trainer timing bug.'
                            f'len(time_per_step) == {len(time_per_step)} '
                            f'!= len(scalar) == {len(scalar)}'
                        )
                    scalar = (
                        scalar.sum() / time_per_step.sum()
                    )
                    if key == 'time_per_data_loading':
                        key = 'time_rel_data_loading'
                    elif key == 'time_per_train_step':
                        key = 'time_rel_train_step'
                else:
                    # Something went wrong, most likely an exception.
                    pass
            self.writer.add_scalar(
                f'{prefix}/{key}', scalar.mean(), self.iteration)
        for key, histogram in self.summary['histograms'].items():
            self.writer.add_histogram(
                f'{prefix}/{key}', np.array(histogram), self.iteration
            )
        for key, audio in self.summary['audios'].items():
            if isinstance(audio, (tuple, list)):
                assert len(audio) == 2, (len(audio), audio)
                self.writer.add_audio(
                    f'{prefix}/{key}', audio[0],
                    self.iteration, sample_rate=audio[1]
                )
            else:
                self.writer.add_audio(
                    f'{prefix}/{key}', audio,
                    self.iteration, sample_rate=16000
                )
        for key, image in self.summary['images'].items():
            self.writer.add_image(f'{prefix}/{key}', image, self.iteration)
        self.reset_summary()

    def save_checkpoint(self):
        checkpoint_path = str(
            self.storage_dir / 'checkpoints' / f'ckpt_{self.iteration}')
        if self.use_cuda:
            self.cpu()
        torch.save(
            dict(
                model=nested_op(lambda m: m.state_dict(), self.model),
                iteration=self.iteration,
                epoch=self.epoch,
                optimizer=nested_op(
                    lambda opti: opti and opti.state_dict(), self.optimizer)
            ),
            checkpoint_path
        )
        if self.use_cuda:
            self.cuda(self.gpu_device)
        print(f"{datetime.now()}: Saved model and optimizer state at iteration "
              f"{self.iteration} to {checkpoint_path}")

    def load_checkpoint(self, checkpoint_path):
        """
        Function should not be modified to accept a folder alone to avoid
        a confusion between best snapshot (for test) and last snapshot
        (resume).

        Args:
            checkpoint_path:

        Returns:

        """
        assert os.path.isfile(checkpoint_path), checkpoint_path
        checkpoint_dict = torch.load(str(checkpoint_path), map_location='cpu')
        nested_op(
            lambda m, d: m.load_state_dict(d),
            self.model, checkpoint_dict['model']
        )
        iteration = checkpoint_dict['iteration']
        self.iteration = iteration + 1
        self.epoch = checkpoint_dict['epoch']
        nested_op(
            lambda opti, d: opti.load_state_dict(d)
            if opti is not None else None,
            self.optimizer, checkpoint_dict['optimizer']
        )
        print(f"Loaded checkpoint '{checkpoint_path}' (iteration {iteration})")

    def cpu(self):
        nested_op(lambda m: m.cpu(), self.model)
        nested_op(
            lambda opti: opti.cpu() if opti is not None else None,
            self.optimizer
        )

    def cuda(self, device):
        nested_op(lambda m: m.cuda(device), self.model)
        nested_op(
            lambda opti: opti.cuda(device) if opti is not None else None,
            self.optimizer
        )
