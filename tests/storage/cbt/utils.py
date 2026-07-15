"""CBT backup/restore utilities."""

import base64
import hashlib
import json
import logging
import os
import re
import shlex
from collections.abc import Callable, Generator
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kubernetes.client.rest import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.utils.quantity import parse_quantity
from ocp_resources.config_map import ConfigMap
from ocp_resources.datavolume import DataVolume
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.pod import Pod
from ocp_resources.resource import ResourceEditor
from ocp_resources.secret import Secret
from ocp_resources.virtual_machine_backup import VirtualMachineBackup
from ocp_resources.virtual_machine_cluster_instancetype import VirtualMachineClusterInstancetype
from ocp_resources.virtual_machine_cluster_preference import VirtualMachineClusterPreference
from pyhelper_utils.shell import run_ssh_commands
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from tests.storage.cbt.pull_collect_runner import PULL_COLLECT_PARAMS_ENV
from tests.storage.cbt.pull_restore_runner import PULL_RESTORE_PARAMS_ENV
from tests.storage.cbt.push_restore_runner import PUSH_RESTORE_PARAMS_ENV
from utilities.constants.images import OS_FLAVOR_RHEL
from utilities.constants.instance_types import U1_SMALL
from utilities.constants.networking import NET_UTIL_CONTAINER_IMAGE, POD_CONTAINER_SPEC
from utilities.constants.timeouts import (
    TIMEOUT_2MIN,
    TIMEOUT_5MIN,
    TIMEOUT_5SEC,
    TIMEOUT_10MIN,
    TIMEOUT_30MIN,
)
from utilities.storage import verify_file_in_windows_vm
from utilities.virt import VirtualMachineForTests, running_vm

if TYPE_CHECKING:
    from ocp_resources.virtual_machine import VirtualMachine

LOGGER = logging.getLogger(__name__)

CONCURRENT_CBT_VM_COUNT = 5
DATA_DISK_SIZE = "5Gi"
UNDERSIZED_BACKUP_PVC_SIZE = "1Gi"

CBT_TEST_DATA: str = "cbt-backup-test-data-content"
CBT_INCREMENTAL_TEST_DATA: str = "cbt-incremental-backup-test-data"
CBT_MULTI_INCREMENTAL_DATA_PHASE_1: str = "cbt-multi-incremental-data-phase-1"
CBT_MULTI_INCREMENTAL_DATA_PHASE_2: str = "cbt-multi-incremental-data-phase-2"
CBT_POST_MIGRATION_TEST_DATA: str = "cbt-post-migration-test-data"
CBT_DATA_DISK_TEST_DATA: str = "cbt-data-disk-test-data"
CBT_HOTPLUG_DISK_TEST_DATA: str = "cbt-hotplug-disk-test-data"
CBT_WINDOWS_TEST_DATA: str = "cbt-windows-test-data"
CBT_WINDOWS_INCREMENTAL_TEST_DATA: str = "cbt-windows-incremental-test-data"
CBT_BOOT_DISK_TEST_DATA_FILE = "/tmp/cbt-test-data.txt"
CBT_INCREMENTAL_TEST_DATA_FILE = "/tmp/cbt-incremental-test-data.txt"
CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_1 = "/tmp/cbt-multi-incremental-test-data-phase-1.txt"
CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_2 = "/tmp/cbt-multi-incremental-test-data-phase-2.txt"
CBT_POST_MIGRATION_TEST_DATA_FILE = "/tmp/cbt-post-migration-test-data.txt"
CBT_DATA_DISK_TEST_DATA_FILE = "/mnt/cbt-data/cbt-data-disk-test-data.txt"
CBT_HOTPLUG_DISK_TEST_DATA_FILE = "/mnt/cbt-hotplug/cbt-hotplug-disk-test-data.txt"
CBT_WINDOWS_TEST_DATA_FILE = r"C:\Users\cbt-test\cbt-windows-test-data.txt"
CBT_WINDOWS_INCREMENTAL_TEST_DATA_FILE = r"C:\Users\cbt-test\cbt-windows-incremental-test-data.txt"
CBT_DATA_DISK_MOUNT_PATH = "/mnt/cbt-data"
CBT_DATA_DISK_DEVICE = "/dev/vdb"
CBT_HOTPLUG_DISK_MOUNT_PATH = "/mnt/cbt-hotplug"
CBT_HOTPLUG_DISK_DEVICE = "/dev/sda"
CBT_WINDOWS_TEST_USER_DIR = r"C:\Users\cbt-test"
CBT_ENABLED_LABEL: dict[str, str] = {"changedBlockTracking": "true"}

CBT_BACKUP_MODE_PUSH = "Push"
CBT_BACKUP_MODE_PULL = "Pull"
RESTORED_DISK_FILENAME = "disk.img"
DEFAULT_BACKUP_VOLUME_NAME = "boot"
K8S_NAME_MAX_LENGTH = 63
RESTORE_PROCESSOR_CONTAINER = "cbt-restore-processor"
PULL_RESTORE_CHUNK_SIZE_BYTES = 64 * 1024 * 1024
PULL_RESTORE_PVC_SIZE_OVERHEAD = "2Gi"
PULL_RESTORE_POD_TIMEOUT_SECONDS = TIMEOUT_30MIN
PULL_CHUNK_PATH = "/tmp/pull-chunk"

BACKUP_DIR = "/backup"
BOOT_VOLUME_MOUNT_KEY = "target-boot"
BOOT_VOLUME_MOUNT_PATH = "/target-vol-0"
BOOT_VOLUME_DEVICE_PATH = "/dev/target-boot"
BACKUP_PVC_VOLUME_KEY = "backup-src"
RESTORE_WORK_VOLUME_KEY = "restore-work"
RESTORE_WORK_MOUNT_PATH = "/work"
CHECKPOINT_TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")

PULL_CA_CERT_PATH = "/tmp/backup-ca.crt"
PULL_MAP_SCAN_LIMIT_BYTES = 1 << 30
PULL_COLLECT_CHUNK_SIZE_BYTES = 256 * 1024 * 1024
PULL_MAP_HOLE_DESCRIPTIONS = ["hole", "zero"]
PULL_FULL_BACKUP_MIN_COLLECTED_BYTES = 100 * 1024 * 1024


def cbt_pvc_size_with_headroom(
    source_disk_size: str,
    headroom_gib: int = 10,
    backup_copies: int = 1,
) -> str:
    """Return a PVC size with headroom above the source disk capacity.

    ``backup_copies`` is the number of full-sized backup images the PVC must
    hold (e.g. pull-mode incremental collection seeds a new raw file by copying
    the previous checkpoint).
    """
    source_gib = parse_quantity(source_disk_size) // (1024**3)
    return f"{source_gib * backup_copies + headroom_gib}Gi"


def cbt_resource_id(name: str) -> str:
    """Return a short stable identifier for CBT resource names."""
    return hashlib.sha256(name.encode()).hexdigest()[:10]


def included_boot_volume(backup: VirtualMachineBackup) -> dict[str, Any]:
    """
    Return the single included boot volume entry from backup status.

    Restore supports one boot disk only. The returned dict always includes
    ``volumeName``.
    """
    included_volumes = backup.instance.status.get("includedVolumes", [])
    if not included_volumes:
        raise RuntimeError(f"Backup {backup.name} has no includedVolumes in status")
    if len(included_volumes) != 1:
        raise RuntimeError(
            f"Backup {backup.name} includes {len(included_volumes)} volumes; boot-disk-only restore supports one volume"
        )
    volume_status = included_volumes[0]
    volume_name = volume_status.get("volumeName", volume_status.get("name"))
    if not volume_name:
        raise RuntimeError(f"Included volume has no volumeName: {volume_status}")
    return {**volume_status, "volumeName": str(volume_name)}


def pull_checkpoint_dir_name(checkpoint_name: str) -> str:
    """Return a checkpoint directory name that sorts in backup order on client storage."""
    iso_match = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})", checkpoint_name)
    if iso_match:
        date_part, hour, minute, second = iso_match.groups()
        return f"{date_part}_{hour}-{minute}-{second}"
    timestamp_match = CHECKPOINT_TIMESTAMP_PATTERN.search(string=checkpoint_name)
    if timestamp_match:
        return timestamp_match.group(1)
    return re.sub(r"[^\w.\-]", "_", checkpoint_name)


def pull_collect_params_for_backup(
    *,
    backup: VirtualMachineBackup,
    export_token: str,
    boot_disk_size: str,
) -> dict[str, Any]:
    """Build pull collect runner parameters from a ready pull-mode backup."""
    included_volume = included_boot_volume(backup=backup)
    volume_name = included_volume["volumeName"]
    map_endpoint = included_volume.get("mapEndpoint")
    data_endpoint = included_volume.get("dataEndpoint")
    endpoint_cert = backup.instance.status.get("endpointCert")
    if not endpoint_cert:
        raise RuntimeError(f"Backup {backup.name} status has no endpointCert")
    if not map_endpoint:
        raise RuntimeError(f"Backup {backup.name} volume {volume_name} has no mapEndpoint")
    if not data_endpoint:
        raise RuntimeError(f"Backup {backup.name} volume {volume_name} has no dataEndpoint")
    checkpoint_name = backup.instance.status.get("checkpointName")
    if not checkpoint_name:
        raise RuntimeError(f"Backup {backup.name} status has no checkpointName")
    raw_file = (
        f"{BACKUP_DIR}/{volume_name}/{pull_checkpoint_dir_name(checkpoint_name=checkpoint_name)}/{volume_name}.raw"
    )
    return {
        "endpoint_cert": endpoint_cert,
        "export_token": export_token,
        "map_endpoint": map_endpoint,
        "data_endpoint": data_endpoint,
        "disk_size_bytes": int(parse_quantity(boot_disk_size)),
        "raw_file": raw_file,
        "force_full_backup": bool(backup.instance.spec.get("forceFullBackup", False)),
        "pull_ca_cert_path": PULL_CA_CERT_PATH,
        "backup_dir": BACKUP_DIR,
        "pull_map_scan_limit_bytes": PULL_MAP_SCAN_LIMIT_BYTES,
        "pull_collect_chunk_size_bytes": PULL_COLLECT_CHUNK_SIZE_BYTES,
        "pull_map_hole_descriptions": PULL_MAP_HOLE_DESCRIPTIONS,
        "pull_full_backup_min_collected_bytes": PULL_FULL_BACKUP_MIN_COLLECTED_BYTES,
        "checkpoint_timestamp_pattern": CHECKPOINT_TIMESTAMP_PATTERN.pattern,
    }


def _read_guest_file(vm: VirtualMachineForTests, filename: str) -> str:
    """Return the contents of a file from the guest over SSH."""
    return "".join(
        run_ssh_commands(
            host=vm.ssh_exec,
            commands=shlex.split(f"sudo cat {filename}"),
            wait_timeout=TIMEOUT_2MIN,
            sleep=TIMEOUT_5SEC,
        )
    ).strip()


def assert_vm_has_boot_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the VM contains the original boot-disk test data."""
    assert _read_guest_file(vm=vm, filename=CBT_BOOT_DISK_TEST_DATA_FILE) == CBT_TEST_DATA, (
        f"Boot-disk test data mismatch on VM {vm.name}"
    )


def assert_restored_vm_has_boot_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored VM contains the original boot-disk test data."""
    assert_vm_has_boot_test_data(vm=vm)


def assert_restored_vm_has_boot_and_post_migration_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored VM contains boot and post-migration test data."""
    assert_restored_vm_has_boot_test_data(vm=vm)
    assert _read_guest_file(vm=vm, filename=CBT_POST_MIGRATION_TEST_DATA_FILE) == CBT_POST_MIGRATION_TEST_DATA, (
        f"Post-migration test data mismatch on VM {vm.name}"
    )


def assert_restored_vm_has_boot_and_incremental_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored VM contains both full-backup and incremental test data."""
    assert_restored_vm_has_boot_test_data(vm=vm)
    assert _read_guest_file(vm=vm, filename=CBT_INCREMENTAL_TEST_DATA_FILE) == CBT_INCREMENTAL_TEST_DATA, (
        f"Incremental test data mismatch on VM {vm.name}"
    )


def assert_restored_vm_has_boot_and_multi_incremental_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored VM contains boot, phase-1, and phase-2 multi-incremental test data."""
    assert_restored_vm_has_boot_test_data(vm=vm)
    assert (
        _read_guest_file(vm=vm, filename=CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_1)
        == CBT_MULTI_INCREMENTAL_DATA_PHASE_1
    ), f"Multi-incremental phase-1 test data mismatch on VM {vm.name}"
    assert (
        _read_guest_file(vm=vm, filename=CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_2)
        == CBT_MULTI_INCREMENTAL_DATA_PHASE_2
    ), f"Multi-incremental phase-2 test data mismatch on VM {vm.name}"


def read_file_content_from_vm(vm: VirtualMachineForTests, file_path: str) -> str:
    """Return the contents of a file from the guest over SSH."""
    return _read_guest_file(vm=vm, filename=file_path)


def assert_restored_vm_has_boot_and_data_disk_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored VM contains boot and data-disk test data."""
    assert_restored_vm_has_boot_test_data(vm=vm)
    assert read_file_content_from_vm(vm=vm, file_path=CBT_DATA_DISK_TEST_DATA_FILE) == CBT_DATA_DISK_TEST_DATA, (
        f"Data-disk test data mismatch on VM {vm.name}"
    )


def assert_restored_vm_has_boot_and_hotplug_disk_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored VM contains boot and hotplug-disk test data."""
    assert_restored_vm_has_boot_test_data(vm=vm)
    assert read_file_content_from_vm(vm=vm, file_path=CBT_HOTPLUG_DISK_TEST_DATA_FILE) == CBT_HOTPLUG_DISK_TEST_DATA, (
        f"Hotplug-disk test data mismatch on VM {vm.name}"
    )


def assert_restored_windows_vm_has_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored Windows VM contains the original test data."""
    verify_file_in_windows_vm(
        windows_vm=vm,
        file_name_with_path=CBT_WINDOWS_TEST_DATA_FILE,
        file_content=CBT_WINDOWS_TEST_DATA,
    )


def assert_restored_windows_vm_has_boot_and_incremental_test_data(vm: VirtualMachineForTests) -> None:
    """Assert the restored Windows VM contains full-backup and incremental test data."""
    assert_restored_windows_vm_has_test_data(vm=vm)
    verify_file_in_windows_vm(
        windows_vm=vm,
        file_name_with_path=CBT_WINDOWS_INCREMENTAL_TEST_DATA_FILE,
        file_content=CBT_WINDOWS_INCREMENTAL_TEST_DATA,
    )


def get_vm_disk_volume_names(vm: VirtualMachine) -> list[str]:
    """
    Return persistent disk volume names from a VM template.

    Excludes cloud-init and other non-disk volume sources.
    """
    template_spec = vm.instance.spec["template"]["spec"]
    disk_names = {disk["name"] for disk in template_spec["domain"]["devices"]["disks"]}
    backup_volume_source_keys = ("dataVolume", "persistentVolumeClaim")
    volume_names: list[str] = []
    for volume in template_spec["volumes"]:
        volume_name = volume["name"]
        if volume_name not in disk_names:
            continue
        if not any(source_key in volume for source_key in backup_volume_source_keys):
            continue
        volume_names.append(volume_name)
    return volume_names


def normalize_backup_volume_names(included_volumes: list[dict[str, Any]]) -> list[str]:
    """Return backed-up volume names from backup status, with safe defaults."""
    if not included_volumes:
        return [DEFAULT_BACKUP_VOLUME_NAME]
    volume_names: list[str] = []
    for index, volume in enumerate(included_volumes):
        volume_name = volume.get("name") or volume.get("volumeName")
        if volume_name:
            volume_names.append(volume_name)
        elif len(included_volumes) == 1:
            volume_names.append(DEFAULT_BACKUP_VOLUME_NAME)
        else:
            volume_names.append(f"volume-{index}")
    return volume_names


def resolve_restore_volume_names(
    included_volumes: list[dict[str, Any]],
    source_volume_names: list[str] | None = None,
) -> list[str]:
    """Return volume names for qcow2 matching and restored VM disk attachment."""
    status_names = normalize_backup_volume_names(included_volumes=included_volumes)
    if not source_volume_names:
        return status_names
    if len(source_volume_names) != len(status_names):
        raise ValueError(
            f"source_volume_names length {len(source_volume_names)} "
            f"does not match backup volume count {len(status_names)}"
        )
    return source_volume_names


def truncate_k8s_name(name: str, max_length: int = K8S_NAME_MAX_LENGTH) -> str:
    """Truncate a Kubernetes resource name to the DNS label limit."""
    if len(name) <= max_length:
        return name
    return name[:max_length].rstrip("-")


def chown_mount_path_for_vm_user(vm: VirtualMachineForTests, mount_path: str) -> None:
    """Grant the VM SSH user ownership of a mounted path for test file writes."""
    run_ssh_commands(
        host=vm.ssh_exec,
        commands=[shlex.split(f"sudo chown -R {vm.username}:{vm.username} {mount_path}")],
        wait_timeout=TIMEOUT_2MIN,
        sleep=TIMEOUT_5SEC,
    )


def mount_cbt_data_disk_on_vm(vm: VirtualMachineForTests) -> None:
    """Mount the restored data disk at the expected test path on a Linux VM."""
    run_ssh_commands(
        host=vm.ssh_exec,
        commands=[
            shlex.split(f"sudo mkdir -p {CBT_DATA_DISK_MOUNT_PATH}"),
            shlex.split(f"sudo mount {CBT_DATA_DISK_DEVICE} {CBT_DATA_DISK_MOUNT_PATH}"),
        ],
        wait_timeout=TIMEOUT_2MIN,
        sleep=TIMEOUT_5SEC,
    )
    chown_mount_path_for_vm_user(vm=vm, mount_path=CBT_DATA_DISK_MOUNT_PATH)


def mount_cbt_hotplug_disk_on_vm(vm: VirtualMachineForTests) -> None:
    """Mount the restored hotplug disk at the expected test path on a Linux VM."""
    run_ssh_commands(
        host=vm.ssh_exec,
        commands=[
            shlex.split(f"sudo mkdir -p {CBT_HOTPLUG_DISK_MOUNT_PATH}"),
            shlex.split(f"sudo mount {CBT_HOTPLUG_DISK_DEVICE} {CBT_HOTPLUG_DISK_MOUNT_PATH}"),
        ],
        wait_timeout=TIMEOUT_2MIN,
        sleep=TIMEOUT_5SEC,
    )
    chown_mount_path_for_vm_user(vm=vm, mount_path=CBT_HOTPLUG_DISK_MOUNT_PATH)


def add_pvc_volume_to_vm(
    vm: VirtualMachineForTests,
    pvc: PersistentVolumeClaim,
    volume_name: str,
) -> None:
    """Attach an existing PVC as an additional virtio disk to a VM."""
    vm_instance = vm.instance.to_dict()
    template_spec = vm_instance["spec"]["template"]["spec"]
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "domain": {
                        "devices": {
                            "disks": [
                                *template_spec["domain"]["devices"]["disks"],
                                {"disk": {"bus": "virtio"}, "name": volume_name},
                            ]
                        }
                    },
                    "volumes": [
                        *template_spec["volumes"],
                        {"name": volume_name, "persistentVolumeClaim": {"claimName": pvc.name}},
                    ],
                },
            },
        }
    }
    ResourceEditor(patches={vm: patch}).update()


def restore_vm_from_backup(
    backup: VirtualMachineBackup,
    restored_vm_name: str,
    namespace: str,
    client: DynamicClient,
    storage_class: str,
    size: str,
    admin_client: DynamicClient,
    backup_pvc_name: str | None = None,
    data_disk_size: str | None = None,
    source_volume_names: list[str] | None = None,
    os_flavor: str = OS_FLAVOR_RHEL,
    vm_preference_name: str = "rhel.9",
    vm_instance_type_name: str = U1_SMALL,
) -> VirtualMachineForTests:
    """
    Restore VM disk(s) from a completed CBT backup and create a new VM.

    Push mode restores qcow2 chains from a backup PVC. Pull mode downloads raw
    volumes from export endpoints inside a restore processor pod.
    """
    backup_mode = backup.instance.spec["mode"]
    included_volumes = backup.instance.status.get("includedVolumes", [])
    status_volume_names = normalize_backup_volume_names(included_volumes=included_volumes)
    volume_names = resolve_restore_volume_names(
        included_volumes=included_volumes,
        source_volume_names=source_volume_names,
    )
    qcow2_suffixes = source_volume_names if source_volume_names else status_volume_names
    LOGGER.info(f"CBT restore {restored_vm_name}: volume_names={volume_names}, qcow2_suffixes={qcow2_suffixes}")
    restore_id = cbt_resource_id(name=restored_vm_name)

    volume_sizes = {volume_names[0]: size}
    if len(volume_names) > 1:
        if data_disk_size is None:
            raise ValueError("data_disk_size is required when backup includes multiple volumes")
        for volume_name in volume_names[1:]:
            volume_sizes[volume_name] = data_disk_size

    with ExitStack() as stack:
        target_pvcs: dict[str, PersistentVolumeClaim] = {}
        volume_mount_targets: list[tuple[str, str]] = []
        for index, volume_name in enumerate(volume_names):
            mount_path = _multi_volume_target_mount_path(volume_index=index)
            volume_mount_targets.append((volume_name, mount_path))
            target_pvc_size = volume_sizes[volume_name]
            if backup_mode == CBT_BACKUP_MODE_PULL:
                target_pvc_size = _pull_restore_target_pvc_size(source_size=target_pvc_size)
            target_pvcs[volume_name] = stack.enter_context(
                PersistentVolumeClaim(
                    name=_multi_volume_target_pvc_name(restore_id=restore_id, volume_index=index),
                    namespace=namespace,
                    client=client,
                    accessmodes=PersistentVolumeClaim.AccessMode.RWO,
                    size=target_pvc_size,
                    storage_class=storage_class,
                    volume_mode=DataVolume.VolumeMode.FILE,
                    teardown=False,
                )
            )

        volume_mounts: list[dict[str, Any]] = []
        volumes: list[dict[str, Any]] = []

        for index, volume_name in enumerate(volume_names):
            volume_key = _multi_volume_target_mount_key(volume_index=index)
            mount_path = _multi_volume_target_mount_path(volume_index=index)
            volume_mounts.append({"name": volume_key, "mountPath": mount_path})
            volumes.append({
                "name": volume_key,
                "persistentVolumeClaim": {"claimName": target_pvcs[volume_name].name},
            })

        restore_pod_name = truncate_k8s_name(
            name=f"cbt-rstr-{restore_id}-{'push' if backup_mode == CBT_BACKUP_MODE_PUSH else 'pull'}"
        )

        if backup_mode == CBT_BACKUP_MODE_PUSH:
            backup_pvc = backup_pvc_name or backup.instance.spec["pvcName"]
            volume_mounts.append({"name": BACKUP_PVC_VOLUME_KEY, "mountPath": BACKUP_DIR, "readOnly": True})
            volumes.append({
                "name": BACKUP_PVC_VOLUME_KEY,
                "persistentVolumeClaim": {"claimName": backup_pvc},
            })
            volume_mounts.append({"name": RESTORE_WORK_VOLUME_KEY, "mountPath": RESTORE_WORK_MOUNT_PATH})
            volumes.append({"name": RESTORE_WORK_VOLUME_KEY, "emptyDir": {}})
            _run_restore_processor_pod(
                pod_name=restore_pod_name,
                namespace=namespace,
                admin_client=admin_client,
                volume_mounts=volume_mounts,
                volumes=volumes,
                wait_timeout=TIMEOUT_10MIN,
                restore_action=lambda restore_pod: _run_push_restore(
                    restore_pod=restore_pod,
                    volume_mount_targets=volume_mount_targets,
                    qcow2_suffixes=qcow2_suffixes,
                ),
            )
        elif backup_mode == CBT_BACKUP_MODE_PULL:
            _run_restore_processor_pod(
                pod_name=restore_pod_name,
                namespace=namespace,
                admin_client=admin_client,
                volume_mounts=volume_mounts,
                volumes=volumes,
                wait_timeout=PULL_RESTORE_POD_TIMEOUT_SECONDS,
                restore_action=lambda restore_pod: _run_pull_restore(
                    restore_pod=restore_pod,
                    backup=backup,
                    client=client,
                    volume_mount_targets=volume_mount_targets,
                    volume_sizes=volume_sizes,
                ),
            )
        else:
            raise ValueError(f"Unsupported backup mode {backup_mode!r}; expected Push or Pull")

        boot_volume_name = volume_names[0]
        restored_vm = VirtualMachineForTests(
            name=restored_vm_name,
            namespace=namespace,
            client=client,
            vm_instance_type=VirtualMachineClusterInstancetype(client=client, name=vm_instance_type_name),
            vm_preference=VirtualMachineClusterPreference(client=client, name=vm_preference_name),
            pvc=target_pvcs[boot_volume_name],
            os_flavor=os_flavor,
            label=CBT_ENABLED_LABEL,
            generate_unique_name=False,
        )
        restored_vm.deploy()

        for volume_name in volume_names[1:]:
            add_pvc_volume_to_vm(
                vm=restored_vm,
                pvc=target_pvcs[volume_name],
                volume_name=volume_name,
            )

        return restored_vm


def restore_and_start_multi_volume_vm_from_backup(
    *,
    vm: VirtualMachineForTests,
    backup: VirtualMachineBackup,
    namespace: str,
    client: DynamicClient,
    admin_client: DynamicClient,
    storage_class: str,
    size: str,
    backup_pvc_name: str | None = None,
    data_disk_size: str | None = None,
    source_volume_names: list[str] | None = None,
    mount_secondary_disk: Callable[[VirtualMachineForTests], None] | None = None,
    ssh_timeout: int = TIMEOUT_5MIN,
) -> Generator[VirtualMachineForTests]:
    """Delete the source VM, restore multiple volumes from backup, start it, then clean up."""
    restored_vm_name = vm.name
    if restored_vm_name is None:
        raise RuntimeError("Cannot restore: source VM has no name")
    vm_instance_type_name = vm.instance.spec["instancetype"]["name"]
    vm_preference_name = vm.instance.spec["preference"]["name"]
    os_flavor = vm.os_flavor
    vm.delete(wait=True)
    vm.teardown = False

    restored_vm = restore_vm_from_backup(
        backup=backup,
        restored_vm_name=restored_vm_name,
        namespace=namespace,
        client=client,
        storage_class=storage_class,
        size=size,
        admin_client=admin_client,
        backup_pvc_name=backup_pvc_name,
        data_disk_size=data_disk_size,
        source_volume_names=source_volume_names,
        os_flavor=os_flavor,
        vm_preference_name=vm_preference_name,
        vm_instance_type_name=vm_instance_type_name,
    )
    running_vm(vm=restored_vm, ssh_timeout=ssh_timeout)
    if mount_secondary_disk is not None:
        mount_secondary_disk(vm=restored_vm)
    try:
        yield restored_vm
    finally:
        restored_vm.delete(wait=True)


def _multi_volume_target_pvc_name(restore_id: str, volume_index: int) -> str:
    suffix = "boot" if volume_index == 0 else f"vol{volume_index}"
    return truncate_k8s_name(name=f"cbt-rst-{restore_id}-{suffix}")


def _multi_volume_target_mount_key(volume_index: int) -> str:
    return "target-boot" if volume_index == 0 else f"target-vol-{volume_index}"


def _multi_volume_target_mount_path(volume_index: int) -> str:
    return f"/target-vol-{volume_index}"


def _uses_placeholder_qcow2_suffixes(qcow2_suffixes: list[str]) -> bool:
    return any(
        qcow2_suffix == DEFAULT_BACKUP_VOLUME_NAME or qcow2_suffix.startswith("volume-")
        for qcow2_suffix in qcow2_suffixes
    )


def _checkpoint_timestamp_from_qcow2_path(qcow2_path: str) -> str:
    match = CHECKPOINT_TIMESTAMP_PATTERN.search(qcow2_path)
    return match.group(1) if match else ""


def _sort_qcow2_files_by_checkpoint(qcow2_files: list[str]) -> list[str]:
    return sorted(qcow2_files, key=_checkpoint_timestamp_from_qcow2_path)


def _latest_checkpoint_timestamp(qcow2_files: list[str]) -> str:
    checkpoint_timestamps = [
        timestamp
        for timestamp in (_checkpoint_timestamp_from_qcow2_path(path=path) for path in qcow2_files)
        if timestamp
    ]
    if not checkpoint_timestamps:
        raise RuntimeError(f"No checkpoint timestamps found in qcow2 paths: {qcow2_files}")
    return sorted(checkpoint_timestamps)[-1]


def _pod_execute(restore_pod: Pod, command: list[str], timeout: int = TIMEOUT_10MIN) -> str:
    LOGGER.info(f"CBT restore exec on {restore_pod.name}: {command}")
    return restore_pod.execute(
        command=command,
        timeout=timeout,
        container=RESTORE_PROCESSOR_CONTAINER,
    )


def _write_file_in_pod(restore_pod: Pod, file_path: str, content: str) -> None:
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
    _pod_execute(
        restore_pod=restore_pod,
        command=[
            "/bin/bash",
            "-c",
            f"echo {shlex.quote(encoded_content)} | base64 -d > {shlex.quote(file_path)}",
        ],
    )


def _list_qcow2_files_in_backup(restore_pod: Pod, name_pattern: str) -> list[str]:
    find_output = _pod_execute(
        restore_pod=restore_pod,
        command=["/usr/bin/find", BACKUP_DIR, "-name", name_pattern, "-type", "f"],
    )
    qcow2_files = [line.strip() for line in find_output.splitlines() if line.strip()]
    if qcow2_files:
        return qcow2_files
    backup_listing = _pod_execute(
        restore_pod=restore_pod,
        command=["/usr/bin/find", BACKUP_DIR, "-type", "f"],
    )
    raise RuntimeError(f"No qcow2 files matching {name_pattern!r} under {BACKUP_DIR}. Files:\n{backup_listing}")


def _qemu_img_convert_to_raw(restore_pod: Pod, qcow2_file: str, target_dir: str) -> None:
    target_file = f"{target_dir}/{RESTORED_DISK_FILENAME}"
    _pod_execute(
        restore_pod=restore_pod,
        command=["qemu-img", "convert", "-f", "qcow2", "-O", "raw", qcow2_file, target_file],
        timeout=PULL_RESTORE_POD_TIMEOUT_SECONDS,
    )


def _restore_push_volume_chain(
    restore_pod: Pod,
    volume_name: str,
    target_dir: str,
    qcow2_suffix: str,
    single_volume: bool,
) -> None:
    name_pattern = "*.qcow2" if single_volume else f"*-{qcow2_suffix}.qcow2"
    qcow2_files = _sort_qcow2_files_by_checkpoint(
        _list_qcow2_files_in_backup(restore_pod=restore_pod, name_pattern=name_pattern)
    )
    if len(qcow2_files) == 1:
        _qemu_img_convert_to_raw(restore_pod=restore_pod, qcow2_file=qcow2_files[0], target_dir=target_dir)
        return

    volume_work_dir = f"{RESTORE_WORK_MOUNT_PATH}/{volume_name}"
    _pod_execute(restore_pod=restore_pod, command=["/bin/mkdir", "-p", volume_work_dir])
    work_files: list[str] = []
    for file_index, qcow2_file in enumerate(qcow2_files):
        work_path = f"{volume_work_dir}/chain-{file_index}-{os.path.basename(qcow2_file)}"
        _pod_execute(restore_pod=restore_pod, command=["/bin/cp", qcow2_file, work_path])
        work_files.append(work_path)

    base_image = work_files[0]
    for work_file in work_files[1:]:
        _pod_execute(
            restore_pod=restore_pod,
            command=["qemu-img", "rebase", "-b", base_image, "-F", "qcow2", "-f", "qcow2", "-u", work_file],
        )
        base_image = work_file
    _qemu_img_convert_to_raw(restore_pod=restore_pod, qcow2_file=base_image, target_dir=target_dir)


def _restore_push_multi_disk_with_placeholder_suffixes(
    restore_pod: Pod,
    volume_mount_targets: list[tuple[str, str]],
    qcow2_suffixes: list[str],
) -> None:
    all_qcow2_files = _list_qcow2_files_in_backup(restore_pod=restore_pod, name_pattern="*.qcow2")
    latest_timestamp = _latest_checkpoint_timestamp(qcow2_files=all_qcow2_files)
    for (_, target_dir), qcow2_suffix in zip(volume_mount_targets, qcow2_suffixes, strict=True):
        matching_files = [
            path for path in all_qcow2_files if latest_timestamp in path and path.endswith(f"-{qcow2_suffix}.qcow2")
        ]
        if not matching_files:
            raise RuntimeError(
                f"No qcow2 file found for suffix {qcow2_suffix!r} in checkpoint {latest_timestamp}. "
                f"Files: {all_qcow2_files}"
            )
        _qemu_img_convert_to_raw(restore_pod=restore_pod, qcow2_file=sorted(matching_files)[-1], target_dir=target_dir)


def _run_push_restore(
    restore_pod: Pod,
    volume_mount_targets: list[tuple[str, str]],
    qcow2_suffixes: list[str],
) -> None:
    if len(volume_mount_targets) > 1 and _uses_placeholder_qcow2_suffixes(qcow2_suffixes=qcow2_suffixes):
        _restore_push_multi_disk_with_placeholder_suffixes(
            restore_pod=restore_pod,
            volume_mount_targets=volume_mount_targets,
            qcow2_suffixes=qcow2_suffixes,
        )
        return

    single_volume = len(volume_mount_targets) == 1
    for (volume_name, target_dir), qcow2_suffix in zip(volume_mount_targets, qcow2_suffixes, strict=True):
        _restore_push_volume_chain(
            restore_pod=restore_pod,
            volume_name=volume_name,
            target_dir=target_dir,
            qcow2_suffix=qcow2_suffix,
            single_volume=single_volume,
        )


def _pvc_size_to_bytes(size: str) -> int:
    return int(parse_quantity(size))


def _pull_restore_target_pvc_size(source_size: str) -> str:
    gibibyte = 1024**3
    total_bytes = int(parse_quantity(source_size)) + int(parse_quantity(PULL_RESTORE_PVC_SIZE_OVERHEAD))
    gibibytes = (total_bytes + gibibyte - 1) // gibibyte
    return f"{gibibytes}Gi"


def _get_backup_export_token(backup: VirtualMachineBackup, client: DynamicClient) -> str:
    if hasattr(backup, "get_export_token"):
        return backup.get_export_token()
    secret_name = backup.instance.spec["tokenSecretRef"]
    secret = Secret(name=secret_name, namespace=backup.namespace, client=client)
    token_data = secret.instance.data["token"]
    return base64.b64decode(token_data).decode("utf-8")


def _download_pull_volume(
    restore_pod: Pod,
    data_endpoint: str,
    export_token: str,
    target_dir: str,
    disk_size_bytes: int,
) -> None:
    target_file = f"{target_dir}/{RESTORED_DISK_FILENAME}"
    _pod_execute(
        restore_pod=restore_pod,
        command=["/bin/bash", "-c", f": > {shlex.quote(target_file)}"],
    )
    offset = 0
    while offset < disk_size_bytes:
        remaining_bytes = disk_size_bytes - offset
        chunk_length = min(PULL_RESTORE_CHUNK_SIZE_BYTES, remaining_bytes)
        download_url = f"{data_endpoint}?x-kubevirt-export-token={export_token}&offset={offset}&length={chunk_length}"
        _pod_execute(
            restore_pod=restore_pod,
            command=[
                "curl",
                "-s",
                "-L",
                "--fail",
                "--cacert",
                PULL_CA_CERT_PATH,
                download_url,
                "--output",
                PULL_CHUNK_PATH,
            ],
            timeout=TIMEOUT_10MIN,
        )
        _pod_execute(
            restore_pod=restore_pod,
            command=[
                "dd",
                f"if={PULL_CHUNK_PATH}",
                f"of={target_file}",
                "oflag=seek_bytes",
                f"seek={offset}",
                "conv=notrunc",
                "status=none",
            ],
        )
        offset += chunk_length


def _run_pull_restore(
    restore_pod: Pod,
    backup: VirtualMachineBackup,
    client: DynamicClient,
    volume_mount_targets: list[tuple[str, str]],
    volume_sizes: dict[str, str],
) -> None:
    included_volumes = backup.instance.status.get("includedVolumes", [])
    endpoint_cert = backup.instance.status.get("endpointCert", "")
    if not endpoint_cert:
        raise RuntimeError(f"Backup {backup.name} status has no endpointCert")

    export_token = _get_backup_export_token(backup=backup, client=client)
    _write_file_in_pod(restore_pod=restore_pod, file_path=PULL_CA_CERT_PATH, content=endpoint_cert)

    for volume, (volume_name, target_dir) in zip(included_volumes, volume_mount_targets, strict=True):
        data_endpoint = volume.get("dataEndpoint")
        if not data_endpoint:
            raise RuntimeError(f"Backup {backup.name} volume {volume_name} has no dataEndpoint")
        disk_size_bytes = _pvc_size_to_bytes(size=volume_sizes[volume_name])
        LOGGER.info(f"Pull restore volume {volume_name} from {data_endpoint} ({disk_size_bytes} bytes)")
        _download_pull_volume(
            restore_pod=restore_pod,
            data_endpoint=data_endpoint,
            export_token=export_token,
            target_dir=target_dir,
            disk_size_bytes=disk_size_bytes,
        )


def _run_restore_processor_pod(
    pod_name: str,
    namespace: str,
    admin_client: DynamicClient,
    volume_mounts: list[dict[str, Any]],
    volumes: list[dict[str, Any]],
    wait_timeout: int,
    restore_action: Callable[[Pod], None],
) -> None:
    container_spec = {
        **POD_CONTAINER_SPEC,
        "name": RESTORE_PROCESSOR_CONTAINER,
        "image": NET_UTIL_CONTAINER_IMAGE,
        "command": ["sleep", "infinity"],
        "volumeMounts": volume_mounts,
    }

    with Pod(
        name=pod_name,
        namespace=namespace,
        client=admin_client,
        containers=[container_spec],
        volumes=volumes,
        restart_policy="Never",
    ) as restore_pod:
        LOGGER.info(f"Running CBT restore processor pod {pod_name} in {namespace}")
        restore_pod.wait_for_status(status=Pod.Status.RUNNING, timeout=wait_timeout)
        try:
            restore_action(restore_pod)
        except Exception as restore_error:
            pod_logs = restore_pod.log()
            raise RuntimeError(
                f"Restore pod {restore_pod.name} failed during restore: {restore_error}. Logs:\n{pod_logs}"
            ) from restore_error
        LOGGER.info(f"Restore pod {restore_pod.name} completed successfully")


def wait_for_backup_to_fail(backup: VirtualMachineBackup, timeout: int = TIMEOUT_10MIN) -> None:
    """
    Wait until a backup reaches Failed=True or raise if it completes successfully.

    Args:
        backup: VirtualMachineBackup expected to fail
        timeout: Seconds to wait for the Failed condition

    Raises:
        RuntimeError: If the backup reaches Complete=True instead of failing
        TimeoutExpiredError: If Failed=True is not observed within timeout
    """
    LOGGER.info(f"Waiting for backup {backup.name} to reach Failed=True")
    try:
        for backup_instance in TimeoutSampler(
            wait_timeout=timeout,
            sleep=TIMEOUT_5SEC,
            func=lambda: backup.instance,
        ):
            conditions = backup_instance.get("status", {}).get("conditions", [])
            for condition in conditions:
                condition_type = condition.get("type")
                condition_status = condition.get("status")
                if condition_type == "Failed" and condition_status == "True":
                    LOGGER.info(f"Backup {backup.name} reached Failed=True")
                    return
                if condition_type == "Complete" and condition_status == "True":
                    raise RuntimeError(
                        f"Backup {backup.name} reached Complete=True but was expected to fail. Conditions: {conditions}"
                    )
    except TimeoutExpiredError as timeout_error:
        raise TimeoutExpiredError(
            f"Backup {backup.name} did not reach Failed=True within {timeout}s. "
            f"Status: {backup.instance.get('status', {})}"
        ) from timeout_error


def release_pull_mode_backup(backup: VirtualMachineBackup) -> None:
    """
    Delete a pull-mode backup after its export has been consumed.

    Pull backups remain active until the client deletes the VirtualMachineBackup CR.
    The admission webhook allows only one in-progress backup per source at a time.
    """
    LOGGER.info(f"Releasing pull-mode backup {backup.name} in namespace {backup.namespace}")
    backup.delete(wait=True)


def _boot_volume_pod_volumes(
    *, boot_pvc_name: str, volume_mode: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (volume_mounts, volume_devices, volumes) for the restore target boot PVC.

    Block-mode PVCs must be exposed as raw block devices via volumeDevices; filesystem
    mounts (volumeMounts) only work for Filesystem-mode PVCs.
    """
    volumes = [{"name": BOOT_VOLUME_MOUNT_KEY, "persistentVolumeClaim": {"claimName": boot_pvc_name}}]
    if volume_mode == DataVolume.VolumeMode.BLOCK:
        return [], [{"name": BOOT_VOLUME_MOUNT_KEY, "devicePath": BOOT_VOLUME_DEVICE_PATH}], volumes
    return [{"name": BOOT_VOLUME_MOUNT_KEY, "mountPath": BOOT_VOLUME_MOUNT_PATH}], [], volumes


def _restore_target_path(*, volume_mode: str) -> str:
    """Return the in-pod path for the restored boot disk (block device or disk.img)."""
    if volume_mode == DataVolume.VolumeMode.BLOCK:
        return BOOT_VOLUME_DEVICE_PATH
    return f"{BOOT_VOLUME_MOUNT_PATH}/disk.img"


def create_and_collect_pull_mode_backup(
    *,
    name: str,
    namespace: str,
    client: DynamicClient,
    token_secret_name: str,
    export_token: str,
    staging_pvc_name: str,
    client_backup_pvc_name: str,
    backup_tracker_source: dict[str, str],
    force_full_backup: bool,
    boot_disk_size: str,
) -> None:
    """Create an online pull-mode backup, collect extents to client PVC storage, then delete the backup CR.

    Collection to the client PVC is test-side stand-in storage for validating pull export;
    it is not product offline-backup support.
    """
    with VirtualMachineBackup(
        name=name,
        namespace=namespace,
        client=client,
        mode=VirtualMachineBackup.Mode.PULL,
        token_secret_ref=token_secret_name,
        pvc_name=staging_pvc_name,
        force_full_backup=force_full_backup,
        source=backup_tracker_source,
    ) as backup:
        # Pull readiness is Progressing=True with reason ExportReady; there is no
        # condition type named ExportReady.
        backup.wait_for_condition(
            condition="Progressing",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            reason="ExportReady",
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        _run_python_runner_pod(
            pod_name=f"cbt-pull-collect-{cbt_resource_id(name=f'{backup.name}-collect')}",
            namespace=namespace,
            client=client,
            runner_script_filename="pull_collect_runner.py",
            container_name="cbt-pull-collect",
            params_env_name=PULL_COLLECT_PARAMS_ENV,
            runner_params=pull_collect_params_for_backup(
                backup=backup,
                export_token=export_token,
                boot_disk_size=boot_disk_size,
            ),
            volume_mounts=[{"name": BACKUP_PVC_VOLUME_KEY, "mountPath": BACKUP_DIR}],
            volumes=[
                {
                    "name": BACKUP_PVC_VOLUME_KEY,
                    "persistentVolumeClaim": {"claimName": client_backup_pvc_name},
                }
            ],
            wait_timeout=TIMEOUT_30MIN,
            pod_role="pull collect",
        )
        LOGGER.info(f"Pull backup collection complete for {backup.name}; deleting backup CR")
        backup.delete(wait=True)
        backup.teardown = False


def restore_and_start_vm_from_push_backup(
    *,
    vm: VirtualMachineForTests,
    backup: VirtualMachineBackup,
    namespace: str,
    client: DynamicClient,
    storage_class: str,
    size: str,
    volume_mode: str,
    access_mode: str,
    backup_pvc_name: str,
    ssh_timeout: int = TIMEOUT_5MIN,
) -> Generator[VirtualMachineForTests]:
    """Delete the source VM, restore from a push backup, start it, then clean up."""
    restored_vm_name = vm.name
    if restored_vm_name is None:
        raise RuntimeError("Cannot restore: source VM has no name")
    boot_volume_name = included_boot_volume(backup=backup)["volumeName"]
    vm_instance_type_name = vm.instance.spec["instancetype"]["name"]
    vm_preference_name = vm.instance.spec["preference"]["name"]
    os_flavor = vm.os_flavor
    vm.delete(wait=True)
    vm.teardown = False

    restore_id = cbt_resource_id(name=restored_vm_name)
    target_file = _restore_target_path(volume_mode=volume_mode)
    LOGGER.info(f"CBT push restore {restored_vm_name}: boot_volume_name={boot_volume_name}")
    with PersistentVolumeClaim(
        name=f"cbt-rst-{restore_id}-boot",
        namespace=namespace,
        client=client,
        accessmodes=access_mode,
        size=size,
        storage_class=storage_class,
        volume_mode=volume_mode,
        teardown=False,
    ) as boot_pvc:
        boot_volume_mounts, boot_volume_devices, boot_volumes = _boot_volume_pod_volumes(
            boot_pvc_name=boot_pvc.name, volume_mode=volume_mode
        )
        _run_python_runner_pod(
            pod_name=f"cbt-rstr-{restore_id}-push",
            namespace=namespace,
            client=client,
            runner_script_filename="push_restore_runner.py",
            container_name="cbt-push-restore",
            params_env_name=PUSH_RESTORE_PARAMS_ENV,
            runner_params={
                "backup_dir": BACKUP_DIR,
                "volume_work_dir": f"{RESTORE_WORK_MOUNT_PATH}/{boot_volume_name}",
                "target_file": target_file,
                "checkpoint_timestamp_pattern": CHECKPOINT_TIMESTAMP_PATTERN.pattern,
            },
            volume_mounts=[
                *boot_volume_mounts,
                {"name": BACKUP_PVC_VOLUME_KEY, "mountPath": BACKUP_DIR, "readOnly": True},
                {"name": RESTORE_WORK_VOLUME_KEY, "mountPath": RESTORE_WORK_MOUNT_PATH},
            ],
            volume_devices=boot_volume_devices or None,
            volumes=[
                *boot_volumes,
                {"name": BACKUP_PVC_VOLUME_KEY, "persistentVolumeClaim": {"claimName": backup_pvc_name}},
                {"name": RESTORE_WORK_VOLUME_KEY, "emptyDir": {}},
            ],
            wait_timeout=TIMEOUT_30MIN,
            pod_role="push restore",
        )
        restored_vm = VirtualMachineForTests(
            name=restored_vm_name,
            namespace=namespace,
            client=client,
            vm_instance_type=VirtualMachineClusterInstancetype(client=client, name=vm_instance_type_name),
            vm_preference=VirtualMachineClusterPreference(client=client, name=vm_preference_name),
            pvc=boot_pvc,
            os_flavor=os_flavor,
            label=CBT_ENABLED_LABEL,
            generate_unique_name=False,
        )
        restored_vm.deploy()

    running_vm(vm=restored_vm, ssh_timeout=ssh_timeout)
    try:
        yield restored_vm
    finally:
        restored_vm.delete(wait=True)


def restore_and_start_vm_from_pull_client_backup(
    *,
    vm: VirtualMachineForTests,
    client_backup_pvc_name: str,
    namespace: str,
    client: DynamicClient,
    storage_class: str,
    size: str,
    volume_mode: str,
    access_mode: str,
    ssh_timeout: int = TIMEOUT_5MIN,
) -> Generator[VirtualMachineForTests]:
    """Delete the source VM, restore from pull client storage, start it, then clean up."""
    restored_vm_name = vm.name
    if restored_vm_name is None:
        raise RuntimeError("Cannot restore: source VM has no name")
    # Collect stores raw files under the backup status volumeName; capture it before
    # the original VM is deleted so restore can scope to that directory.
    boot_volume_name = vm.instance.spec.template.spec.volumes[0]["name"]
    vm_instance_type_name = vm.instance.spec["instancetype"]["name"]
    vm_preference_name = vm.instance.spec["preference"]["name"]
    os_flavor = vm.os_flavor
    vm.delete(wait=True)
    vm.teardown = False

    restore_id = cbt_resource_id(name=restored_vm_name)
    target_file = _restore_target_path(volume_mode=volume_mode)
    LOGGER.info(f"CBT pull restore {restored_vm_name}: boot_volume_name={boot_volume_name}")
    # teardown=False so the with-block exit does not delete the boot PVC while the VM still
    # references it; cleanup deletes the VM first, then the PVC.
    with PersistentVolumeClaim(
        name=f"cbt-rst-{restore_id}-boot",
        namespace=namespace,
        client=client,
        accessmodes=access_mode,
        size=size,
        storage_class=storage_class,
        volume_mode=volume_mode,
        teardown=False,
    ) as boot_pvc:
        restored_vm = None
        try:
            boot_volume_mounts, boot_volume_devices, boot_volumes = _boot_volume_pod_volumes(
                boot_pvc_name=boot_pvc.name, volume_mode=volume_mode
            )
            _run_python_runner_pod(
                pod_name=f"cbt-rstr-{restore_id}-client",
                namespace=namespace,
                client=client,
                runner_script_filename="pull_restore_runner.py",
                container_name="cbt-pull-restore",
                params_env_name=PULL_RESTORE_PARAMS_ENV,
                runner_params={
                    "backup_dir": BACKUP_DIR,
                    "volume_name": boot_volume_name,
                    "target_file": target_file,
                    "volume_mode": volume_mode,
                    "checkpoint_timestamp_pattern": CHECKPOINT_TIMESTAMP_PATTERN.pattern,
                },
                volume_mounts=[
                    *boot_volume_mounts,
                    {"name": BACKUP_PVC_VOLUME_KEY, "mountPath": BACKUP_DIR, "readOnly": True},
                ],
                volume_devices=boot_volume_devices or None,
                volumes=[
                    *boot_volumes,
                    {
                        "name": BACKUP_PVC_VOLUME_KEY,
                        "persistentVolumeClaim": {"claimName": client_backup_pvc_name},
                    },
                ],
                wait_timeout=TIMEOUT_30MIN,
                pod_role="pull client restore",
            )
            restored_vm = VirtualMachineForTests(
                name=restored_vm_name,
                namespace=namespace,
                client=client,
                vm_instance_type=VirtualMachineClusterInstancetype(client=client, name=vm_instance_type_name),
                vm_preference=VirtualMachineClusterPreference(client=client, name=vm_preference_name),
                pvc=boot_pvc,
                os_flavor=os_flavor,
                label=CBT_ENABLED_LABEL,
                generate_unique_name=False,
            )
            restored_vm.deploy()
            running_vm(vm=restored_vm, ssh_timeout=ssh_timeout)
            yield restored_vm
        finally:
            try:
                if restored_vm is not None:
                    restored_vm.delete(wait=True)
            finally:
                boot_pvc.delete(wait=True)


def _run_python_runner_pod(
    *,
    pod_name: str,
    namespace: str,
    client: DynamicClient,
    runner_script_filename: str,
    container_name: str,
    params_env_name: str,
    runner_params: dict[str, Any],
    volume_mounts: list[dict[str, Any]],
    volumes: list[dict[str, Any]],
    wait_timeout: int,
    pod_role: str,
    volume_devices: list[dict[str, Any]] | None = None,
) -> None:
    """Run a one-shot pod whose main process executes a mounted Python runner script."""
    script_mount_path = "/scripts"
    script_volume_key = "runner-script"
    script_config_map_name = f"{pod_name}-script"
    runner_script_content = Path(__file__).with_name(runner_script_filename).read_text(encoding="utf-8")
    runner_pod_volume_mounts = [
        *volume_mounts,
        {
            "name": script_volume_key,
            "mountPath": script_mount_path,
            "readOnly": True,
        },
    ]
    runner_pod_volumes = [
        *volumes,
        {
            "name": script_volume_key,
            "configMap": {"name": script_config_map_name},
        },
    ]
    with ConfigMap(
        name=script_config_map_name,
        namespace=namespace,
        client=client,
        data={runner_script_filename: runner_script_content},
    ):
        _run_one_shot_client_pod(
            pod_name=pod_name,
            namespace=namespace,
            client=client,
            container_name=container_name,
            volume_mounts=runner_pod_volume_mounts,
            volume_devices=volume_devices,
            volumes=runner_pod_volumes,
            container_command=[
                "python3",
                "-u",
                f"{script_mount_path}/{runner_script_filename}",
            ],
            container_env=[
                {
                    "name": params_env_name,
                    "value": json.dumps(runner_params),
                }
            ],
            wait_timeout=wait_timeout,
            pod_role=pod_role,
        )


def _pod_debug_context(client_pod: Pod) -> str:
    """Return pod phase, container state, and logs for failure diagnostics."""
    client_pod.get()
    container_name = client_pod.instance.spec.containers[0].name
    container_statuses = client_pod.instance.status.get("containerStatuses") or []
    pod_conditions = client_pod.instance.status.get("conditions") or []
    try:
        pod_logs = client_pod.log(container=container_name)
    except ApiException as log_error:
        pod_logs = f"<unavailable: {log_error}>"
    return (
        f"phase={client_pod.instance.status.phase}\n"
        f"conditions={pod_conditions}\n"
        f"containerStatuses={container_statuses}\n"
        f"logs:\n{pod_logs}"
    )


def _run_one_shot_client_pod(
    *,
    pod_name: str,
    namespace: str,
    client: DynamicClient,
    container_name: str,
    volume_mounts: list[dict[str, Any]],
    volumes: list[dict[str, Any]],
    container_command: list[str],
    wait_timeout: int,
    pod_role: str,
    volume_devices: list[dict[str, Any]] | None = None,
    container_env: list[dict[str, str]] | None = None,
) -> None:
    """Run a single-purpose client pod whose main process runs to completion."""
    container_spec: dict[str, Any] = {
        **POD_CONTAINER_SPEC,
        "name": container_name,
        "image": NET_UTIL_CONTAINER_IMAGE,
        "command": container_command,
        "env": container_env or [],
        "volumeMounts": volume_mounts,
    }
    if volume_devices:
        container_spec["volumeDevices"] = volume_devices

    with Pod(
        name=pod_name,
        namespace=namespace,
        client=client,
        containers=[container_spec],
        volumes=volumes,
        restart_policy="Never",
    ) as client_pod:
        LOGGER.info(f"Running CBT {pod_role} pod {pod_name} in {namespace}")
        try:
            client_pod.wait_for_status(
                status=Pod.Status.SUCCEEDED,
                timeout=wait_timeout,
                stop_status=Pod.Status.FAILED,
                sleep=TIMEOUT_5SEC,
            )
        except TimeoutExpiredError as wait_error:
            raise RuntimeError(
                f"CBT {pod_role} pod {client_pod.name} did not succeed: {wait_error}. "
                f"{_pod_debug_context(client_pod=client_pod)}"
            ) from wait_error
        LOGGER.info(f"CBT {pod_role} pod {client_pod.name} completed successfully")
