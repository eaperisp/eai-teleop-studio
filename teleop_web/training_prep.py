"""Training preparation helpers for the teleop web console.

The data platform prepares datasets, training-set manifests, and training
environment handoff packages. Heavy model training runs outside the collection
server.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any


class TrainingPrepError(ValueError):
    pass


TRAINING_SET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_XLA_FLAGS = "--xla_gpu_enable_triton_gemm=false --xla_gpu_autotune_level=0"


def _int_value(raw: Any, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _float_text(raw: Any, default: str) -> str:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return str(value)


def _bool_value(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _safe_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def training_data_root(data_dir: Path) -> Path:
    return data_dir / "datasets" / "training"


def _task_key(task: dict[str, Any]) -> str:
    return str(task.get("id") or task.get("name") or "")


class TrainingPrepManager:
    def __init__(
        self,
        data_dir: Path,
        *,
        dataset_root: Path,
        lerobot_home: Path,
        openpi_assets_dir: Path,
    ) -> None:
        self.update_paths(
            data_dir,
            dataset_root=dataset_root,
            lerobot_home=lerobot_home,
            openpi_assets_dir=openpi_assets_dir,
        )

    def update_paths(
        self,
        data_dir: Path,
        *,
        dataset_root: Path,
        lerobot_home: Path,
        openpi_assets_dir: Path,
    ) -> None:
        self.data_dir = data_dir
        self.dataset_root = dataset_root
        self.lerobot_home = lerobot_home
        self.openpi_assets_dir = openpi_assets_dir
        self.training_root = training_data_root(data_dir)
        legacy_root = data_dir / "training"
        if not self.training_root.exists() and legacy_root.is_dir():
            self.training_root.parent.mkdir(parents=True, exist_ok=True)
            for child in legacy_root.iterdir():
                target = self.training_root / child.name
                if target.exists():
                    continue
                if child.is_dir():
                    import shutil
                    shutil.copytree(child, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(child.read_bytes())
        self.training_root.mkdir(parents=True, exist_ok=True)
        self.training_sets_file = self.training_root / "training_sets.json"
        self.packages_file = self.training_root / "training_packages.json"
        self.package_root = self.training_root / "packages"
        self.package_root.mkdir(parents=True, exist_ok=True)

    def _load_sets(self) -> list[dict[str, Any]]:
        payload = _read_json(self.training_sets_file, {"training_sets": []})
        items = payload.get("training_sets") if isinstance(payload, dict) else []
        return items if isinstance(items, list) else []

    def _save_sets(self, items: list[dict[str, Any]]) -> None:
        _atomic_json(self.training_sets_file, {"training_sets": items})

    def _load_packages(self) -> list[dict[str, Any]]:
        payload = _read_json(self.packages_file, {"packages": []})
        items = payload.get("packages") if isinstance(payload, dict) else []
        return items if isinstance(items, list) else []

    def _save_packages(self, items: list[dict[str, Any]]) -> None:
        _atomic_json(self.packages_file, {"packages": items})

    def find_training_set(self, set_id: str) -> dict[str, Any] | None:
        for item in self._load_sets():
            if str(item.get("id") or "") == str(set_id or ""):
                return self._refresh_training_set(item)
        return None

    def mark_normalize_started(self, set_id: str, *, job_id: str, command: str, started_at: str) -> None:
        items = self._load_sets()
        for index, item in enumerate(items):
            if str(item.get("id") or "") != str(set_id or ""):
                continue
            updated = dict(item)
            updated["normalize_started_at"] = started_at
            updated["normalize_exit_code"] = None
            updated.pop("normalize_failed_at", None)
            updated["last_normalize_record"] = {
                "kind": "normalize",
                "job_id": job_id,
                "status": "running",
                "started_at": started_at,
                "finished_at": None,
                "exit_code": None,
                "command": command,
                "progress": {},
                "error": None,
            }
            updated["updated_at"] = _now()
            items[index] = updated
            self._save_sets(items)
            return

    def mark_normalize_finished(self, set_id: str, exit_code: int, *, job: dict[str, Any] | None = None) -> None:
        items = self._load_sets()
        for index, item in enumerate(items):
            if str(item.get("id") or "") != str(set_id or ""):
                continue
            updated = dict(item)
            now = _now()
            raw_logs = (job or {}).get("logs") or []
            logs = list(raw_logs)[-80:] if isinstance(raw_logs, list) else list(raw_logs)[-80:]
            progress = dict((job or {}).get("progress") or {})
            if exit_code == 0:
                progress["percent"] = 100
            error = None
            if exit_code != 0:
                for line in reversed(logs):
                    text = str(line).strip()
                    if text:
                        error = text
                        break
                error = error or f"归一化任务退出码 {exit_code}"
            updated["normalize_exit_code"] = exit_code
            if exit_code == 0:
                updated["normalized_at"] = now
                updated.pop("normalize_failed_at", None)
                updated.pop("norm_stats_stale", None)
            else:
                updated["normalize_failed_at"] = now
            updated["last_normalize_record"] = {
                "kind": "normalize",
                "job_id": (job or {}).get("id"),
                "status": "completed" if exit_code == 0 else "failed",
                "started_at": (job or {}).get("started_at") or updated.get("normalize_started_at"),
                "finished_at": now,
                "exit_code": exit_code,
                "command": (job or {}).get("command"),
                "progress": progress,
                "error": error,
            }
            updated["updated_at"] = now
            items[index] = updated
            self._save_sets(items)
            return

    def _lerobot_metadata(self, repo_id: str) -> dict[str, Any]:
        root = self.lerobot_home / repo_id
        metadata = _read_json(root / "metadata.json", {})
        info = _read_json(root / "meta" / "info.json", {})
        features = info.get("features") if isinstance(info, dict) else {}
        if not isinstance(features, dict):
            features = {}

        def feature_dim(*names: str) -> int | None:
            for name in names:
                shape = ((features.get(name) or {}).get("shape") or [])
                if isinstance(shape, list) and shape:
                    try:
                        return int(shape[-1])
                    except (TypeError, ValueError):
                        pass
            return None

        image_keys = metadata.get("image_keys")
        if not isinstance(image_keys, list):
            image_keys = [
                key
                for key, spec in features.items()
                if isinstance(spec, dict) and spec.get("dtype") in {"image", "video"}
            ]
        return {
            "metadata_ready": bool(metadata),
            "info_ready": bool(info),
            "total_episodes": int((info or {}).get("total_episodes") or 0),
            "total_frames": int((info or {}).get("total_frames") or 0),
            "state_dim": metadata.get("state_dim") or feature_dim("observation.state", "state"),
            "action_dim": metadata.get("action_dim") or feature_dim("action", "actions"),
            "image_keys": image_keys,
            "source_image_shape": metadata.get("source_image_shape") or [],
            "output_image_shape": metadata.get("output_image_shape") or [],
            "camera_plan": metadata.get("camera_plan") or {},
            "camera_preset": metadata.get("camera_preset") or "",
            "vector_dims": metadata.get("vector_dims") or [],
            "vector_layout": metadata.get("vector_layout") or [],
            "end_effector_types": metadata.get("end_effector_types") or {},
            "motor_action_indices": metadata.get("motor_action_indices") or [],
            "state_defaults": metadata.get("state_defaults") or [],
            "image_size": metadata.get("image_size") or "",
            "repo_path": str(root),
        }

    def profile_from_task(self, task: dict[str, Any]) -> dict[str, Any]:
        status = task.get("postprocess_status") or {}
        repo_id = status.get("repo_id") or f"local/{task.get('name')}"
        metadata = self._lerobot_metadata(str(repo_id))
        raw_episodes = int(task.get("existing_episodes") or task.get("completed_episodes") or 0)
        lerobot_episodes = int(status.get("lerobot_episodes") or metadata.get("total_episodes") or 0)
        if "lerobot_ready" in status:
            lerobot_ready = bool(status.get("lerobot_ready"))
        else:
            lerobot_ready = bool(metadata.get("info_ready") and raw_episodes > 0 and lerobot_episodes >= raw_episodes)
        norm_ready = bool(status.get("norm_stats_ready"))
        control_signature = {
            "state_dim": metadata.get("state_dim"),
            "action_dim": metadata.get("action_dim"),
            "vector_dims": metadata.get("vector_dims"),
        }
        camera_signature = {
            "image_keys": metadata.get("image_keys") or [],
            "output_image_shape": metadata.get("output_image_shape") or [],
            "camera_plan": metadata.get("camera_plan") or {},
            "image_size": metadata.get("image_size") or "",
        }
        warnings: list[str] = []
        if not lerobot_ready:
            warnings.append("尚未完成 LeRobot 转换")
        if not norm_ready:
            warnings.append("单任务归一化未完成；多任务会重新计算训练集归一化")
        if raw_episodes <= 0:
            warnings.append("原始 episode 为空")
        return {
            "task_id": task.get("id"),
            "task_name": task.get("name"),
            "instruction": task.get("instruction") or task.get("instruction_en") or "",
            "description": task.get("description") or task.get("description_zh") or "",
            "repo_id": repo_id,
            "raw_dataset_dir": status.get("robot_dir") or str(self.dataset_root / str(task.get("name"))),
            "lerobot_dir": status.get("lerobot_dir") or metadata["repo_path"],
            "norm_stats_dir": status.get("norm_stats_dir") or "",
            "raw_episodes": raw_episodes,
            "target_episodes": int(task.get("target_episodes") or 0),
            "lerobot_episodes": lerobot_episodes,
            "lerobot_frames": int(status.get("lerobot_frames") or metadata.get("total_frames") or 0),
            "lerobot_ready": lerobot_ready,
            "norm_stats_ready": norm_ready,
            "metadata": metadata,
            "control_signature": control_signature,
            "camera_signature": camera_signature,
            "dataset_ready": bool(lerobot_ready and raw_episodes > 0),
            "ready_for_training": bool(lerobot_ready and raw_episodes > 0),
            "warnings": warnings,
        }

    def profiles(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.profile_from_task(task) for task in tasks]

    @staticmethod
    def compatibility(profiles: list[dict[str, Any]]) -> dict[str, Any]:
        if not profiles:
            return {
                "status": "pending",
                "compatible": None,
                "issues": [],
                "message": "尚未加入任务，暂不执行兼容性校验",
                "task_count": 0,
            }
        issues: list[str] = []
        not_ready = [p["task_name"] for p in profiles if not p.get("dataset_ready")]
        if not_ready:
            issues.append(f"以下任务尚未完成 LeRobot 转换：{', '.join(map(str, not_ready))}")
        base = profiles[0]
        for profile in profiles[1:]:
            if profile.get("control_signature") != base.get("control_signature"):
                issues.append(f"{profile.get('task_name')} 的动作/状态维度与 {base.get('task_name')} 不一致")
            if profile.get("camera_signature") != base.get("camera_signature"):
                issues.append(f"{profile.get('task_name')} 的相机 schema 与 {base.get('task_name')} 不一致")
        return {
            "status": "compatible" if not issues else "incompatible",
            "compatible": not issues,
            "issues": issues,
            "task_count": len(profiles),
        }

    def _training_norm_dir(self, training_set: dict[str, Any]) -> Path:
        repo_ids = [str(task.get("repo_id") or "") for task in training_set.get("tasks") or []]
        if len(repo_ids) == 1:
            repo_part = repo_ids[0]
        else:
            repo_part = f"training_sets/{training_set.get('name')}"
        return self.openpi_assets_dir / str(training_set.get("config_name")) / repo_part

    def _norm_status(self, training_set: dict[str, Any]) -> dict[str, Any]:
        norm_dir = Path(str(training_set.get("norm_stats_dir") or self._training_norm_dir(training_set)))
        if not training_set.get("tasks"):
            return {
                "mode": "pending_tasks",
                "ready": False,
                "dir": str(norm_dir),
                "stats_file": str(norm_dir / "norm_stats.json"),
                "required": False,
                "message": "加入任务后再计算训练集归一化",
            }
        stale = bool(training_set.get("norm_stats_stale"))
        ready = (norm_dir / "norm_stats.json").is_file() and not stale
        mode = "reuse_single_task" if len(training_set.get("tasks") or []) == 1 else "training_set"
        return {
            "mode": mode,
            "ready": ready,
            "stale": stale,
            "dir": str(norm_dir),
            "stats_file": str(norm_dir / "norm_stats.json"),
            "required": True,
            "message": "已完成训练集归一化" if ready else "需要按训练集重新计算归一化",
        }

    def _refresh_training_set(self, training_set: dict[str, Any]) -> dict[str, Any]:
        refreshed = dict(training_set)
        if not refreshed.get("tasks"):
            refreshed["mode"] = "empty"
            refreshed["compatibility"] = self.compatibility([])
        refreshed["norm_stats"] = self._norm_status(refreshed)
        refreshed["package_ready"] = bool(
            (refreshed.get("compatibility") or {}).get("compatible")
            and (refreshed.get("norm_stats") or {}).get("ready")
        )
        return refreshed

    def create_training_set(self, raw: Any, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TrainingPrepError("训练集参数格式不正确")
        name = str(raw.get("name") or "").strip()
        if not TRAINING_SET_NAME_RE.match(name):
            raise TrainingPrepError("训练集名称仅支持字母、数字、下划线、连字符和点")
        task_ids = raw.get("task_ids") or []
        if not isinstance(task_ids, list):
            raise TrainingPrepError("任务列表格式不正确")
        selected_keys = {str(item) for item in task_ids}
        profiles = [
            self.profile_from_task(task)
            for task in tasks
            if _task_key(task) in selected_keys or str(task.get("name")) in selected_keys
        ]
        if len(profiles) != len(selected_keys):
            raise TrainingPrepError("部分任务不存在，请刷新后重试")
        config_name = str(raw.get("config_name") or f"pi05_{name}").strip()
        if not CONFIG_NAME_RE.match(config_name):
            raise TrainingPrepError("OpenPI config-name 格式不正确")
        dataset_action_dim = self._profiles_action_dim(profiles)
        training_set = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "mode": "empty" if not profiles else ("single_task" if len(profiles) == 1 else "multi_task"),
            "config_name": config_name,
            "created_at": _now(),
            "updated_at": _now(),
            "description": str(raw.get("description") or "").strip(),
            "h200_dataset_root": str(raw.get("h200_dataset_root") or "/home/ubuntu/datasets/lerobot"),
            "h200_assets_root": str(raw.get("h200_assets_root") or "/home/ubuntu/assets"),
            "openpi_data_home": str(raw.get("openpi_data_home") or "/home/ubuntu/models/openpi"),
            "hf_home": str(raw.get("hf_home") or "/home/ubuntu/models/openpi/huggingface"),
            "assets_base_dir": str(raw.get("assets_base_dir") or raw.get("h200_assets_root") or "/home/ubuntu/assets"),
            "checkpoint_base_dir": str(raw.get("checkpoint_base_dir") or "/home/ubuntu/models/openpi/checkpoints"),
            "action_dim": _int_value(raw.get("action_dim"), 32),
            "real_action_dim": _int_value(
                dataset_action_dim if dataset_action_dim is not None else raw.get("real_action_dim"),
                (profiles[0].get("metadata", {}).get("action_dim") if profiles else None) or 14,
            ),
            "action_horizon": _int_value(raw.get("action_horizon"), 16),
            "exp_name": str(raw.get("exp_name") or f"{name}_{config_name}_50k").strip(),
            "fsdp_devices": _int_value(raw.get("fsdp_devices"), 2),
            "batch_size": _int_value(raw.get("batch_size"), 32),
            "num_train_steps": _int_value(raw.get("num_train_steps"), 50000),
            "save_interval": _int_value(raw.get("save_interval"), 5000),
            "keep_period": _int_value(raw.get("keep_period"), 25000),
            "xla_mem_fraction": _float_text(raw.get("xla_mem_fraction"), "0.95"),
            "xla_flags": str(raw.get("xla_flags") or DEFAULT_XLA_FLAGS),
            "uv_no_sync": _bool_value(raw.get("uv_no_sync"), True),
            "wandb_enabled": _bool_value(raw.get("wandb_enabled"), False),
            "overwrite": _bool_value(raw.get("overwrite"), True),
            "tasks": [
                {
                    "task_id": p.get("task_id"),
                    "task_name": p.get("task_name"),
                    "repo_id": p.get("repo_id"),
                    "instruction": p.get("instruction"),
                    "lerobot_dir": p.get("lerobot_dir"),
                    "task_norm_stats_dir": p.get("norm_stats_dir"),
                    "raw_episodes": p.get("raw_episodes"),
                    "lerobot_episodes": p.get("lerobot_episodes"),
                    "lerobot_frames": p.get("lerobot_frames"),
                }
                for p in profiles
            ],
            "compatibility": self.compatibility(profiles),
            "profiles": profiles,
        }
        training_set["norm_stats_dir"] = str(self._training_norm_dir(training_set))
        training_set = self._refresh_training_set(training_set)
        items = [item for item in self._load_sets() if item.get("name") != name]
        items.append(training_set)
        self._save_sets(items)
        return training_set

    def update_training_set(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TrainingPrepError("训练集参数格式不正确")
        set_id = str(raw.get("training_set_id") or "").strip()
        if not set_id:
            raise TrainingPrepError("训练集 ID 不能为空")
        items = self._load_sets()
        index = next((idx for idx, item in enumerate(items) if item.get("id") == set_id), None)
        if index is None:
            raise TrainingPrepError("训练集不存在，请刷新后重试")
        current = dict(items[index])
        name = str(raw.get("name") or current.get("name") or "").strip()
        if not TRAINING_SET_NAME_RE.match(name):
            raise TrainingPrepError("训练集名称仅支持字母、数字、下划线、连字符和点")
        duplicate = next(
            (
                item for item in items
                if item.get("id") != set_id and str(item.get("name") or "") == name
            ),
            None,
        )
        if duplicate is not None:
            raise TrainingPrepError("训练集名称已存在，请换一个名称")
        config_name = str(raw.get("config_name") or current.get("config_name") or f"pi05_{name}").strip()
        if not CONFIG_NAME_RE.match(config_name):
            raise TrainingPrepError("OpenPI config-name 格式不正确")

        before_norm_key = (
            current.get("name"),
            current.get("config_name"),
            current.get("action_dim"),
            current.get("real_action_dim"),
            current.get("action_horizon"),
        )
        current.update({
            "name": name,
            "config_name": config_name,
            "description": str(raw.get("description") or "").strip(),
            "h200_dataset_root": str(raw.get("h200_dataset_root") or current.get("h200_dataset_root") or "/home/ubuntu/datasets/lerobot"),
            "h200_assets_root": str(raw.get("h200_assets_root") or current.get("h200_assets_root") or "/home/ubuntu/assets"),
            "assets_base_dir": str(raw.get("assets_base_dir") or raw.get("h200_assets_root") or current.get("assets_base_dir") or current.get("h200_assets_root") or "/home/ubuntu/assets"),
            "action_dim": _int_value(raw.get("action_dim"), _safe_int(current.get("action_dim"), 32)),
            "real_action_dim": _int_value(raw.get("real_action_dim"), _safe_int(current.get("real_action_dim"), 14)),
            "action_horizon": _int_value(raw.get("action_horizon"), _safe_int(current.get("action_horizon"), 16)),
            "updated_at": _now(),
        })
        dataset_action_dim = self._profiles_action_dim(current.get("profiles") or [])
        if dataset_action_dim is not None:
            current["real_action_dim"] = dataset_action_dim
        current["norm_stats_dir"] = str(self._training_norm_dir(current))
        after_norm_key = (
            current.get("name"),
            current.get("config_name"),
            current.get("action_dim"),
            current.get("real_action_dim"),
            current.get("action_horizon"),
        )
        if after_norm_key != before_norm_key:
            current["norm_stats_stale"] = True
            current.pop("normalized_at", None)
            current.pop("normalize_failed_at", None)
            current["normalize_exit_code"] = None
            current.pop("last_normalize_record", None)
        current = self._refresh_training_set(current)
        items[index] = current
        self._save_sets(items)
        return current

    @staticmethod
    def _task_entry(profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": profile.get("task_id"),
            "task_name": profile.get("task_name"),
            "repo_id": profile.get("repo_id"),
            "instruction": profile.get("instruction"),
            "lerobot_dir": profile.get("lerobot_dir"),
            "task_norm_stats_dir": profile.get("norm_stats_dir"),
            "raw_episodes": profile.get("raw_episodes"),
            "lerobot_episodes": profile.get("lerobot_episodes"),
            "lerobot_frames": profile.get("lerobot_frames"),
        }

    @staticmethod
    def _profiles_action_dim(profiles: list[dict[str, Any]]) -> int | None:
        dims = {
            int(profile.get("metadata", {}).get("action_dim"))
            for profile in profiles
            if profile.get("metadata", {}).get("action_dim")
        }
        return next(iter(dims)) if len(dims) == 1 else None

    def _update_training_set_from_profiles(self, training_set: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any]:
        updated = dict(training_set)
        previous_task_ids = [str(task.get("task_id")) for task in updated.get("tasks") or []]
        next_task_ids = [str(profile.get("task_id")) for profile in profiles]
        updated["mode"] = "empty" if not profiles else ("single_task" if len(profiles) == 1 else "multi_task")
        updated["tasks"] = [self._task_entry(profile) for profile in profiles]
        updated["profiles"] = profiles
        updated["compatibility"] = self.compatibility(profiles)
        dataset_action_dim = self._profiles_action_dim(profiles)
        if dataset_action_dim is not None:
            updated["real_action_dim"] = dataset_action_dim
        updated["norm_stats_dir"] = str(self._training_norm_dir(updated))
        if previous_task_ids != next_task_ids:
            updated["norm_stats_stale"] = bool(next_task_ids)
            updated.pop("normalized_at", None)
            updated.pop("normalize_failed_at", None)
            updated["normalize_exit_code"] = None
            updated.pop("last_normalize_record", None)
        updated["updated_at"] = _now()
        return self._refresh_training_set(updated)

    def add_tasks_to_training_set(self, raw: Any, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TrainingPrepError("加入训练集参数格式不正确")
        set_id = str(raw.get("training_set_id") or "").strip()
        task_ids = raw.get("task_ids") or []
        if not set_id:
            raise TrainingPrepError("请选择目标训练集")
        if not isinstance(task_ids, list) or not task_ids:
            raise TrainingPrepError("请至少选择一个任务")
        items = self._load_sets()
        index = next((idx for idx, item in enumerate(items) if item.get("id") == set_id), None)
        if index is None:
            raise TrainingPrepError("训练集不存在，请刷新后重试")
        selected_keys = {str(item) for item in task_ids}
        all_profiles = self.profiles(tasks)
        profile_by_key: dict[str, dict[str, Any]] = {}
        for profile in all_profiles:
            profile_by_key[str(profile.get("task_id"))] = profile
            profile_by_key[str(profile.get("task_name"))] = profile
        selected_profiles = [profile_by_key[key] for key in selected_keys if key in profile_by_key]
        if len(selected_profiles) != len(selected_keys):
            raise TrainingPrepError("部分任务不存在，请刷新后重试")
        existing_task_ids = {str(task.get("task_id")) for task in items[index].get("tasks") or []}
        merged_profiles = [
            profile
            for profile in all_profiles
            if str(profile.get("task_id")) in existing_task_ids or str(profile.get("task_id")) in selected_keys
        ]
        items[index] = self._update_training_set_from_profiles(items[index], merged_profiles)
        self._save_sets(items)
        return items[index]

    def remove_task_from_training_set(self, raw: Any, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TrainingPrepError("移除训练任务参数格式不正确")
        set_id = str(raw.get("training_set_id") or "").strip()
        task_id = str(raw.get("task_id") or "").strip()
        if not set_id or not task_id:
            raise TrainingPrepError("训练集和任务不能为空")
        items = self._load_sets()
        index = next((idx for idx, item in enumerate(items) if item.get("id") == set_id), None)
        if index is None:
            raise TrainingPrepError("训练集不存在，请刷新后重试")
        keep_task_ids = {str(task.get("task_id")) for task in items[index].get("tasks") or [] if str(task.get("task_id")) != task_id}
        profiles = [profile for profile in self.profiles(tasks) if str(profile.get("task_id")) in keep_task_ids]
        items[index] = self._update_training_set_from_profiles(items[index], profiles)
        self._save_sets(items)
        return items[index]

    def package_training_set(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise TrainingPrepError("交接包参数格式不正确")
        set_id = str(raw.get("training_set_id") or "").strip()
        training_set = next((item for item in self._load_sets() if item.get("id") == set_id), None)
        if training_set is None:
            raise TrainingPrepError("训练集不存在，请先创建训练集")
        training_set = self._refresh_training_set(training_set)
        if not training_set.get("tasks"):
            raise TrainingPrepError("训练集尚未加入任务，请先在数据处理页选择任务并加入训练集")
        compatibility = training_set.get("compatibility") or {}
        if not compatibility.get("compatible"):
            issues = "；".join(compatibility.get("issues") or ["训练集不兼容"])
            raise TrainingPrepError(f"训练集暂不能生成交接包：{issues}")
        norm_stats = training_set.get("norm_stats") or {}
        if not norm_stats.get("ready"):
            raise TrainingPrepError("训练集尚未完成归一化，请先计算训练集归一化")
        package_id = f"{training_set['name']}_{time.strftime('%Y%m%d_%H%M%S')}"
        package_dir = self.package_root / package_id
        duplicate = 2
        while package_dir.exists():
            package_id = f"{training_set['name']}_{time.strftime('%Y%m%d_%H%M%S')}_{duplicate}"
            package_dir = self.package_root / package_id
            duplicate += 1
        package_dir.mkdir(parents=True, exist_ok=False)
        manifest = {
            "package_id": package_id,
            "created_at": _now(),
            "data_platform": {
                "data_dir": str(self.data_dir),
                "lerobot_home": str(self.lerobot_home),
                "openpi_assets_dir": str(self.openpi_assets_dir),
            },
            "training_set": training_set,
            "training_environment": {
                "dataset_root": training_set.get("h200_dataset_root"),
                "assets_root": training_set.get("assets_base_dir") or training_set.get("h200_assets_root"),
                "openpi_data_home": training_set.get("openpi_data_home"),
                "hf_home": training_set.get("hf_home"),
                "openpi_config_name": training_set.get("config_name"),
            },
        }
        _atomic_json(package_dir / "manifest.json", manifest)
        (package_dir / "train.env").write_text(self._env_text(training_set), encoding="utf-8")
        (package_dir / "train.sh").write_text(self._train_script(training_set), encoding="utf-8")
        (package_dir / "sync_to_training_env.sh").write_text(self._sync_script(training_set), encoding="utf-8")
        (package_dir / "README.md").write_text(self._readme_text(training_set), encoding="utf-8")
        package = {
            "id": package_id,
            "training_set_id": training_set.get("id"),
            "training_set_name": training_set.get("name"),
            "config_name": training_set.get("config_name"),
            "path": str(package_dir),
            "manifest": str(package_dir / "manifest.json"),
            "created_at": manifest["created_at"],
        }
        packages = self._load_packages()
        packages.insert(0, package)
        self._save_packages(packages[:50])
        return package

    @staticmethod
    def _env_text(training_set: dict[str, Any]) -> str:
        repo_ids = ",".join(str(task.get("repo_id")) for task in training_set.get("tasks") or [])
        primary_repo_id = str((training_set.get("tasks") or [{}])[0].get("repo_id") or "")
        return "\n".join([
            f"UV_NO_SYNC={1 if training_set.get('uv_no_sync', True) else 0}",
            f"HF_LEROBOT_HOME={training_set.get('h200_dataset_root')}",
            f"OPENPI_DATA_HOME={training_set.get('openpi_data_home') or '/home/ubuntu/models/openpi'}",
            f"HF_HOME={training_set.get('hf_home') or '/home/ubuntu/models/openpi/huggingface'}",
            f"OPENPI_CONFIG_NAME={training_set.get('config_name')}",
            f"OPENPI_ACTION_DIM={training_set.get('action_dim') or 32}",
            f"OPENPI_REAL_ACTION_DIM={training_set.get('real_action_dim') or 14}",
            f"OPENPI_ACTION_HORIZON={training_set.get('action_horizon') or 16}",
            f"OPENPI_REPO_ID={primary_repo_id}",
            f"OPENPI_REPO_IDS={repo_ids}",
            f"OPENPI_H2_REPO_ID={primary_repo_id}",
            f"OPENPI_H2_REPO_IDS={repo_ids}",
            f"OPENPI_H2_ACTION_DIM={training_set.get('action_dim') or 32}",
            f"OPENPI_H2_REAL_ACTION_DIM={training_set.get('real_action_dim') or 14}",
            f"OPENPI_H2_ACTION_HORIZON={training_set.get('action_horizon') or 16}",
            f"OPENPI_TRAINING_SET_NORM_DIR={training_set.get('norm_stats_dir')}",
            f"ASSETS_BASE_DIR={training_set.get('assets_base_dir') or training_set.get('h200_assets_root')}",
            f"CHECKPOINT_BASE_DIR={training_set.get('checkpoint_base_dir') or '/home/ubuntu/models/openpi/checkpoints'}",
            f"EXP_NAME={training_set.get('exp_name') or training_set.get('name')}",
            f"FSDP_DEVICES={training_set.get('fsdp_devices') or 2}",
            f"BATCH_SIZE={training_set.get('batch_size') or 32}",
            f"NUM_TRAIN_STEPS={training_set.get('num_train_steps') or 50000}",
            f"SAVE_INTERVAL={training_set.get('save_interval') or 5000}",
            f"KEEP_PERIOD={training_set.get('keep_period') or 25000}",
            f"XLA_PYTHON_CLIENT_MEM_FRACTION={training_set.get('xla_mem_fraction') or '0.95'}",
            f"XLA_FLAGS={training_set.get('xla_flags') or DEFAULT_XLA_FLAGS}",
            f"WANDB_ENABLED={1 if training_set.get('wandb_enabled') else 0}",
            f"OVERWRITE={1 if training_set.get('overwrite', True) else 0}",
            "",
        ])

    @staticmethod
    def _train_script(training_set: dict[str, Any]) -> str:
        wandb_flag = "--wandb-enabled" if training_set.get("wandb_enabled") else "--no-wandb-enabled"
        overwrite_flag = "--overwrite" if training_set.get("overwrite", True) else ""
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'set -a; source "$SCRIPT_DIR/train.env"; set +a',
            "",
            "uv run scripts/train.py \\",
            '  "${OPENPI_CONFIG_NAME}" \\',
            '  --exp-name "${EXP_NAME}" \\',
            '  --assets-base-dir "${ASSETS_BASE_DIR}" \\',
            '  --checkpoint-base-dir "${CHECKPOINT_BASE_DIR}" \\',
            '  --fsdp-devices "${FSDP_DEVICES}" \\',
            '  --batch-size "${BATCH_SIZE}" \\',
            '  --num-train-steps "${NUM_TRAIN_STEPS}" \\',
            '  --save-interval "${SAVE_INTERVAL}" \\',
            '  --keep-period "${KEEP_PERIOD}" \\',
        ]
        if overwrite_flag:
            lines.append(f"  {overwrite_flag} \\")
        lines.append(f"  {wandb_flag}")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _sync_script(training_set: dict[str, Any]) -> str:
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'TARGET="${1:?usage: sync_to_training_env.sh user@training-host}"',
            f'DATASET_ROOT="{training_set.get("h200_dataset_root")}"',
            f'ASSETS_ROOT="{training_set.get("assets_base_dir") or training_set.get("h200_assets_root")}"',
            "",
        ]
        for task in training_set.get("tasks") or []:
            repo_id = task.get("repo_id")
            lines.append(f'ssh "$TARGET" "mkdir -p \\"$DATASET_ROOT/{repo_id}\\""')
            lines.append(f'rsync -avh --info=progress2 "{task.get("lerobot_dir")}/" "$TARGET:$DATASET_ROOT/{repo_id}/"')
        norm_dir = training_set.get("norm_stats_dir") or ""
        if norm_dir:
            config_name = training_set.get("config_name")
            norm_target = f"$ASSETS_ROOT/{config_name}/training_sets/{training_set.get('name')}"
            if len(training_set.get("tasks") or []) == 1:
                norm_target = f"$ASSETS_ROOT/{config_name}/{(training_set.get('tasks') or [{}])[0].get('repo_id')}"
            lines.append(f'if [ -d "{norm_dir}" ]; then')
            lines.append(f'  ssh "$TARGET" "mkdir -p \\"{norm_target}\\""')
            lines.append(f'  rsync -avh --info=progress2 "{norm_dir}/" "$TARGET:{norm_target}/"')
            lines.append("else")
            lines.append(f'  echo "WARN: training-set norm_stats not found: {norm_dir}"')
            lines.append("fi")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _readme_text(training_set: dict[str, Any]) -> str:
        tasks = "\n".join(
            f"- `{task.get('repo_id')}`: {task.get('instruction') or task.get('task_name')}"
            for task in training_set.get("tasks") or []
        )
        return f"""# 训练环境交接包：{training_set.get('name')}

## 数据集
{tasks}

## 归一化

本包使用训练集级别归一化：

`{training_set.get('norm_stats_dir')}`

单任务训练集可以等价于该任务自己的分布；多任务训练集必须基于所有任务的 LeRobot 数据重新计算，不复用单个任务的 norm_stats。

## OpenPI 环境变量

```bash
source train.env
```

`OPENPI_ACTION_DIM` 默认 32，`OPENPI_REAL_ACTION_DIM` 保留真实动作维度，`OPENPI_ACTION_HORIZON` 默认 16，可在创建训练集时调整。

## 同步

```bash
bash sync_to_training_env.sh ubuntu@TRAINING_HOST
```

训练配置代码仍以项目里的训练文档为准；本包提供数据路径、任务列表、训练集归一化路径、环境变量和 `train.sh` 启动模板。
"""

    def state(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        profiles = self.profiles(tasks)
        return {
            "root": str(self.training_root),
            "profiles": profiles,
            "training_sets": [
                self._refresh_training_set(item)
                for item in sorted(self._load_sets(), key=lambda item: item.get("updated_at") or "", reverse=True)
            ],
            "packages": self._load_packages(),
        }
