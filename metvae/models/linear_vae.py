"""
Author : XuchanBao
This code was adapted from
https://github.com/XuchanBao/linear-ae
"""
import torch
import torch.nn as nn
from catvae.composition import ilr
from gneiss.cluster import random_linkage
from gneiss.balances import sparse_balance_basis
from torch.distributions import Multinomial, LogNormal
import torch.nn.functional as F
import numpy as np

LOG_2_PI = np.log(2.0 * np.pi)


class LinearVAE(nn.Module):

    def __init__(self, input_dim, hidden_dim, init_scale=0.001,
                 use_analytic_elbo=True, encoder_depth=1,
                 likelihood='gaussian', basis=None, bias=False):
        super(LinearVAE, self).__init__()
        self.bias = bias
        self.hidden_dim = hidden_dim
        self.likelihood = likelihood
        self.use_analytic_elbo = use_analytic_elbo

        if basis is None:
            tree = random_linkage(input_dim)
            basis = sparse_balance_basis(tree)[0].copy()
        indices = np.vstack((basis.row, basis.col))
        Psi = torch.sparse_coo_tensor(
            indices.copy(), basis.data.astype(np.float32).copy(),
            requires_grad=False)
        self.input_dim = Psi.shape[0]
        self.register_buffer('Psi', Psi)

        if encoder_depth > 1:
            self.first_encoder = nn.Linear(
                self.input_dim, hidden_dim, bias=self.bias)
            num_encoder_layers = encoder_depth
            layers = []
            layers.append(self.first_encoder)
            for layer_i in range(num_encoder_layers - 1):
                layers.append(nn.Softplus())
                layers.append(
                    nn.Linear(hidden_dim, hidden_dim, bias=self.bias))
            self.encoder = nn.Sequential(*layers)

            # initialize
            for encoder_layer in self.encoder:
                if isinstance(encoder_layer, nn.Linear):
                    encoder_layer.weight.data.normal_(0.0, init_scale)

        else:
            self.encoder = nn.Linear(self.input_dim, hidden_dim, bias=self.bias)
            self.encoder.weight.data.normal_(0.0, init_scale)

        self.decoder = nn.Linear(hidden_dim, self.input_dim, bias=self.bias)
        self.imputer = lambda x: x + 1
        self.variational_logvars = nn.Parameter(torch.zeros(hidden_dim))
        self.log_sigma_sq = nn.Parameter(torch.tensor(0.0))

    def gaussian_kl(self, z_mean, z_logvar):
        return 0.5 * (1 + z_logvar - z_mean * z_mean - torch.exp(z_logvar))
        # return 0.5 * (1 + z_logvar - z_mean * z_mean - torch.exp(z_logvar))

    def recon_model_loglik(self, x, eta):
        # WARNING : the gaussian likelidhood is not supported
        if self.likelihood == 'gaussian':
            x_in = self.Psi.t() @ torch.log(x + 1).t()
            diff = (x - eta) ** 2
            sigma_sq = torch.exp(self.log_sigma_sq)
            # No dimension constant as we sum after
            return 0.5 * (-diff / sigma_sq - LOG_2_PI - self.log_sigma_sq)
        elif self.likelihood == 'multinomial':
            logp = (self.Psi.t() @ eta.t()).t()
            mult_loss = Multinomial(logits=logp).log_prob(x).mean()
            return mult_loss
        elif self.likelihood == 'lognormal':
            logp = F.logsoftmax((self.Psi.t() @ eta.t()).t(), axis=-1)
            logN = torch.log(x.sum(axis=-1))
            mu = logp + logN
            sigma_sq = torch.exp(self.log_sigma_sq)
            nz = x > 0
            logn_loss = LogNormal(loc=mu[nz], scale=sigma_sq).log_prob(x[nz])
            return logn_loss.mean()
        else:
            raise ValueError(
                f'{self.likelihood} has not be properly specified.')

    def analytic_exp_recon_loss(self, x):
        z_logvar = self.variational_logvars
        tr_wdw = torch.trace(
            torch.mm(self.decoder.weight,
                     torch.mm(torch.diag(torch.exp(z_logvar)),
                              self.decoder.weight.t())))

        wv = torch.mm(self.decoder.weight, self.encoder.weight)
        vtwtwv = wv.t().mm(wv)
        xtvtwtwvx = (x * torch.mm(x, vtwtwv)).mean(0).sum()
        xtwvx = 2.0 * (x * x.mm(wv)).mean(0).sum()
        xtx = (x * x).mean(0).sum()
        exp_recon_loss = -(
            tr_wdw + xtvtwtwvx - xtwvx + xtx) / (
                2.0 * torch.exp(self.log_sigma_sq)) - x.shape[-1] * (
                    LOG_2_PI + self.log_sigma_sq) / 2.0
        return exp_recon_loss

    def analytic_elbo(self, x, z_mean):
        """Computes the analytic ELBO for a linear VAE."""
        z_logvar = self.variational_logvars
        kl_div = (-self.gaussian_kl(z_mean, z_logvar)).mean(0).sum()
        exp_recon_loss = self.analytic_exp_recon_loss(x)
        return kl_div - exp_recon_loss

    def encode(self, x):
        hx = ilr(self.imputer(x), self.Psi)
        z = self.encoder(hx)
        return z

    def forward(self, x):
        x_ = ilr(self.imputer(x), self.Psi)
        z_mean = self.encoder(x_)

        if not self.use_analytic_elbo:
            eps = torch.normal(torch.zeros_like(z_mean), 1.0)
            z_sample = z_mean + eps * torch.exp(0.5 * self.variational_logvars)
            x_out = self.decoder(z_sample)
            kl_div = (-self.gaussian_kl(
                z_mean, self.variational_logvars)).mean(0).sum()
            recon_loss = (-self.recon_model_loglik(x, x_out)).mean(0).sum()
            loss = kl_div + recon_loss
        else:
            loss = self.analytic_elbo(x_, z_mean)
        return loss

    def get_reconstruction_loss(self, x):
        x_ = ilr(self.imputer(x), self.Psi)
        if self.use_analytic_elbo:
            return - self.analytic_exp_recon_loss(x_)
        else:
            z_mean = self.encoder(x_)
            eps = torch.normal(torch.zeros_like(z_mean), 1.0)
            z_sample = z_mean + eps * torch.exp(0.5 * self.variational_logvars)
            x_out = self.decoder(z_sample)
            recon_loss = -self.recon_model_loglik(x, x_out)
            return recon_loss


class LinearBatchVAE(LinearVAE):
    def __init__(self, input_dim, hidden_dim, init_scale=0.001,
                 encoder_depth=1,
                 likelihood='gaussian', basis=None, bias=False):
        """ Only the stochastic version will be made available. """
        super(LinearBatchVAE, self).__init__(
            input_dim, hidden_dim, init_scale,
            likelihood=likelihood,
            use_analytic_elbo=False,
            basis=basis, encoder_depth=encoder_depth,
            bias=bias)

    def encode(self, x):
        # B = B.sum(axis=0) + 1
        # B = B.unsqueeze(0)
        # batch_effects = (self.Psi @ B.t()).t()
        hx = ilr(self.imputer(x), self.Psi)
        #hx -= batch_effects  # Subtract out batch effects
        z = self.encoder(hx)
        return z

    def forward(self, x, B):
        hx = ilr(self.imputer(x), self.Psi)
        batch_effects = (self.Psi @ B.t()).t()
        hx -= batch_effects  # Subtract out batch effects
        z_mean = self.encoder(hx)
        eps = torch.normal(torch.zeros_like(z_mean), 1.0)
        z_sample = z_mean + eps * torch.exp(0.5 * self.variational_logvars)
        x_out = self.decoder(z_sample)
        x_out += batch_effects  # Add batch effects back in
        kl_div = (-self.gaussian_kl(
            z_mean, self.variational_logvars)).mean(0).sum()
        recon_loss = (-self.recon_model_loglik(x, x_out)).mean(0).sum()
        loss = kl_div + recon_loss
        return loss

    def get_reconstruction_loss(self, x, B):
        hx = ilr(self.imputer(x), self.Psi)
        batch_effects = (self.Psi @ B.t()).t()
        hx -= batch_effects  # Subtract out batch effects
        z_mean = self.encoder(hx)
        eta = self.decoder(z_mean)
        eta += batch_effects  # Add batch effects back in
        recon_loss = -self.recon_model_loglik(x, eta)
        return recon_loss
