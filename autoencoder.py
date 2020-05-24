import torch as th
import gc
from op import FusedLeakyReLU
from tqdm import tqdm
from torch.utils import data
from torchvision import transforms, utils
from dataset import MultiResolutionDataset
import torch.nn.functional as F
import wandb
from copy import copy
import numpy as np
import argparse
from GPyOpt.methods import BayesianOptimization
import os
import validation
import torch.distributed as dist
from distributed import (
    get_rank,
    synchronize,
    reduce_loss_dict,
    reduce_sum,
    get_world_size,
)


def data_sampler(dataset, shuffle, distributed):
    if distributed:
        return data.distributed.DistributedSampler(dataset, shuffle=shuffle)
    if shuffle:
        return data.RandomSampler(dataset)
    else:
        return data.SequentialSampler(dataset)


def sample_data(loader):
    while True:
        for batch in loader:
            yield batch


def accumulate(model1, model2, decay=0.5 ** (32.0 / 10_000)):
    par1 = dict(model1.named_parameters())
    par2 = dict(model2.named_parameters())
    for n, p in model1.named_parameters():
        p.data = decay * par1[n].data + (1 - decay) * par2[n].data


class PrintShape(th.nn.Module):
    def __init__(self):
        super(PrintShape, self).__init__()

    def forward(self, x):
        print(x.shape)
        return x


class Flatten(th.nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


class UnFlatten(th.nn.Module):
    def __init__(self, channels, size):
        super(UnFlatten, self).__init__()
        self.channels = channels
        self.size = size

    def forward(self, x):
        return x.view(x.size(0), self.channels, self.size, self.size)


def info(x):
    print(x.shape, x.min(), x.mean(), x.max())


class LogCoshVAE(th.nn.Module):
    def __init__(self, in_channels, latent_dim, hidden_dims=None, alpha=10.0, beta=1.0, kld_weight=1):
        super(LogCoshVAE, self).__init__()

        my_hidden_dims = copy(hidden_dims)

        self.latent_dim = latent_dim
        self.alpha = alpha
        self.beta = beta
        self.kld_weight = kld_weight

        modules = []
        if my_hidden_dims is None:
            my_hidden_dims = [32, 64, 128, 256, 512]

        # Build Encoder
        for h_dim in my_hidden_dims:
            modules.append(
                th.nn.Sequential(
                    th.nn.Conv2d(in_channels, out_channels=h_dim, kernel_size=3, stride=2, padding=1),
                    th.nn.BatchNorm2d(h_dim),
                    FusedLeakyReLU(h_dim),
                )
            )
            in_channels = h_dim

        self.encoder = th.nn.Sequential(*modules)
        self.fc_mu = th.nn.Linear(my_hidden_dims[-1] * 4, latent_dim)
        self.fc_var = th.nn.Linear(my_hidden_dims[-1] * 4, latent_dim)

        # Build Decoder
        modules = []

        self.decoder_input = th.nn.Linear(latent_dim, my_hidden_dims[-1] * 4)

        my_hidden_dims.reverse()

        for i in range(len(my_hidden_dims) - 1):
            modules.append(
                th.nn.Sequential(
                    th.nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
                    th.nn.Conv2d(my_hidden_dims[i], my_hidden_dims[i + 1], kernel_size=3, padding=1),
                    th.nn.BatchNorm2d(my_hidden_dims[i + 1]),
                    FusedLeakyReLU(my_hidden_dims[i + 1]),
                )
            )

        self.decoder = th.nn.Sequential(*modules)

        self.final_layer = th.nn.Sequential(
            th.nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
            th.nn.Conv2d(my_hidden_dims[-1], my_hidden_dims[-1], kernel_size=3, padding=1),
            th.nn.BatchNorm2d(my_hidden_dims[-1]),
            FusedLeakyReLU(my_hidden_dims[-1]),
            th.nn.Conv2d(my_hidden_dims[-1], out_channels=3, kernel_size=3, padding=1),
            th.nn.Tanh(),
        )

    def encode(self, input):
        result = self.encoder(input)
        result = th.flatten(result, start_dim=1)

        mu = self.fc_mu(result)
        log_var = self.fc_var(result)

        return mu, log_var

    def decode(self, z):
        result = self.decoder_input(z)
        result = result.view(-1, self.latent_dim, 2, 2)
        result = self.decoder(result)
        result = self.final_layer(result)
        return result

    def reparameterize(self, mu, logvar):
        std = th.exp(0.5 * logvar)
        eps = th.randn_like(std)
        return eps * std + mu

    def forward(self, input):
        mu, log_var = self.encode(input)
        z = self.reparameterize(mu, log_var)
        return self.decode(z), mu, log_var

    def loss(self, real, fake, mu, log_var):
        t = fake - real

        recons_loss = self.alpha * t + th.log(1.0 + th.exp(-2 * self.alpha * t)) - th.log(th.tensor(2.0))
        recons_loss = (1.0 / self.alpha) * recons_loss.mean()

        kld_loss = th.mean(-0.5 * th.sum(1 + log_var - mu ** 2 - log_var.exp(), dim=1), dim=0)

        loss = recons_loss + self.beta * self.kld_weight * kld_loss

        return {"Total": loss, "Reconstruction": recons_loss, "Kullback Leibler Divergence": -kld_loss}


if __name__ == "__main__":
    device = "cuda"
    th.backends.cudnn.benchmark = True

    wandb.init(project=f"maua-stylegan")

    def train(latent_dim, learning_rate, number_filters, vae_alpha, vae_beta, kl_divergence_weight):
        print(
            f"latent_dim={latent_dim}",
            f"learning_rate={learning_rate}",
            f"number_filters={number_filters}",
            f"vae_alpha={vae_alpha}",
            f"vae_beta={vae_beta}",
            f"kl_divergence_weight={kl_divergence_weight}",
        )

        batch_size = 512
        i = None
        while batch_size >= 1:
            try:
                transform = transforms.Compose(
                    [
                        transforms.Resize(128),
                        transforms.RandomHorizontalFlip(p=0.5),
                        transforms.ToTensor(),
                        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True),
                    ]
                )
                data_path = "/home/hans/trainsets/cyphis"
                name = os.path.splitext(os.path.basename(data_path))[0]
                dataset = MultiResolutionDataset(data_path, transform, 256)
                dataloader = data.DataLoader(
                    dataset,
                    batch_size=int(batch_size),
                    sampler=data_sampler(dataset, shuffle=True, distributed=False),
                    num_workers=12,
                    drop_last=True,
                )
                loader = sample_data(dataloader)
                sample_imgs = next(loader)[:24]
                wandb.log(
                    {"Real Images": [wandb.Image(utils.make_grid(sample_imgs, nrow=6, normalize=True, range=(-1, 1)))]}
                )

                hidden_dims = [min(int(number_filters) * 2 ** i, latent_dim) for i in range(5)] + [latent_dim]
                vae, vae_optim = None, None
                vae = LogCoshVAE(
                    3,
                    latent_dim,
                    hidden_dims=hidden_dims,
                    alpha=vae_alpha,
                    beta=vae_beta,
                    kld_weight=kl_divergence_weight,
                ).to(device)
                vae.train()
                vae_optim = th.optim.Adam(vae.parameters(), lr=learning_rate)

                mse_loss = th.nn.MSELoss()

                sample_z = th.randn(size=(24, latent_dim))

                scores = []
                num_iters = 20_000
                pbar = range(num_iters)
                if get_rank() == 0:
                    pbar = tqdm(pbar, smoothing=0.1)
                for i in pbar:
                    vae.train()

                    real = next(loader).to(device)
                    fake, mu, log_var = vae(real)

                    loss_dict = vae.loss(real, fake, mu, log_var)
                    loss = loss_dict["Total"]

                    vae.zero_grad()
                    loss.backward()
                    vae_optim.step()

                    wandb.log(
                        {
                            "VAE": loss,
                            "Reconstruction": loss_dict["Reconstruction"],
                            "KL Divergence": loss_dict["Kullback Leibler Divergence"],
                        }
                    )

                    if i % int(num_iters / 20) == 0 or i + 1 == num_iters:
                        with th.no_grad():
                            vae.eval()

                            sample, _, _ = vae(sample_imgs.to(device))
                            grid = utils.make_grid(sample, nrow=6, normalize=True, range=(-1, 1),)
                            del sample
                            wandb.log({"Reconstructed Images VAE": [wandb.Image(grid, caption=f"Step {i}")]})

                            sample = vae.decode(sample_z.to(device))
                            grid = utils.make_grid(sample, nrow=6, normalize=True, range=(-1, 1),)
                            del sample
                            wandb.log({"Generated Images VAE": [wandb.Image(grid, caption=f"Step {i}")]})

                    if i % int(num_iters / 10) == 0 or i + 1 == num_iters:
                        with th.no_grad():
                            fid_dict = validation.vae_fid(vae, int(batch_size), latent_dim, 5000, name)
                            wandb.log(fid_dict)
                            mse = mse_loss(fake, real) * 5000
                            score = fid_dict["FID"] + mse
                            wandb.log({"Score": score})
                            pbar.set_description(f"FID: {fid_dict['FID']:.2f} MSE: {mse:.2f}")

                        if i >= num_iters / 2:
                            scores.append(score)

                    if th.isnan(loss).any():
                        print("NaN losses, exiting...")
                        wandb.log({"Score": 27000})
                        return

                weights = np.sqrt(np.arange(1, len(scores) + 1))
                weights /= sum(weights)
                weighted_scores = sum([w * v for w, v in zip(weights, scores)])
                wandb.log({"Score": weighted_scores})
                return

            except RuntimeError as e:
                if i is not None:
                    if i > 10_000:
                        return

                if "CUDA out of memory" in str(e):
                    batch_size = batch_size / 2

                    if batch_size < 1:
                        print("This configuration does not fit into memory, exiting...")
                        wandb.log({"Score": 27000})
                        return

                    print(f"Out of memory, halving batch size... {batch_size}")
                    if vae is not None:
                        del vae
                    if vae_optim is not None:
                        del vae_optim
                    gc.collect()
                    th.cuda.empty_cache()

                else:
                    print(e)
                    return

    parser = argparse.ArgumentParser()
    parser.add_argument("--latent_dim", type=int, default=8)
    parser.add_argument("--learning_rate", type=float, default=-3)
    parser.add_argument("--number_filters", type=int, default=4)
    parser.add_argument("--vae_alpha", type=float, default=-2)
    parser.add_argument("--vae_beta", type=float, default=-3)
    parser.add_argument("--kl_divergence_weight", type=float, default=-3)
    args = parser.parse_args()

    train(
        2 ** args.latent_dim,
        10 ** args.learning_rate,
        2 ** args.number_filters,
        10 ** args.vae_alpha,
        10 ** args.vae_beta,
        10 ** args.kl_divergence_weight,
    )

