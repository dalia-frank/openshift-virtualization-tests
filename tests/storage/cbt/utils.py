"""CBT backup utilities (backup success only)."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from kubernetes.utils.quantity import parse_quantity
from ocp_resources.virtual_machine_export import VirtualMachineExport
from timeout_sampler import TimeoutSampler

from utilities.constants.timeouts import TIMEOUT_5SEC, TIMEOUT_10MIN

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from ocp_resources.virtual_machine import VirtualMachine
    from ocp_resources.virtual_machine_backup import VirtualMachineBackup

LOGGER = logging.getLogger(__name__)


def cbt_pvc_size_with_headroom(source_disk_size: str, headroom_gib: int = 10) -> str:
    """Return a PVC size with headroom above the source disk capacity."""
    source_gib = parse_quantity(source_disk_size) // (1024**3)
    return f"{source_gib + headroom_gib}Gi"


def cbt_resource_id(name: str) -> str:
    """Return a short stable identifier for CBT resource names."""
    return hashlib.sha256(name.encode()).hexdigest()[:10]


def assert_backup_includes_volumes(
    *,
    backup: VirtualMachineBackup,
    expected_volume_names: list[str],
    expected_backup_type: str | None = None,
) -> None:
    """Assert a ready backup includes the expected volumes (and optional type)."""
    backup_status = backup.instance.status
    included_volumes = backup_status["includedVolumes"]
    actual_volume_names = [volume["volumeName"] for volume in included_volumes]
    assert sorted(actual_volume_names) == sorted(expected_volume_names), (
        f"Backup {backup.name} included volumes {actual_volume_names}, "
        f"expected {expected_volume_names}: {included_volumes}"
    )
    if expected_backup_type is not None:
        assert backup_status["type"] == expected_backup_type, (
            f"Backup {backup.name} type is {backup_status['type']!r}, expected {expected_backup_type!r}"
        )


def wait_for_vm_cbt_enabled(vm: VirtualMachine) -> None:
    """Wait until changed block tracking is Enabled on the VM."""
    LOGGER.info(f"Waiting for CBT Enabled on VM {vm.name}")
    for cbt_state in TimeoutSampler(
        wait_timeout=TIMEOUT_10MIN,
        sleep=TIMEOUT_5SEC,
        func=lambda: vm.instance.status.get("changedBlockTracking", {}).get("state"),
    ):
        if cbt_state == "Enabled":
            return


def wait_for_pull_backup_export_ready(backup: VirtualMachineBackup) -> None:
    """Wait until a pull-mode backup export is ready for collection."""
    # Pull readiness is Progressing=True with reason ExportReady (there is no ExportReady condition type).
    LOGGER.info(f"Waiting for pull-mode backup {backup.name} export to become ready")
    backup.wait_for_condition(
        condition="Progressing",
        status=backup.Condition.Status.TRUE,
        reason="ExportReady",
        timeout=TIMEOUT_10MIN,
        sleep_time=TIMEOUT_5SEC,
    )


def wait_for_pull_backup_export_deleted(*, name: str, namespace: str, client: DynamicClient) -> None:
    """Wait until the VirtualMachineExport owned by a pull-mode backup is gone."""
    export = VirtualMachineExport(name=name, namespace=namespace, client=client)
    LOGGER.info(f"Waiting for VirtualMachineExport {namespace}/{name} to be deleted")
    for export_deleted in TimeoutSampler(
        wait_timeout=TIMEOUT_10MIN,
        sleep=TIMEOUT_5SEC,
        func=lambda: not export.exists,
    ):
        if export_deleted:
            return
