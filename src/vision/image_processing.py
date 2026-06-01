"""Camera capture and PlantCV-based image processing helpers."""

from __future__ import annotations

import logging
import multiprocessing as mp
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def _capture_worker(path_img: str, wait_time: float, shared: dict[str, Any]) -> None:
    """Capture one image in a child process and report result in shared state."""
    try:
        picamera2_module = __import__("picamera2")
        picamera2_cls = getattr(picamera2_module, "Picamera2")
        preview_cls = getattr(picamera2_module, "Preview")
        camera = picamera2_cls()
        camera.start_preview(preview_cls.NULL)
        camera.start()
        time.sleep(wait_time)
        camera.capture_file(path_img)
        camera.stop_preview()
        camera.stop()
        camera.close()
        cv2 = __import__("cv2")
        img = cv2.imread(path_img)
        rotated = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        cv2.imwrite(path_img, rotated)
        shared["success"] = True
    except Exception as exc:  # noqa: BLE001
        shared["error"] = str(exc)


@dataclass(slots=True)
class VisionConfig:
    """Runtime settings for the vision pipeline."""

    channel: str = "k"
    kernel_size: int = 20
    sigma: float = 2.0
    skeleton_output: str = "data/images/skeleton.jpg"
    background_image: str = "data/images/background.jpg"
    pixel_to_cm_ratio: float = 1.0


class CameraService:
    """Picamera2 wrapper with process-based timeout protection."""

    def __init__(self) -> None:
        self._lib_available = False

    @property
    def is_ready(self) -> bool:
        """Check if camera library was successfully found."""
        return self._lib_available

    def setup(self) -> bool:
        """Verify Picamera2 library is available without locking the hardware."""
        try:
            __import__("picamera2")
            self._lib_available = True
            LOGGER.info("Camera service ready (hardware will be acquired on demand)")
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Camera library not found or unusable: %s", exc)
            self._lib_available = False
            return False

    def capture_file(
        self,
        output_path: str | Path,
        warmup_seconds: float = 7.0,
        timeout_seconds: float = 16.0,
        led: Any = None,
    ) -> str:
        """Capture one image file with a timeout guard."""
        output_path_str = str(output_path)
        try:
            Path(output_path_str).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Unable to create image directory for %s: %s", output_path_str, exc)
            return ""

        manager = mp.Manager()
        return_dict: dict[str, Any] = manager.dict()

        process = mp.Process(
            target=_capture_worker,
            args=(output_path_str, warmup_seconds, return_dict),
        )

        if led is not None:
            led.on()
        try:
            process.start()
            process.join(timeout_seconds + warmup_seconds)

            if process.is_alive():
                LOGGER.error("Camera capture timed out for %s", output_path_str)
                process.terminate()
                process.join()
                return ""
        finally:
            if led is not None:
                led.off()

        if return_dict.get("success"):
            return output_path_str

        LOGGER.error("Camera capture failed: %s", return_dict.get("error", "unknown error"))
        return ""


class PlantImageProcessor:
    """PlantCV-based segmentation and growth extraction pipeline."""

    def __init__(self, config: VisionConfig | None = None) -> None:
        self._config = config or VisionConfig()

    def _import_processing_stack(self) -> tuple[Any, Any, Any]:
        """Import PlantCV/NumPy/OpenCV only when needed."""
        pcv_module = __import__("plantcv", fromlist=["plantcv"]).plantcv
        np_module = __import__("numpy")
        cv2_module = __import__("cv2")
        return pcv_module, np_module, cv2_module

    def match_luminance(self, image_rgb: Any, background_rgb: Any, radius: int = 150) -> Any:
        """Match background luminance to the current image near the brightest zone."""
        _, np_module, cv2_module = self._import_processing_stack()

        image_yuv = cv2_module.cvtColor(image_rgb, cv2_module.COLOR_RGB2YCrCb)
        back_yuv = cv2_module.cvtColor(background_rgb, cv2_module.COLOR_RGB2YCrCb)

        image_y, _, _ = cv2_module.split(image_yuv)
        back_y, _, _ = cv2_module.split(back_yuv)

        max_loc = np_module.unravel_index(np_module.argmax(image_y), image_y.shape)
        y, x = max_loc

        y1, y2 = max(0, y - radius), min(image_y.shape[0], y + radius + 1)
        x1, x2 = max(0, x - radius), min(image_y.shape[1], x + radius + 1)

        mean_image = image_y[y1:y2, x1:x2].mean()
        mean_back = back_y[y1:y2, x1:x2].mean()

        back_y_norm = back_y + (mean_image - mean_back)
        back_y_norm = np_module.clip(back_y_norm, 0, 255).astype("uint8")

        back_yuv[:, :, 0] = back_y_norm
        return cv2_module.cvtColor(back_yuv, cv2_module.COLOR_YCrCb2RGB)

    def remove_shadows(
        self,
        image_rgb: Any,
        beta1: float = 0.38,
        beta2: float = 0.85,
        tau_s: float = 0.27,
        tau_h: float = 1.0,
    ) -> Any:
        """Detect and compensate shadows with HSV constraints."""
        _, np_module, cv2_module = self._import_processing_stack()

        image_hsv = cv2_module.cvtColor(image_rgb, cv2_module.COLOR_RGB2HSV).astype("float32")
        background = cv2_module.imread(self._config.background_image)
        if background is None:
            LOGGER.warning("Background image not found at %s, skipping shadow removal", self._config.background_image)
            return image_rgb

        background = self.match_luminance(image_rgb, background)
        background = cv2_module.cvtColor(background, cv2_module.COLOR_BGR2RGB)
        back_hsv = cv2_module.cvtColor(background, cv2_module.COLOR_RGB2HSV).astype("float32")

        hue_f, sat_f, val_f = cv2_module.split(image_hsv)
        hue_b, sat_b, val_b = cv2_module.split(back_hsv)
        val_b[val_b == 0] = 1e-6

        val_ratio = val_f / val_b
        sat_diff = np_module.abs(sat_f - sat_b)
        hue_diff = np_module.abs(hue_f - hue_b)
        hue_diff = np_module.minimum(hue_diff, 180 - hue_diff)

        shadow_mask = (
            (val_ratio >= beta1)
            & (val_ratio <= beta2)
            & (sat_diff <= tau_s * 255)
            & (hue_diff <= tau_h * 255)
        )

        shadow_mask_u8 = shadow_mask.astype("uint8") * 255
        shadow_mask_blurred = cv2_module.GaussianBlur(shadow_mask_u8, (5, 71), 0)
        _, shadow_mask_clean = cv2_module.threshold(shadow_mask_blurred, 127, 255, cv2_module.THRESH_BINARY)
        shadow_mask = shadow_mask_clean.astype(bool)

        if np_module.count_nonzero(shadow_mask) <= 20000:
            return image_rgb.copy()

        image_no_shadow = image_rgb.copy()
        image_no_shadow[shadow_mask] = background[shadow_mask]

        eroded = cv2_module.erode(shadow_mask_u8, np_module.ones((60, 60), "uint8"), iterations=1)
        dilated = cv2_module.dilate(shadow_mask_u8, np_module.ones((7, 7), "uint8"), iterations=1)
        transition = cv2_module.subtract(dilated, eroded)

        blurred = cv2_module.GaussianBlur(image_no_shadow, (5, 91), 0)
        image_no_shadow[transition.astype(bool)] = blurred[transition.astype(bool)]
        return cv2_module.bilateralFilter(image_no_shadow, d=15, sigmaColor=35, sigmaSpace=95)

    def get_segment_list(
        self,
        image_path: str | Path,
        channel: str | None = None,
        kernel_size: int | None = None,
        sigma: float | None = None,
        skeleton_filename: str | None = None,
    ) -> list[float] | None:
        """Extract skeleton segment lengths from one image."""
        pcv_module, np_module, cv2_module = self._import_processing_stack()
        selected_channel = channel or self._config.channel
        selected_kernel = kernel_size or self._config.kernel_size
        selected_sigma = sigma or self._config.sigma

        image, _, _ = pcv_module.readimage(str(image_path))
        image_no_shadow = self.remove_shadows(image)

        height, width = image_no_shadow.shape[0], image_no_shadow.shape[1]
        channel_img = pcv_module.rgb2gray_cmyk(rgb_img=image_no_shadow, channel=selected_channel)
        edges = pcv_module.canny_edge_detect(channel_img, sigma=selected_sigma)

        edges_crop = pcv_module.crop(edges, 5, 5, height - 10, width - 10)

        kernel = np_module.ones((selected_kernel, selected_kernel), "uint8")
        closing = cv2_module.morphologyEx(edges_crop, cv2_module.MORPH_CLOSE, kernel)

        thresh = cv2_module.threshold(closing, 128, 255, cv2_module.THRESH_BINARY)[1]
        contours = cv2_module.findContours(thresh, cv2_module.RETR_EXTERNAL, cv2_module.CHAIN_APPROX_SIMPLE)[0]
        if len(contours) == 0:
            return None

        largest = max(contours, key=cv2_module.contourArea)
        result = np_module.zeros_like(closing)
        cv2_module.drawContours(result, [largest], 0, (255, 255, 255), cv2_module.FILLED)

        pcv_module.params.line_thickness = 3
        skeleton = pcv_module.morphology.skeletonize(mask=result)
        skeleton, segmented_img, objects = pcv_module.morphology.prune(skel_img=skeleton, size=20)

        if skeleton_filename is None:
            skeleton_path = self._config.skeleton_output
        else:
            base_dir = Path(self._config.skeleton_output).parent
            skeleton_path = str(base_dir / skeleton_filename)

        Path(skeleton_path).parent.mkdir(parents=True, exist_ok=True)
        cv2_module.imwrite(skeleton_path, skeleton)

        _ = pcv_module.morphology.segment_path_length(
            segmented_img=segmented_img,
            objects=objects,
            label="default",
        )
        return list(pcv_module.outputs.observations["default"]["segment_path_length"]["value"])

    def get_total_length(
        self,
        image_path: str | Path,
        channel: str | None = None,
        kernel_size: int | None = None,
        sigma: float | None = None,
    ) -> float:
        """Return the total skeleton length in pixels."""
        segments = self.get_segment_list(
            image_path=image_path,
            channel=channel,
            kernel_size=kernel_size,
            sigma=sigma,
        )
        if not segments:
            raise KeyError("No plant segment detected in image")
        return float(sum(segments))

    def capture_and_process(
        self,
        camera_service: CameraService,
        output_dir: str | Path,
        warmup_seconds: float = 7.0,
        timeout_seconds: float = 16.0,
        led: Any = None,
    ) -> dict[str, Any]:
        """Capture a new image, then compute growth from it."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
        out_path = Path(output_dir) / f"{timestamp}.jpg"

        captured_path = camera_service.capture_file(
            output_path=out_path,
            warmup_seconds=warmup_seconds,
            timeout_seconds=timeout_seconds,
            led=led,
        )
        if captured_path == "":
            return {
                "timestamp": datetime.now(UTC).isoformat(),
                "status": "error",
                "error": "camera_capture_failed",
                "image_path": "",
                "growth": None,
            }

        try:
            growth_px = self.get_total_length(captured_path)
            growth_cm = growth_px * self._config.pixel_to_cm_ratio
            return {
                "timestamp": datetime.now(UTC).isoformat(),
                "status": "ok",
                "image_path": captured_path,
                "growth_px": growth_px,
                "growth": growth_cm,
            }
        except Exception as exc:  # noqa: BLE001 - failures depend on image/dependency state
            LOGGER.exception("Image processing failed for %s: %s", captured_path, exc)
            return {
                "timestamp": datetime.now(UTC).isoformat(),
                "status": "error",
                "error": str(exc),
                "image_path": captured_path,
                "growth": None,
            }
