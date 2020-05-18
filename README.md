This is my fork of stylegan2-pytorch. There are many like it, but this one is mine.

#  rosinality/stylegan2-pytorch README

Implementation of Analyzing and Improving the Image Quality of StyleGAN (https://arxiv.org/abs/1912.04958) in PyTorch

## Usage

First create lmdb datasets:

> python prepare_data.py --out LMDB_PATH --n_worker N_WORKER --size SIZE1,SIZE2,SIZE3,... DATASET_PATH

This will convert images to jpeg and pre-resizes it. This implementation does not use progressive growing, but you can create multiple resolution datasets using size arguments with comma separated lists, for the cases that you want to try another resolutions later.

Then you can train model in distributed settings

> python -m torch.distributed.launch --nproc_per_node=N_GPU --master_port=PORT train.py --batch BATCH_SIZE LMDB_PATH

train.py supports Weights & Biases logging. If you want to use it, add --wandb arguments to the script.

### Convert weight from official checkpoints

You need to clone official repositories, (https://github.com/NVlabs/stylegan2) as it is requires for load official checkpoints.

For example, if you cloned repositories in ~/stylegan2 and downloaded stylegan2-ffhq-config-f.pkl, You can convert it like this:

> python convert_weight.py --repo ~/stylegan2 stylegan2-ffhq-config-f.pkl

This will create converted stylegan2-ffhq-config-f.pt file.  

### Generate samples

> python generate.py --sample N_FACES --pics N_PICS --ckpt PATH_CHECKPOINT  

You should change your size (--size 256 for example) if you train with another dimension.   

### Project images to latent spaces

> python projector.py --ckpt [CHECKPOINT] --size [GENERATOR_OUTPUT_SIZE] FILE1 FILE2 ...

## License

Model details and custom CUDA kernel codes are from official repostiories: https://github.com/NVlabs/stylegan2

Codes for Learned Perceptual Image Patch Similarity, LPIPS came from https://github.com/richzhang/PerceptualSimilarity

To match FID scores more closely to tensorflow official implementations, use FID Inception V3 implementations in https://github.com/mseitzer/pytorch-fid
