from itertools import product

import numpy as np
import torch
from torch import nn, optim


class ClassBalanceModel(nn.Module):
    """A model for learning the class balance, P(Y=y), given a subset of LFs
    which are *conditionally independent*, i.e. \lambda_i \perp \lambda_j | Y,
    for  i != j.

    Learns the model using a tensor factorization approach.

    Note: This approach can also be used for estimation of the LabelModel, may
    want to later refactor and expand this class.
    """

    def __init__(self, k, abstains=True, config=None):
        super().__init__()
        self.config = config
        self.k = k  # The cardinality of the true label, Y \in {1,...,k}

        # Labeling functions output labels in range {k_0,...,k}, and have
        # cardinality k_lf
        # If abstains=False, k_0 = 1 ==> k_lf = k
        # If abstains=True, k_0 = 0 ==> k_lf = k + 1
        self.abstains = abstains
        self.k_0 = 0 if self.abstains else 1
        self.k_lf = k + 1 if self.abstains else k

        # Estimated quantities (np.array)
        self.cond_probs = None
        self.class_balance = None

    def _get_overlaps_tensor(self, L):
        """Transforms the input label matrix to a three-way overlaps tensor.

        Args:
            L: (np.array) An n x m array of LF output labels, in {0,...,k} if
                self.abstains, else in {1,...,k}, generated by m conditionally
                independent LFs on n data points

        Outputs:
            O: (torch.Tensor) A (m, m, m, k, k, k) tensor of the label-specific
            empirical overlap rates; that is,

                O[i,j,k,y1,y2,y3] = P(\lf_i = y1, \lf_j = y2, \lf_k = y3)

            where this quantity is computed empirically by this function, based
            on the label matrix L.
        """
        n, m = L.shape

        # Convert from a (n,m) matrix of ints to a (k_lf, n, m) indicator tensor
        LY = np.array([np.where(L == y, 1, 0) for y in range(self.k_0, self.k + 1)])

        # Form the three-way overlaps matrix
        O = np.einsum("abc,dbe,fbg->cegadf", LY, LY, LY) / n
        return torch.from_numpy(O).float()

    def get_mask(self, m):
        """Get the mask for the three-way overlaps matrix O, which is 0 when
        indices i,j,k are not unique"""
        mask = torch.ones((m, m, m, self.k_lf, self.k_lf, self.k_lf)).byte()
        for i, j, k in product(range(m), repeat=3):
            if len(set((i, j, k))) < 3:
                mask[i, j, k, :, :, :] = 0
        return mask

    @staticmethod
    def get_loss(O, Q, mask):
        # Main constraint: match empirical three-way overlaps matrix
        # (entries O_{ijk} for i != j != k)
        diffs = (O - torch.einsum("aby,cdy,efy->acebdf", [Q, Q, Q]))[mask]
        return torch.norm(diffs) ** 2

    def train_model(self, L=None, O=None, lr=1, max_iter=1000, verbose=False):
        # Get overlaps tensor if L provided else use O directly (e.g. for tests)
        if O is not None:
            pass
        elif L is not None:
            O = self._get_overlaps_tensor(L)
        else:
            raise ValueError("L or O required as input.")
        self.m = O.shape[0]

        # Compute mask
        self.mask = self.get_mask(self.m)

        # Initialize parameters
        self.Q = nn.Parameter(torch.rand(self.m, self.k_lf, self.k)).float()

        # Use L-BFGS here
        # Seems to be a tricky problem for simple 1st order approaches, and
        # small enough for quasi-Newton... L-BFGS seems to work well here
        optimizer = optim.LBFGS([self.Q], lr=lr, max_iter=max_iter)

        # The closure computes the loss
        def closure():
            optimizer.zero_grad()
            loss = self.get_loss(O, self.Q, self.mask)
            loss.backward()
            if verbose:
                print(f"Loss: {loss.detach():.8f}")
            return loss

        # Perform optimizer step
        optimizer.step(closure)

        # Recover the class balance
        # Note that the columns are not necessarily ordered correctly at this
        # point, since there's a column-wise symmetry remaining
        q = self.Q.detach().numpy()
        p_y = np.mean(q.sum(axis=1) ** 3, axis=0)

        # Resolve remaining col-wise symmetry
        # We do this by first estimating the conditional probabilities (accs.)
        # P(\lambda_i = y' | Y = y) of the labeling functions, *then leveraging
        # the assumption that they are better than random* to resolve col-wise
        # symmetries here
        # Note we then store both the estimated conditional probs, and the class
        # balance

        # Recover the estimated cond probs: Q = C(P^{1/3}) --> C = Q(P^{-1/3})
        cps = q @ np.diag(1 / p_y ** (1 / 3))

        # Note: For assessing the order, we only care about the non-abstains
        if self.k_lf > self.k:
            cps_na = cps[:, 1:, :]
        else:
            cps_na = cps

        # Re-order cps and p_y using assumption and store np.array values
        # Note: We take the *most common* ordering
        vals, counts = np.unique(cps_na.argmax(axis=2), axis=0, return_counts=True)
        col_order = vals[counts.argmax()]
        self.class_balance = p_y[col_order]
        self.cond_probs = cps[col_order]
