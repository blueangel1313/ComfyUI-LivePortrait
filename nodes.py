import os
import torch
import yaml
import numpy as np
import folder_paths
import comfy.model_management as mm
import comfy.utils
import cv2
from pathlib import Path
import pickle

script_directory = os.path.dirname(os.path.abspath(__file__))


from .liveportrait.src.utils.camera import get_rotation_matrix
from .liveportrait.src.config.argument_config import ArgumentConfig
from .liveportrait.src.config.crop_config import CropConfig
from .liveportrait.src.config.inference_config import InferenceConfig

from .patch import LivePortraitPipeline, Cropper, apply_config
from .liveportrait.src.modules.spade_generator import SPADEDecoder
from .liveportrait.src.modules.warping_network import WarpingNetwork
from .liveportrait.src.modules.motion_extractor import MotionExtractor
from .liveportrait.src.modules.appearance_feature_extractor import (
    AppearanceFeatureExtractor,
)
from .liveportrait.src.modules.stitching_retargeting_network import (
    StitchingRetargetingNetwork,
)

# class CropConfig:
#     def __init__(self, dsize=512, scale=2.3, vx_ratio=0, vy_ratio=-0.125):
#         self.dsize = dsize
#         self.scale = scale
#         self.vx_ratio = vx_ratio
#         self.vy_ratio = vy_ratio


class ArgumentConfig:
    def __init__(
        self,
        device_id=0,
        flag_lip_zero=True,
        flag_eye_retargeting=False,
        flag_lip_retargeting=False,
        flag_stitching=True,
        flag_relative=True,
        flag_pasteback=True,
        flag_do_crop=True,
        flag_do_rot=True,
        dsize=512,
        scale=2.3,
        vx_ratio=0,
        vy_ratio=-0.125,
    ):
        self.device_id = device_id
        self.flag_lip_zero = flag_lip_zero
        self.flag_eye_retargeting = flag_eye_retargeting
        self.flag_lip_retargeting = flag_lip_retargeting
        self.flag_stitching = flag_stitching
        self.flag_relative = flag_relative
        self.flag_pasteback = flag_pasteback
        self.flag_do_crop = flag_do_crop
        self.flag_do_rot = flag_do_rot
        self.dsize = dsize
        self.scale = scale
        self.vx_ratio = vx_ratio
        self.vy_ratio = vy_ratio


class DownloadAndLoadLivePortraitModels:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
        }

    RETURN_TYPES = ("LIVEPORTRAITPIPE",)
    RETURN_NAMES = ("live_portrait_pipe",)
    FUNCTION = "loadmodel"
    CATEGORY = "LivePortrait"

    def loadmodel(self):
        device = mm.get_torch_device()
        mm.soft_empty_cache()

        pbar = comfy.utils.ProgressBar(3)

        download_path = os.path.join(folder_paths.models_dir, "liveportrait")
        model_path = os.path.join(download_path)

        if not os.path.exists(model_path):
            print(f"Downloading model to: {model_path}")
            from huggingface_hub import snapshot_download

            snapshot_download(
                repo_id="Kijai/LivePortrait_safetensors",
                local_dir=download_path,
                local_dir_use_symlinks=False,
            )

        model_config_path = os.path.join(
            script_directory, "liveportrait", "src", "config", "models.yaml"
        )
        with open(model_config_path, "r") as file:
            model_config = yaml.safe_load(file)

        feature_extractor_path = os.path.join(
            model_path, "appearance_feature_extractor.safetensors"
        )
        motion_extractor_path = os.path.join(model_path, "motion_extractor.safetensors")
        warping_module_path = os.path.join(model_path, "warping_module.safetensors")
        spade_generator_path = os.path.join(model_path, "spade_generator.safetensors")
        stitching_retargeting_path = os.path.join(
            model_path, "stitching_retargeting_module.safetensors"
        )

        # init F
        model_params = model_config["model_params"][
            "appearance_feature_extractor_params"
        ]
        self.appearance_feature_extractor = AppearanceFeatureExtractor(
            **model_params
        ).to(device)
        self.appearance_feature_extractor.load_state_dict(
            comfy.utils.load_torch_file(feature_extractor_path)
        )
        self.appearance_feature_extractor.eval()
        print("Load appearance_feature_extractor done.")
        pbar.update(1)
        # init M
        model_params = model_config["model_params"]["motion_extractor_params"]
        self.motion_extractor = MotionExtractor(**model_params).to(device)
        self.motion_extractor.load_state_dict(
            comfy.utils.load_torch_file(motion_extractor_path)
        )
        self.motion_extractor.eval()
        print("Load motion_extractor done.")
        pbar.update(1)
        # init W
        model_params = model_config["model_params"]["warping_module_params"]
        self.warping_module = WarpingNetwork(**model_params).to(device)
        self.warping_module.load_state_dict(
            comfy.utils.load_torch_file(warping_module_path)
        )
        self.warping_module.eval()
        print("Load warping_module done.")
        pbar.update(1)
        # init G
        model_params = model_config["model_params"]["spade_generator_params"]
        self.spade_generator = SPADEDecoder(**model_params).to(device)
        self.spade_generator.load_state_dict(
            comfy.utils.load_torch_file(spade_generator_path)
        )
        self.spade_generator.eval()
        print("Load spade_generator done.")
        pbar.update(1)

        def filter_checkpoint_for_model(checkpoint, prefix):
            """Filter and adjust the checkpoint dictionary for a specific model based on the prefix."""
            # Create a new dictionary where keys are adjusted by removing the prefix and the model name
            filtered_checkpoint = {
                key.replace(prefix + "_module.", ""): value
                for key, value in checkpoint.items()
                if key.startswith(prefix)
            }
            return filtered_checkpoint

        config = model_config["model_params"]["stitching_retargeting_module_params"]
        checkpoint = comfy.utils.load_torch_file(stitching_retargeting_path)

        stitcher_prefix = "retarget_shoulder"
        stitcher_checkpoint = filter_checkpoint_for_model(checkpoint, stitcher_prefix)
        stitcher = StitchingRetargetingNetwork(**config.get("stitching"))
        stitcher.load_state_dict(stitcher_checkpoint)
        stitcher = stitcher.to(device)
        stitcher.eval()

        lip_prefix = "retarget_mouth"
        lip_checkpoint = filter_checkpoint_for_model(checkpoint, lip_prefix)
        retargetor_lip = StitchingRetargetingNetwork(**config.get("lip"))
        retargetor_lip.load_state_dict(lip_checkpoint)
        retargetor_lip = retargetor_lip.to(device)
        retargetor_lip.eval()

        eye_prefix = "retarget_eye"
        eye_checkpoint = filter_checkpoint_for_model(checkpoint, eye_prefix)
        retargetor_eye = StitchingRetargetingNetwork(**config.get("eye"))
        retargetor_eye.load_state_dict(eye_checkpoint)
        retargetor_eye = retargetor_eye.to(device)
        retargetor_eye.eval()
        print("Load stitching_retargeting_module done.")

        self.stich_retargeting_module = {
            "stitching": stitcher,
            "lip": retargetor_lip,
            "eye": retargetor_eye,
        }
        inference_config = InferenceConfig()

        models = {
            "appearance_feature_extractor": self.appearance_feature_extractor,
            "motion_extractor": self.motion_extractor,
            "spade_generator": self.spade_generator,
            "warping_module": self.warping_module,
            "stitching_retargeting_module": self.stich_retargeting_module,
        }

        pipeline = LivePortraitPipeline(inference_config, CropConfig(), models)

        return (pipeline,)


class LoadMotionTemplate:
    @classmethod
    def INPUT_TYPES(cls):
        input_dir = Path(folder_paths.get_input_directory()) / "liveportrait"
        files = [x.stem for x in input_dir.glob("*.pkl")]
        return {
            "required": {
                "template": (sorted(files),),
            }
        }

    RETURN_TYPES = ("LIVEPORTRAIT_TEMPLATE",)
    RETURN_NAMES = ("template",)
    FUNCTION = "process"
    CATEGORY = "LivePortrait"

    def process(
        self,
        name: str,
    ):
        return (pickle_pth,)


class LivePortraitMotionTemplate:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "pipeline": ("LIVEPORTRAITPIPE",),
                "name": ("STRING", {"default": "MotionTemplate"}),
                "driving_images": ("IMAGE",),
                "dsize": ("INT", {"default": 512, "min": 64, "max": 2048}),
                "scale": ("FLOAT", {"default": 2.3, "min": 1.0, "max": 4.0}),
                "vx_ratio": (
                    "FLOAT",
                    {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.01},
                ),
                "vy_ratio": (
                    "FLOAT",
                    {"default": -0.125, "min": -1.0, "max": 1.0, "step": 0.01},
                ),
            },
        }

    RETURN_TYPES = ("LIVEPORTRAIT_TEMPLATE",)
    RETURN_NAMES = ("template",)
    FUNCTION = "process"
    CATEGORY = "LivePortrait"

    def process(
        self,
        name: str,
        pipeline: LivePortraitPipeline,
        driving_images: torch.Tensor,
        dsize: int,
        scale: float,
        vx_ratio: float,
        vy_ratio: float,
    ):
        driving_images_np = (driving_images * 255).byte().numpy()

        crop_cfg = CropConfig(
            dsize=dsize, scale=scale, vx_ratio=vx_ratio, vy_ratio=vy_ratio
        )
        # inference_cfg = InferenceConfig()
        # )  # use attribute of args to initial InferenceConfig

        # wrapper = LivePortraitWrapper(cfg=inference_cfg)
        cropper = Cropper(crop_cfg=crop_cfg)

        # wants BGR

        resized = [cv2.resize(im, (256, 256)) for im in driving_images_np]
        lmk = cropper.get_retargeting_lmk_info(resized)
        prepared = pipeline.live_portrait_wrapper.prepare_driving_videos(resized)

        count = prepared.shape[0]

        templates = []

        progress = comfy.utils.ProgressBar(count)

        for i in range(count):
            id = prepared[i]
            kp_info = pipeline.live_portrait_wrapper.get_kp_info(id)
            rot = get_rotation_matrix(kp_info["pitch"], kp_info["yaw"], kp_info["roll"])

            template_dct = {"n_frames": count, "frames_index": i}
            template_dct["scale"] = kp_info["scale"].cpu().numpy().astype(np.float32)
            template_dct["R_d"] = rot.cpu().numpy().astype(np.float32)
            template_dct["exp"] = kp_info["exp"].cpu().numpy().astype(np.float32)
            template_dct["t"] = kp_info["t"].cpu().numpy().astype(np.float32)
            progress.update(1)

            templates.append(template_dct)

        out_dir = Path(folder_paths.get_input_directory()) / "liveportrait"
        out_dir.mkdir(exist_ok=True, parents=True)
        res = out_dir / (name + ".pkl")
        with open(res, "wb") as f:
            pickle.dump([templates, lmk], f)

        return (res.as_poxix(),)


class LivePortraitProcess:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "pipeline": ("LIVEPORTRAITPIPE",),
                "source_image": ("IMAGE",),
                "dsize": ("INT", {"default": 512, "min": 64, "max": 2048}),
                "scale": (
                    "FLOAT",
                    {"default": 2.3, "min": 1.0, "max": 4.0, "step": 0.01},
                ),
                "vx_ratio": (
                    "FLOAT",
                    {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.01},
                ),
                "vy_ratio": (
                    "FLOAT",
                    {"default": -0.125, "min": -1.0, "max": 1.0, "step": 0.01},
                ),
                "eye_retargeting": ("BOOLEAN", {"default": False}),
                "eyes_retargeting_multiplier": (
                    "FLOAT",
                    {"default": 1.0, "min": 0.01, "max": 10.0, "step": 0.001},
                ),
                "lip_retargeting": ("BOOLEAN", {"default": False}),
                "lip_retargeting_multiplier": (
                    "FLOAT",
                    {"default": 1.0, "min": 0.01, "max": 10.0, "step": 0.001},
                ),
                "lip_zero": ("BOOLEAN", {"default": True}),
                "stitching": ("BOOLEAN", {"default": True}),
                "relative": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "driving_images": ("IMAGE",),
                "driving_template": ("LIVEPORTRAIT_TEMPLATE",),
            },
        }

    RETURN_TYPES = (
        "IMAGE",
        "IMAGE",
    )
    RETURN_NAMES = (
        "cropped_images",
        "full_images",
    )
    FUNCTION = "process"
    CATEGORY = "LivePortrait"

    def process(
        self,
        source_image,
        dsize,
        scale,
        vx_ratio,
        vy_ratio,
        pipeline: LivePortraitPipeline,
        lip_zero,
        eye_retargeting,
        lip_retargeting,
        stitching,
        relative,
        eyes_retargeting_multiplier,
        lip_retargeting_multiplier,
        driving_template=None,
        driving_images=None,
    ):
        source_image_np = (source_image * 255).byte().numpy()

        driving_images_np = (driving_images * 255).byte().numpy()

        crop_cfg = CropConfig(
            dsize=dsize,
            scale=scale,
            vx_ratio=vx_ratio,
            vy_ratio=vy_ratio,
        )

        cropper = Cropper(crop_cfg=crop_cfg)
        args = ArgumentConfig(
            dsize=dsize, scale=scale, vx_ratio=vx_ratio, vy_ratio=vy_ratio
        )
        pipeline.cropper = cropper

        apply_config(
            pipeline.live_portrait_wrapper.cfg,
            flag_eye_retargeting=eye_retargeting,
            eyes_retargeting_multiplier=eyes_retargeting_multiplier,
            flag_lip_retargeting=lip_retargeting,
            lip_retargeting_multiplier=lip_retargeting_multiplier,
            flag_stitching=stitching,
            flag_relative=relative,
            flag_lip_zero=lip_zero,
            flag_do_crop=True,
        )

        print(pipeline.live_portrait_wrapper.cfg)
        cropped_out_list = []
        full_out_list = []
        for img in source_image_np:
            cropped_frames, full_frame = pipeline.execute(img, driving_images_np)
            cropped_tensors = [
                torch.from_numpy(np_array) for np_array in cropped_frames
            ]
            cropped_tensors_out = torch.stack(cropped_tensors) / 255
            cropped_tensors_out = cropped_tensors_out.cpu().float()

            full_tensors = [torch.from_numpy(np_array) for np_array in full_frame]
            full_tensors_out = torch.stack(full_tensors) / 255
            full_tensors_out = full_tensors_out.cpu().float()

            cropped_out_list.append(cropped_tensors_out)
            full_out_list.append(full_tensors_out)

        cropped_tensors_out = torch.cat(cropped_out_list, dim=0)
        full_tensors_out = torch.cat(full_out_list, dim=0)

        return (cropped_tensors_out, full_tensors_out)


NODE_CLASS_MAPPINGS = {
    "DownloadAndLoadLivePortraitModels": DownloadAndLoadLivePortraitModels,
    "LivePortraitProcess": LivePortraitProcess,
    "LivePortraitMotionTemplate": LivePortraitMotionTemplate,
    "LoadMotionTemplate": LoadMotionTemplate,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DownloadAndLoadLivePortraitModels": "(Down)Load LivePortraitModels",
    "LivePortraitProcess": "LivePortraitProcess",
    "LivePortraitMotionTemplate": "LivePortraitMotionTemplate",
    "LoadMotionTemplate": "LoadMotionTemplate",
}
