import torch

__all__ = [
    'move_axis'
]


def move_axis(a: torch.Tensor, source: int, destination: int):
    """Move an axis from source location to destination location.

    API is a bit closer to Numpy but does not allow more than one source.

    Params:
        a: The Tensor whose axes should be reordered.
        source: Original positions of the axis to move.
        destination: Destination positions for each of the original axis.
    Returns: Tensor with moved axis.

    >>> x = zeros((3, 4, 5))
    >>> move_axis(x, 0, -1).size()
    torch.Size([4, 5, 3])

    >>> move_axis(x, -1, 0).size()
    torch.Size([5, 3, 4])
    """
    source = source % len(a.size())
    destination = destination % len(a.size())
    permutation = [d for d in range(len(a.size())) if not d == source]
    permutation.insert(destination, source)
    return a.permute(permutation)


def zeros(shape, dtype=None):
    return torch.zeros(*shape, dtype=dtype)


def matrix_diag(x):
    """Apply the diag matrix operation along the batch axis."""
    if x.dim() == 1:
        return torch.diag(x)
    feature_dim = x.shape[-1]
    mat = x.reshape((-1, feature_dim))
    diags = torch.stack([torch.diag(vec) for vec in mat])
    return diags.reshape((*x.shape, feature_dim))


def matrix_eye_like(x):
    """Apply the eye matrix operation along the batch axis."""
    if x.dim() == 1:
        return torch.eye(*tensor.size())
    feature_dim = x.shape[-1]
    mat = x.reshape(-1, feature_dim)
    eyes = torch.stack([torch.eye(*vec.size()) for vec in mat])
    return eyes.reshape((*x.shape, feature_dim))


def batch_tril(x):
    """Apply torch.tril along the minibatch axis."""
    matrix_dims = x.shape[-2:]
    mats = x.reshape((-1, *matrix_dims))
    trils = torch.stack([torch.tril(mat) for mat in mats])
    return trils.reshape(x.shape)
