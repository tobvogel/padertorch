import einops
import torch
from torch.nn.utils.rnn import PackedSequence

import padertorch as pt
from padertorch.ops.mappings import ACTIVATION_FN_MAP
from padertorch.summary import mask_to_image, stft_to_image


class MultiChannelPermutationInvariantTraining(pt.Model):
    """

    """
    def __init__(
            self,
            F=257,
            C=6,
            recurrent_layers=3,
            units=600,
            K=2,
            dropout_input=0.,
            dropout_hidden=0.,
            dropout_linear=0.,
            use_phase_diff=False
    ):
        """

        Args:
            F: Number of frequency bins, fft_size / 2 + 1
            recurrent_layers:
            units: results in `units` forward and `units` backward units
            C: Number of microphone streams
            K: Number of output streams/ speakers
            dropout_input: Dropout forget ratio before first recurrent layer
            dropout_hidden: Vertical forget ratio dropout between each
                recurrent layer
            dropout_linear: Dropout forget ratio before first linear layer
        """
        super().__init__()

        self.K = K
        self.F = F*C
        if use_phase_diff:
            self.F = F*C*2 # Inter Phase differences have same dim as spectrum
        assert dropout_input <= 0.5, dropout_input
        self.dropout_input = torch.nn.Dropout(dropout_input)

        assert dropout_hidden <= 0.5, dropout_hidden
        self.blstm = torch.nn.LSTM(
            1024,
            units,
            recurrent_layers,
            bidirectional=True,
            dropout=dropout_hidden,
        )

        assert dropout_linear <= 0.5, dropout_linear
        self.dropout_linear = torch.nn.Dropout(dropout_linear)
        self.projection_layer = torch.nn.Linear(self.F, 1024)
        self.linear1 = torch.nn.Linear(2 * units, 2 * units)
        self.linear2 = torch.nn.Linear(2 * units, F * K)

    def forward(self, batch):
        """

        Args:
            batch: Dictionary with lists of tensors

        Returns: List of mask tensors

        """

        h = pt.ops.pack_sequence(batch['Y_abs_array'])
        _, F = h.data.size()
        assert F == self.F, f'self.F = {self.F} != F = {F}'

        h_data = self.dropout_input(h.data)

        h_data = pt.ops.sequence.log1p(h_data)
        h = PackedSequence(h_data, h.batch_sizes)

        # Returns tensor with shape (t, b, num_directions * hidden_size)
        h, _ = self.blstm(h)

        h_data = self.dropout_linear(h.data)
        h_data = self.linear1(h_data)
        h_data = pt.clamp(h_data, min=0)
        h_data = self.linear2(h_data)
        h_data = pt.clamp(h_data, min=0)
        h = PackedSequence(h_data, h.batch_sizes)

        mask = PackedSequence(
            einops.rearrange(h.data, 'tb (k f) -> tb k f', k=self.K),
            h.batch_sizes,
        )
        return pt.ops.unpack_sequence(mask)

    def review(self, batch, model_out):
        # TODO: Maybe calculate only one loss? May be much faster.

        pit_mse_loss = list()
        for mask, observation, target in zip(
                model_out,
                batch['Y_abs'],
                batch['X_abs']
        ):
            pit_mse_loss.append(pt.ops.losses.loss.pit_mse_loss(
                mask * observation[:, None, :],
                target
            ))

        pit_ips_loss = list()
        for mask, observation, target, cos_phase_diff in zip(
                model_out,
                batch['Y_abs'],
                batch['X_abs'],
                batch['cos_phase_difference']
        ):
            estimation = mask * observation[:, None, :]
            pit_ips_loss.append(pt.ops.losses.loss.pit_mse_loss(
                estimation,
                target * cos_phase_diff
            ))
        losses = {
            'losses': {
                'pit_mse_loss': torch.mean(torch.stack(pit_mse_loss)),
                'pit_ips_loss': torch.mean(torch.stack(pit_ips_loss)),

            }
        }

        estimation = model_out[0]*batch['Y_abs'][0]

        images = dict()
        images['observation'] = stft_to_image(batch['Y_abs'][0], True)
        images['mask'] = mask_to_image(model_out[0], True)
        images['estimation'] = stft_to_image(estimation, True)
        images['target'] = stft_to_image(batch['X_abs'][0], True)

        return dict(scalars=losses,
                    images=images
                    )


class PermutationInvariantTrainingModel(pt.Model):
    """
    Implements a variant of Permutation Invariant Training [1].

    An example notebook can be found here:
    /net/vol/ldrude/share/2018-12-14_pytorch.ipynb

    Check out this repository to see example code:
    git clone git@ntgit.upb.de:scratch/ldrude/pth_bss

    [1] Kolbaek 2017, https://arxiv.org/pdf/1703.06284.pdf

    TODO: Input normalization
    TODO: Batch normalization

    """
    def __init__(
            self,
            F=257,
            recurrent_layers=3,
            units=600,
            K=2,
            dropout_input=0.,
            dropout_hidden=0.,
            dropout_linear=0.,
            output_activation='relu'
    ):
        """

        Args:
            F: Number of frequency bins, fft_size / 2 + 1
            recurrent_layers:
            units: results in `units` forward and `units` backward units
            K: Number of output streams/ speakers
            dropout_input: Dropout forget ratio before first recurrent layer
            dropout_hidden: Vertical forget ratio dropout between each
                recurrent layer
            dropout_linear: Dropout forget ratio before first linear layer
            output_activation: Different activations. Default is 'ReLU'.
        """
        super().__init__()

        self.K = K
        self.F = F

        assert dropout_input <= 0.5, dropout_input
        self.dropout_input = torch.nn.Dropout(dropout_input)

        assert dropout_hidden <= 0.5, dropout_hidden
        self.blstm = torch.nn.LSTM(
            F,
            units,
            recurrent_layers,
            bidirectional=True,
            dropout=dropout_hidden,
        )

        assert dropout_linear <= 0.5, dropout_linear
        self.dropout_linear = torch.nn.Dropout(dropout_linear)
        self.linear1 = torch.nn.Linear(2 * units, 2 * units)
        self.linear2 = torch.nn.Linear(2 * units, F * K)
        self.output_activation = ACTIVATION_FN_MAP[output_activation]()

    def forward(self, batch):
        """

        Args:
            batch: Dictionary with lists of tensors

        Returns: List of mask tensors
            Each list element has shape (T, K, F)

        """

        h = pt.ops.pack_sequence(batch['Y_abs'])

        _, F = h.data.size()
        assert F == self.F, f'self.F = {self.F} != F = {F}'

        h_data = self.dropout_input(h.data)

        h_data = pt.ops.sequence.log1p(h_data)
        h = PackedSequence(h_data, h.batch_sizes)

        # Returns tensor with shape (t, b, num_directions * hidden_size)
        h, _ = self.blstm(h)

        h_data = self.dropout_linear(h.data)
        h_data = self.linear1(h_data)
        h_data = torch.nn.ReLU()(h_data)
        h_data = self.linear2(h_data)
        h_data = self.output_activation(h_data)
        h = PackedSequence(h_data, h.batch_sizes)

        mask = PackedSequence(
            einops.rearrange(h.data, 'tb (k f) -> tb k f', k=self.K),
            h.batch_sizes,
        )
        return pt.ops.unpack_sequence(mask)

    def review(self, batch, model_out):
        # TODO: Maybe calculate only one loss? May be much faster.

        pit_mse_loss = list()
        for mask, observation, target in zip(
                model_out,
                batch['Y_abs'],
                batch['X_abs']
        ):
            pit_mse_loss.append(pt.ops.losses.loss.pit_mse_loss(
                mask * observation[:, None, :],
                target
            ))

        pit_ips_loss = list()
        for mask, observation, target, cos_phase_diff in zip(
            model_out,
            batch['Y_abs'],
            batch['X_abs'],
            batch['cos_phase_difference']
        ):
            pit_ips_loss.append(pt.ops.losses.loss.pit_mse_loss(
                mask * observation[:, None, :],
                target * cos_phase_diff
            ))

        losses = {
                'pit_mse_loss': torch.mean(torch.stack(pit_mse_loss)),
                'pit_ips_loss': torch.mean(torch.stack(pit_ips_loss)),
        }
        #estimation = model_out[0][:]*batch['Y_abs'][0]

        b = 0
        images = dict()
        images['observation'] = stft_to_image(batch['Y_abs'][b])
        images['mask'] = mask_to_image(model_out[b])
        images['target'] = stft_to_image(batch['X_abs'][b])

        return dict(losses=losses,
                    images=images
                    )


class DeepClusteringModel(pt.Model):
    def __init__(
            self,
            F=257,
            recurrent_layers=2,
            units=600,
            E=20,
            input_feature_transform='identity'
    ):
        """

        TODO: Dropout
        TODO: Loss mask to avoid to assign embeddings to silent regions

        Args:
            F: Number of frequency bins, fft_size / 2 + 1
            recurrent_layers:
            units: results in `units` forward and `units` backward units
            E: Dimensionality of the embedding
        """
        super().__init__()
        self.E = E
        self.F = F
        self.input_feature_transform = input_feature_transform
        self.blstm = torch.nn.LSTM(
            F, units, recurrent_layers, bidirectional=True
        )
        self.linear = torch.nn.Linear(2 * units, F * E)

    def forward(self, batch):
        """

        Args:
            batch: Dictionary with lists of tensors

        Returns: List of mask tensors

        """

        h = pt.ops.pack_sequence(batch['Y_abs'])

        if self.input_feature_transform == 'identity':
            pass
        elif self.input_feature_transform == 'log1p':
            # This is equal to the mu-law for mu=1.
            h = pt.ops.sequence.log1p(h)
        elif self.input_feature_transform == 'log':
            h = PackedSequence(h.data + 1e-10, h.batch_sizes)
            h = pt.ops.sequence.log(h)
        else:
            raise NotImplementedError(self.input_feature_transform)

        _, F = h.data.size()
        assert F == self.F, f'self.F = {self.F} != F = {F}'

        # Returns tensor with shape (t, b, num_directions * hidden_size)
        h, _ = self.blstm(h)

        h = PackedSequence(self.linear(h.data), h.batch_sizes)
        h_data = einops.rearrange(h.data, 'tb (e f) -> tb e f', e=self.E)

        # Hershey 2016 page 2 top right paragraph: Unit norm
        h_data = torch.nn.functional.normalize(h_data, dim=-2)

        embedding = PackedSequence(h_data, h.batch_sizes,)
        embedding = pt.ops.unpack_sequence(embedding)
        return embedding

    def review(self, batch, model_out):
        dc_loss = list()
        for embedding, target_mask in zip(model_out, batch['target_mask']):
            dc_loss.append(pt.ops.losses.deep_clustering_loss(
                einops.rearrange(embedding, 't e f -> (t f) e'),
                einops.rearrange(target_mask, 't k f -> (t f) k')
            ))

        return {'losses': {'dc_loss': torch.mean(torch.stack(dc_loss))}}
