import random
from contextlib import contextmanager
from dataclasses import dataclass, field

import torch
import torch.nn as nn
import torch.nn.functional as F
from diffusers import (
    DDPMScheduler,
    DPMSolverMultistepScheduler,
    StableDiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers.loaders import AttnProcsLayers
from diffusers.models.embeddings import TimestepEmbedding
from diffusers.utils.import_utils import is_xformers_available

import threestudio
from threestudio.models.prompt_processors.base_sd3 import PromptProcessorOutput_sd3 as PromptProcessorOutput
from threestudio.utils.base import BaseModule
from threestudio.utils.misc import C, cleanup, parse_version
from threestudio.utils.typing import *
from diffusers import StableDiffusion3Pipeline
import copy
class ToWeightsDType(nn.Module):
    def __init__(self, module: nn.Module, dtype: torch.dtype):
        super().__init__()
        self.module = module
        self.dtype = dtype

    def forward(self, x: Float[Tensor, "..."]) -> Float[Tensor, "..."]:
        return self.module(x).to(self.dtype)
import inspect
def retrieve_timesteps(
    scheduler,
    num_inference_steps: Optional[int] = None,
    device: Optional[Union[str, torch.device]] = None,
    timesteps: Optional[List[int]] = None,
    sigmas: Optional[List[float]] = None,
    **kwargs,
):
    """
    Calls the scheduler's `set_timesteps` method and retrieves timesteps from the scheduler after the call. Handles
    custom timesteps. Any kwargs will be supplied to `scheduler.set_timesteps`.

    Args:
        scheduler (`SchedulerMixin`):
            The scheduler to get timesteps from.
        num_inference_steps (`int`):
            The number of diffusion steps used when generating samples with a pre-trained model. If used, `timesteps`
            must be `None`.
        device (`str` or `torch.device`, *optional*):
            The device to which the timesteps should be moved to. If `None`, the timesteps are not moved.
        timesteps (`List[int]`, *optional*):
            Custom timesteps used to override the timestep spacing strategy of the scheduler. If `timesteps` is passed,
            `num_inference_steps` and `sigmas` must be `None`.
        sigmas (`List[float]`, *optional*):
            Custom sigmas used to override the timestep spacing strategy of the scheduler. If `sigmas` is passed,
            `num_inference_steps` and `timesteps` must be `None`.

    Returns:
        `Tuple[torch.Tensor, int]`: A tuple where the first element is the timestep schedule from the scheduler and the
        second element is the number of inference steps.
    """
    if timesteps is not None and sigmas is not None:
        raise ValueError("Only one of `timesteps` or `sigmas` can be passed. Please choose one to set custom values")
    if timesteps is not None:
        accepts_timesteps = "timesteps" in set(inspect.signature(scheduler.set_timesteps).parameters.keys())
        if not accepts_timesteps:
            raise ValueError(
                f"The current scheduler class {scheduler.__class__}'s `set_timesteps` does not support custom"
                f" timestep schedules. Please check whether you are using the correct scheduler."
            )
        scheduler.set_timesteps(timesteps=timesteps, device=device, **kwargs)
        timesteps = scheduler.timesteps
        num_inference_steps = len(timesteps)
    elif sigmas is not None:
        accept_sigmas = "sigmas" in set(inspect.signature(scheduler.set_timesteps).parameters.keys())
        if not accept_sigmas:
            raise ValueError(
                f"The current scheduler class {scheduler.__class__}'s `set_timesteps` does not support custom"
                f" sigmas schedules. Please check whether you are using the correct scheduler."
            )
        scheduler.set_timesteps(sigmas=sigmas, device=device, **kwargs)
        timesteps = scheduler.timesteps
        num_inference_steps = len(timesteps)
    else:
        scheduler.set_timesteps(num_inference_steps, device=device, **kwargs)
        timesteps = scheduler.timesteps
    return timesteps, num_inference_steps
class MeanVarianceLoss(nn.Module):
    def __init__(self):
        super(MeanVarianceLoss, self).__init__()

    def forward(self, input):
        # Calculate the mean of the tensor
        mean_loss = input.mean()

        # Calculate the variance of the tensor
        var_loss = input.var()

        # Calculate the mean squared errors
        mean_mse = (mean_loss - 0) ** 2
        var_mse = (var_loss - 1) ** 2

        # Summing up the mean mse and variance mse
        total_loss = mean_mse + var_mse

        return total_loss

@threestudio.register("iRFDS-sd3")
class RectifiedFlowGuidance(BaseModule):
    @dataclass
    class Config(BaseModule.Config):
        pretrained_model_name_or_path: str = ""
        enable_memory_efficient_attention: bool = False
        enable_sequential_cpu_offload: bool = False
        enable_attention_slicing: bool = False
        enable_channels_last_format: bool = False
        guidance_scale: float = 7.5
        guidance_scale_lora: float = 7.5
        grad_clip: Optional[
            Any
        ] = None  # field(default_factory=lambda: [0, 2.0, 8.0, 1000])
        half_precision_weights: bool = True


        min_step_percent: float = 0.02
        max_step_percent: float = 0.98

        view_dependent_prompting: bool = True
        camera_condition_type: str = "extrinsics"

    cfg: Config

    def configure(self) -> None:
        threestudio.info(f"Loading Rectified Flow ...")

        self.weights_dtype = (
            torch.float16 if self.cfg.half_precision_weights else torch.float32
        )

        @dataclass
        class SubModules:
            pipe: StableDiffusionPipeline

        pipe = StableDiffusion3Pipeline.from_pretrained("stabilityai/stable-diffusion-3-medium-diffusers",
                                                        torch_dtype=torch.float16, tokenizer=None, safety_checker=None,
                                                        feature_extractor=None, requires_safety_checker=False)
        pipe = pipe.to(self.device)


        self.submodules = SubModules(pipe=pipe)
        if self.cfg.enable_memory_efficient_attention:
            if parse_version(torch.__version__) >= parse_version("2"):
                threestudio.info(
                    "PyTorch2.0 uses memory efficient attention by default."
                )
            elif not is_xformers_available():
                threestudio.warn(
                    "xformers is not available, memory efficient attention is not enabled."
                )
            else:
                self.pipe.enable_xformers_memory_efficient_attention()

        if self.cfg.enable_sequential_cpu_offload:
            self.pipe.enable_sequential_cpu_offload()

        if self.cfg.enable_attention_slicing:
            self.pipe.enable_attention_slicing(1)



        del self.pipe.text_encoder
        del self.pipe.text_encoder_2
        del self.pipe.text_encoder_3
        cleanup()

        for p in self.vae.parameters():
            p.requires_grad_(False)
        for p in self.transformer.parameters():
            p.requires_grad_(False)


        # FIXME: hard-coded dims
        self.camera_embedding = ToWeightsDType(
            TimestepEmbedding(16, 1280), self.weights_dtype
        )


        self.scheduler = self.pipe.scheduler


        self.scheduler_sample = self.pipe.scheduler


        self.num_train_timesteps = self.scheduler.config.num_train_timesteps
        self.set_min_max_steps()  # set to default value
        self.noise_scheduler_copy = copy.deepcopy(self.pipe.scheduler)
        self.timesteps, self.num_inference_steps = retrieve_timesteps(self.scheduler, 15, self.device, None)
        threestudio.info(f"Loaded Rectified Flow!")

    @torch.cuda.amp.autocast(enabled=False)
    def set_min_max_steps(self, min_step_percent=0.02, max_step_percent=0.98):
        self.min_step = int(self.num_train_timesteps * min_step_percent)
        self.max_step = int(self.num_train_timesteps * max_step_percent)

    @property
    def pipe(self):
        return self.submodules.pipe


    @property
    def transformer(self):
        return self.submodules.pipe.transformer


    @property
    def vae(self):
        return self.submodules.pipe.vae


    @torch.no_grad()
    @torch.cuda.amp.autocast(enabled=False)
    def _sample(
        self,
        pipe: StableDiffusionPipeline,
        sample_scheduler: DPMSolverMultistepScheduler,
        text_embeddings: Float[Tensor, "BB N Nf"],
        num_inference_steps: int,
        guidance_scale: float,
        num_images_per_prompt: int = 1,
        height: Optional[int] = None,
        width: Optional[int] = None,
        class_labels: Optional[Float[Tensor, "BB 16"]] = None,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
    ) -> Float[Tensor, "B H W 3"]:
        vae_scale_factor = 2 ** (len(pipe.vae.config.block_out_channels) - 1)
        height = height or pipe.unet.config.sample_size * vae_scale_factor
        width = width or pipe.unet.config.sample_size * vae_scale_factor
        batch_size = text_embeddings.shape[0] // 2
        device = self.device

        sample_scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = sample_scheduler.timesteps
        num_channels_latents = pipe.unet.config.in_channels

        latents = pipe.prepare_latents(
            batch_size * num_images_per_prompt,
            num_channels_latents,
            height,
            width,
            self.weights_dtype,
            device,
            generator,
        )

        for i, t in enumerate(timesteps):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2)
            latent_model_input = sample_scheduler.scale_model_input(
                latent_model_input, t
            )

            # predict the noise residual
            if class_labels is None:
                with self.disable_unet_class_embedding(pipe.unet) as unet:
                    noise_pred = unet(
                        latent_model_input,
                        t,
                        encoder_hidden_states=text_embeddings.to(self.weights_dtype),
                    ).sample
            else:
                noise_pred = pipe.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=text_embeddings.to(self.weights_dtype),
                ).sample

            noise_pred_text, noise_pred_uncond = noise_pred.chunk(2)
            noise_pred = noise_pred_uncond + guidance_scale * (
                noise_pred_text - noise_pred_uncond
            )

            # compute the previous noisy sample x_t -> x_t-1
            latents = sample_scheduler.step(noise_pred, t, latents).prev_sample

        latents = 1 / pipe.vae.config.scaling_factor * latents
        images = pipe.vae.decode(latents).sample
        images = (images / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
        images = images.permute(0, 2, 3, 1).float()
        return images

    def sample(
        self,
        prompt_utils: PromptProcessorOutput,
        elevation: Float[Tensor, "B"],
        azimuth: Float[Tensor, "B"],
        camera_distances: Float[Tensor, "B"],
        seed: int = 0,
        **kwargs,
    ) -> Float[Tensor, "N H W 3"]:
        # view-dependent text embeddings
        text_embeddings_vd = prompt_utils.get_text_embeddings(
            elevation,
            azimuth,
            camera_distances,
            view_dependent_prompting=self.cfg.view_dependent_prompting,
        )
        generator = torch.Generator(device=self.device).manual_seed(seed)

        return self._sample(
            pipe=self.pipe,
            sample_scheduler=self.scheduler_sample,
            text_embeddings=text_embeddings_vd,
            num_inference_steps=25,
            guidance_scale=self.cfg.guidance_scale,
            cross_attention_kwargs=cross_attention_kwargs,
            generator=generator,
        )

    @torch.cuda.amp.autocast(enabled=False)
    def forward_transformer(
        self,
        transformer,
        latents,
        t,
        prompt_embeds,
        pooled_prompt_embeds
    ) -> Float[Tensor, "..."]:
        input_dtype = latents.dtype
        return transformer(
            hidden_states=latents.to(self.weights_dtype),
            timestep=t.to(self.weights_dtype),
            encoder_hidden_states=prompt_embeds.to(self.weights_dtype),
            pooled_projections = pooled_prompt_embeds.to(self.weights_dtype),
            return_dict=False,
        )[0].to(input_dtype)

    @torch.cuda.amp.autocast(enabled=False)
    def encode_images(
        self, imgs: Float[Tensor, "B 3 512 512"]
    ) -> Float[Tensor, "B 4 64 64"]:
        input_dtype = imgs.dtype
        imgs = torch.clamp(imgs, min=0, max=1)
        imgs = self.pipe.image_processor.preprocess(imgs)
        posterior = self.vae.encode(imgs.to(self.weights_dtype)).latent_dist
        latents = posterior.sample()
        latents = (latents - self.vae.config.shift_factor) * self.vae.config.scaling_factor
        return latents.to(input_dtype)

    @torch.cuda.amp.autocast(enabled=False)
    def decode_latents(
        self,
        latents: Float[Tensor, "B 4 H W"],
        latent_height: int = 64,
        latent_width: int = 64,
    ) -> Float[Tensor, "B 3 512 512"]:
        # print(self.vae.config.shift_factor) 0.06
        # print(self.vae.config.scaling_factor) 1.53
        input_dtype = latents.dtype
        latents = F.interpolate(
            latents, (latent_height, latent_width), mode="bilinear", align_corners=False
        )
        latents = (latents / self.vae.config.scaling_factor) + self.vae.config.shift_factor
        image = self.vae.decode(latents.to(self.weights_dtype)).sample
        image = (image * 0.5 + 0.5).clamp(0, 1)
        return image.to(input_dtype)

    @contextmanager
    def disable_unet_class_embedding(self, unet: UNet2DConditionModel):
        class_embedding = unet.class_embedding
        try:
            unet.class_embedding = None
            yield unet
        finally:
            unet.class_embedding = class_embedding
    def get_sigmas(self,timesteps, n_dim=4, dtype=torch.float16):
        sigmas = self.noise_scheduler_copy.sigmas.to(device=self.device, dtype=dtype)
        schedule_timesteps = self.noise_scheduler_copy.timesteps.to(self.device)
        timesteps = timesteps.to(self.device)
        step_indices = [(torch.argmin(torch.abs(schedule_timesteps - t))).item() for t in timesteps]

        sigma = sigmas[step_indices].flatten()
        while len(sigma.shape) < n_dim:
            sigma = sigma.unsqueeze(-1)
        return sigma
    def compute_grad_irfds(
        self,
        latents: Float[Tensor, "B 4 64 64"],
        noise: Float[Tensor, "B 4 64 64"],
        text_embeddings_vd: Float[Tensor, "BB 77 768"],
        camera_condition: Float[Tensor, "B 4 4"],
    ):
        B = latents.shape[0]
        (
            text_embeddings, uncond_text_embeddings
        ) = text_embeddings_vd[0].chunk(2)
        (
            text_embeddings_pooled, uncond_text_embeddings_pooled
        ) = text_embeddings_vd[1].chunk(2)
        prompt_embeds = torch.cat([text_embeddings, uncond_text_embeddings])
        pooled_prompt_embeds = torch.cat([text_embeddings_pooled, uncond_text_embeddings_pooled]).squeeze(1)
        with torch.no_grad():

            indices = torch.randint(0, len(self.timesteps), (B,))
            timesteps =  self.timesteps[indices].to(device=self.device)

            # Add noise according to flow matching.
            sigmas = self.get_sigmas(timesteps, n_dim=latents.ndim, dtype=latents.dtype)
            latents_noisy = sigmas * noise + (1.0 - sigmas) * latents
            velocity = self.forward_transformer(
                self.transformer,
                latents_noisy,
                timesteps,
                text_embeddings,
                text_embeddings_pooled.squeeze(1),
                )

        u = torch.normal(mean=0, std=1, size=(B,), device=self.device)
        weighting = torch.nn.functional.sigmoid(u)
        # NOTE: guidance scale definition here is aligned with diffusers, but different from other guidance

        return velocity, noise, weighting


    def get_latents(
        self, rgb_BCHW: Float[Tensor, "B C H W"], rgb_as_latents=False
    ) -> Float[Tensor, "B 4 64 64"]:
        if rgb_as_latents:
            latents = F.interpolate(
                rgb_BCHW, (64, 64), mode="bilinear", align_corners=False
            )
        else:
            latents = self.encode_images(rgb_BCHW)
        return latents

    def forward(
        self,
        noise_to_optimize: Float[Tensor, "B H W C"],
        rgb: Float[Tensor, "B H W C"],
        prompt_utils: PromptProcessorOutput,
        elevation: Float[Tensor, "B"],
        azimuth: Float[Tensor, "B"],
        camera_distances: Float[Tensor, "B"],
        mvp_mtx: Float[Tensor, "B 4 4"],
        c2w: Float[Tensor, "B 4 4"],
        rgb_as_latents=False,
        **kwargs,
    ):
        batch_size = rgb.shape[0]
        rgb_BCHW = rgb.permute(0, 3, 1, 2)
        latents = self.get_latents(rgb_BCHW, rgb_as_latents=rgb_as_latents)
        # view-dependent text embeddings
        text_embeddings = prompt_utils.get_text_embeddings(
            elevation,
            azimuth,
            camera_distances,
            view_dependent_prompting=False,
        )
        if self.cfg.camera_condition_type == "extrinsics":
            camera_condition = c2w
        elif self.cfg.camera_condition_type == "mvp":
            camera_condition = mvp_mtx
        else:
            raise ValueError(
                f"Unknown camera_condition_type {self.cfg.camera_condition_type}"
            )

        grad, noise, weights = self.compute_grad_irfds(
            latents,noise_to_optimize,text_embeddings,  camera_condition
        )

        grad = torch.nan_to_num(grad)
        # use reparameterization trick
        latents = latents.detach()
        target = (grad).detach()
        loss_iRFDS = F.mse_loss(noise_to_optimize, target + latents, reduction="mean") / batch_size
        loss_fn = MeanVarianceLoss()
        loss_mean_var = loss_fn (noise_to_optimize)
        return {
            "loss_iRFDS": loss_iRFDS,
            "loss_regularize":loss_mean_var,
            "grad_norm": grad.norm(),
            "min_step": self.min_step,
            "max_step": self.max_step,
        }

    def update_step(self, epoch: int, global_step: int, on_load_weights: bool = False):
        # clip grad for stable training as demonstrated in
        # Debiasing Scores and Prompts of 2D Diffusion for Robust Text-to-3D Generation
        # http://arxiv.org/abs/2303.15413
        if self.cfg.grad_clip is not None:
            self.grad_clip_val = C(self.cfg.grad_clip, epoch, global_step)

        self.set_min_max_steps(
            min_step_percent=C(self.cfg.min_step_percent, epoch, global_step),
            max_step_percent=C(self.cfg.max_step_percent, epoch, global_step),
        )
