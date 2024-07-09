# coding: utf-8

"""
Pipeline of LivePortrait
"""

import cv2
import comfy.utils

import os.path as osp
import numpy as np
from .config.inference_config import InferenceConfig

from .utils.camera import get_rotation_matrix
from .utils.crop import _transform_img
from .live_portrait_wrapper import LivePortraitWrapper


def make_abs_path(fn):
    return osp.join(osp.dirname(osp.realpath(__file__)), fn)


class LivePortraitPipeline(object):
    def __init__(
        self,
        appearance_feature_extractor,
        motion_extractor,
        warping_module,
        spade_generator,
        stitching_retargeting_module,
        inference_cfg: InferenceConfig,
    ):
        self.live_portrait_wrapper: LivePortraitWrapper = LivePortraitWrapper(
            appearance_feature_extractor,
            motion_extractor,
            warping_module,
            spade_generator,
            stitching_retargeting_module,
            cfg=inference_cfg,
        )

    def _get_source_frame(self, source_np, idx, total_frames, method):
        if source_np.shape[0] == 1:
            return source_np[0]

        if method == "repeat":
            return source_np[min(idx, source_np.shape[0] - 1)]
        elif method == "cycle":
            return source_np[idx % source_np.shape[0]]
        elif method == "mirror":
            cycle_length = 2 * source_np.shape[0] - 2
            mirror_idx = idx % cycle_length
            if mirror_idx >= source_np.shape[0]:
                mirror_idx = cycle_length - mirror_idx
            return source_np[mirror_idx]
        elif method == "nearest":
            ratio = idx / (total_frames - 1)
            return source_np[
                min(int(ratio * (source_np.shape[0] - 1)), source_np.shape[0] - 1)
            ]

    def execute(self, source_np, driving_images_np, mismatch_method="repeat"):
        cfg = self.live_portrait_wrapper.cfg

        cropped_imgs = []
        full_imgs = []
        driving_lmk_lst = []

        rot_0, kp_0_info = None, None

        total_frames = driving_images_np.shape[0]

        pbar = comfy.utils.ProgressBar(total_frames)

        if cfg.flag_eye_retargeting or cfg.flag_lip_retargeting:
            driving_lmk_lst = self.cropper.get_retargeting_lmk_info(driving_images_np)

        for i in range(total_frames):
            source_frame_rgb = self._get_source_frame(
                source_np, i, total_frames, mismatch_method
            )
            driving_frame = driving_images_np[i]

            crop_info = self.cropper.crop_single_image(source_frame_rgb)
            source_lmk = crop_info["lmk_crop"]
            _, img_crop_256x256 = (
                crop_info["img_crop"],
                crop_info["img_crop_256x256"],
            )

            if cfg.flag_do_crop:
                I_s = self.live_portrait_wrapper.prepare_source(img_crop_256x256)
            else:
                I_s = self.live_portrait_wrapper.prepare_source(source_frame_rgb)

            kp_src_info = self.live_portrait_wrapper.get_kp_info(I_s)
            x_c_s = kp_src_info["kp"]
            R_s = get_rotation_matrix(
                kp_src_info["pitch"], kp_src_info["yaw"], kp_src_info["roll"]
            )
            f_s = self.live_portrait_wrapper.extract_feature_3d(I_s)
            kp_src = self.live_portrait_wrapper.transform_keypoint(kp_src_info)

            if cfg.flag_lip_zero:
                c_d_lip_before_animation = [0.0]
                combined_lip_ratio_tensor_before_animation = (
                    self.live_portrait_wrapper.calc_combined_lip_ratio(
                        c_d_lip_before_animation, source_lmk
                    )
                )
                # TODO: expose lip_zero_threshold
                if (
                    combined_lip_ratio_tensor_before_animation[0][0]
                    < cfg.lip_zero_threshold
                ):
                    cfg.flag_lip_zero = False
                else:
                    lip_delta_before_animation = (
                        self.live_portrait_wrapper.retarget_lip(
                            kp_src, combined_lip_ratio_tensor_before_animation
                        )
                    )

            # driving_frame_rgb = cv2.cvtColor(driving_frame, cv2.COLOR_BGR2RGB)
            driving_frame_256 = cv2.resize(driving_frame, (256, 256))
            I_d = self.live_portrait_wrapper.prepare_driving_videos(
                [driving_frame_256]
            )[0]

            if cfg.flag_eye_retargeting or cfg.flag_lip_retargeting:
                # driving_lmk_lst = self.cropper.get_retargeting_lmk_info([driving_frame])
                input_eye_ratio_lst, input_lip_ratio_lst = (
                    self.live_portrait_wrapper.calc_retargeting_ratio(
                        source_lmk, driving_lmk_lst
                    )
                )

            x_d_info = self.live_portrait_wrapper.get_kp_info(I_d)
            R_d = get_rotation_matrix(
                x_d_info["pitch"], x_d_info["yaw"], x_d_info["roll"]
            )

            if i == 0:
                rot_0 = R_d
                kp_0_info = x_d_info

            if cfg.flag_relative:
                R_new = (R_d @ rot_0.permute(0, 2, 1)) @ R_s
                delta_new = kp_src_info["exp"] + (x_d_info["exp"] - kp_0_info["exp"])
                scale_new = kp_src_info["scale"] * (
                    x_d_info["scale"] / kp_0_info["scale"]
                )
                t_new = kp_src_info["t"] + (x_d_info["t"] - kp_0_info["t"])
            else:
                R_new = R_d
                delta_new = x_d_info["exp"]
                scale_new = kp_src_info["scale"]
                t_new = x_d_info["t"]

            t_new[..., 2].fill_(0)  # zero tz
            x_d_i_new = scale_new * (x_c_s @ R_new + delta_new) + t_new
            if (
                not cfg.flag_stitching
                and not cfg.flag_eye_retargeting
                and not cfg.flag_lip_retargeting
            ):
                # without stitching or retargeting
                if cfg.flag_lip_zero:
                    x_d_i_new += lip_delta_before_animation.reshape(
                        -1, kp_src.shape[1], 3
                    )
                else:
                    pass
            elif (
                cfg.flag_stitching
                and not cfg.flag_eye_retargeting
                and not cfg.flag_lip_retargeting
            ):
                # with stitching and without retargeting
                if cfg.flag_lip_zero:
                    x_d_i_new = self.live_portrait_wrapper.stitching(
                        kp_src, x_d_i_new
                    ) + lip_delta_before_animation.reshape(-1, kp_src.shape[1], 3)
                else:
                    x_d_i_new = self.live_portrait_wrapper.stitching(kp_src, x_d_i_new)
            else:
                eyes_delta, lip_delta = None, None
                if cfg.flag_eye_retargeting:
                    c_d_eyes_i = input_eye_ratio_lst[i]
                    combined_eye_ratio_tensor = (
                        self.live_portrait_wrapper.calc_combined_eye_ratio(
                            c_d_eyes_i, source_lmk
                        )
                    )
                    combined_eye_ratio_tensor = (
                        combined_eye_ratio_tensor * cfg.eyes_retargeting_multiplier
                    )
                    # ∆_eyes,i = R_eyes(x_s; c_s,eyes, c_d,eyes,i)
                    eyes_delta = self.live_portrait_wrapper.retarget_eye(
                        kp_src, combined_eye_ratio_tensor
                    )
                if cfg.flag_lip_retargeting:
                    c_d_lip_i = input_lip_ratio_lst[i]
                    combined_lip_ratio_tensor = (
                        self.live_portrait_wrapper.calc_combined_lip_ratio(
                            c_d_lip_i, source_lmk
                        )
                    )
                    combined_lip_ratio_tensor = (
                        combined_lip_ratio_tensor * cfg.lip_retargeting_multiplier
                    )
                    # ∆_lip,i = R_lip(x_s; c_s,lip, c_d,lip,i)
                    lip_delta = self.live_portrait_wrapper.retarget_lip(
                        kp_src, combined_lip_ratio_tensor
                    )

                if cfg.flag_relative:  # use x_s
                    x_d_i_new = (
                        kp_src
                        + (
                            eyes_delta.reshape(-1, kp_src.shape[1], 3)
                            if eyes_delta is not None
                            else 0
                        )
                        + (
                            lip_delta.reshape(-1, kp_src.shape[1], 3)
                            if lip_delta is not None
                            else 0
                        )
                    )
                else:  # use x_d,i
                    x_d_i_new = (
                        x_d_i_new
                        + (
                            eyes_delta.reshape(-1, kp_src.shape[1], 3)
                            if eyes_delta is not None
                            else 0
                        )
                        + (
                            lip_delta.reshape(-1, kp_src.shape[1], 3)
                            if lip_delta is not None
                            else 0
                        )
                    )

                if cfg.flag_stitching:
                    x_d_i_new = self.live_portrait_wrapper.stitching(kp_src, x_d_i_new)

            out = self.live_portrait_wrapper.warp_decode(f_s, kp_src, x_d_i_new)

            if cfg.flag_stitching:
                x_d_i_new = self.live_portrait_wrapper.stitching(kp_src, x_d_i_new)

            out = self.live_portrait_wrapper.warp_decode(f_s, kp_src, x_d_i_new)
            I_p_i = self.live_portrait_wrapper.parse_output(out["out"])[0]
            cropped_imgs.append(I_p_i)

            # Transform and blend
            I_p_i_to_ori = _transform_img(
                I_p_i,
                crop_info["M_c2o"],
                dsize=(source_frame_rgb.shape[1], source_frame_rgb.shape[0]),
            )

            if cfg.flag_pasteback:
                if cfg.mask_crop is None:
                    cfg.mask_crop = cv2.imread(
                        make_abs_path("./utils/resources/mask_template.png"),
                        cv2.IMREAD_COLOR,
                    )
                mask_ori = _transform_img(
                    cfg.mask_crop,
                    crop_info["M_c2o"],
                    dsize=(source_frame_rgb.shape[1], source_frame_rgb.shape[0]),
                )
                mask_ori = mask_ori.astype(np.float32) / 255.0
                I_p_i_to_ori_blend = np.clip(
                    mask_ori * I_p_i_to_ori + (1 - mask_ori) * source_frame_rgb, 0, 255
                ).astype(np.uint8)
            else:
                I_p_i_to_ori_blend = I_p_i_to_ori

            full_imgs.append(I_p_i_to_ori_blend)
            pbar.update(1)

        return cropped_imgs, full_imgs
