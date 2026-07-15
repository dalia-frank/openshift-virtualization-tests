"""CBT backup/restore fixtures."""

import base64
import secrets
import shlex
from contextlib import ExitStack

import pytest
from ocp_resources.kubevirt import KubeVirt
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.secret import Secret
from ocp_resources.storage_profile import StorageProfile
from ocp_resources.virtual_machine import VirtualMachine
from ocp_resources.virtual_machine_backup import VirtualMachineBackup
from ocp_resources.virtual_machine_backup_tracker import VirtualMachineBackupTracker
from ocp_resources.virtual_machine_cluster_instancetype import VirtualMachineClusterInstancetype
from ocp_resources.virtual_machine_cluster_preference import VirtualMachineClusterPreference
from pyhelper_utils.shell import run_ssh_commands

from tests.storage.cbt.utils import (
    CBT_BOOT_DISK_TEST_DATA_FILE,
    CBT_DATA_DISK_DEVICE,
    CBT_DATA_DISK_MOUNT_PATH,
    CBT_DATA_DISK_TEST_DATA,
    CBT_DATA_DISK_TEST_DATA_FILE,
    CBT_ENABLED_LABEL,
    CBT_HOTPLUG_DISK_DEVICE,
    CBT_HOTPLUG_DISK_MOUNT_PATH,
    CBT_HOTPLUG_DISK_TEST_DATA,
    CBT_HOTPLUG_DISK_TEST_DATA_FILE,
    CBT_INCREMENTAL_TEST_DATA,
    CBT_INCREMENTAL_TEST_DATA_FILE,
    CBT_MULTI_INCREMENTAL_DATA_PHASE_1,
    CBT_MULTI_INCREMENTAL_DATA_PHASE_2,
    CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_1,
    CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_2,
    CBT_POST_MIGRATION_TEST_DATA,
    CBT_POST_MIGRATION_TEST_DATA_FILE,
    CBT_TEST_DATA,
    CBT_WINDOWS_INCREMENTAL_TEST_DATA,
    CBT_WINDOWS_INCREMENTAL_TEST_DATA_FILE,
    CBT_WINDOWS_TEST_DATA,
    CBT_WINDOWS_TEST_DATA_FILE,
    CBT_WINDOWS_TEST_USER_DIR,
    CONCURRENT_CBT_VM_COUNT,
    DATA_DISK_SIZE,
    UNDERSIZED_BACKUP_PVC_SIZE,
    cbt_pvc_size_with_headroom,
    cbt_resource_id,
    chown_mount_path_for_vm_user,
    create_and_collect_pull_mode_backup,
    get_vm_disk_volume_names,
    mount_cbt_data_disk_on_vm,
    mount_cbt_hotplug_disk_on_vm,
    restore_and_start_multi_volume_vm_from_backup,
    restore_and_start_vm_from_pull_client_backup,
    restore_and_start_vm_from_push_backup,
    restore_vm_from_backup,
    wait_for_backup_to_fail,
)
from tests.utils import create_windows2022_dv_from_registry
from utilities.constants.images import OS_FLAVOR_RHEL, OS_FLAVOR_WIN_CONTAINER_DISK
from utilities.constants.instance_types import RHEL9_PREFERENCE, U1_LARGE, U1_SMALL, WINDOWS_2K22_PREFERENCE
from utilities.constants.timeouts import TIMEOUT_2MIN, TIMEOUT_5SEC, TIMEOUT_10MIN, TIMEOUT_30MIN
from utilities.constants.virt import DV_DISK
from utilities.hco import ResourceEditorValidateHCOReconcile
from utilities.storage import (
    add_dv_to_vm,
    create_dv,
    data_volume_template_with_source_ref_dict,
    virtctl_volume,
    wait_for_vm_volume_ready,
    write_file_via_ssh,
    write_file_windows_vm,
)
from utilities.virt import VirtualMachineForTests, migrate_vm_and_verify, running_vm, wait_for_windows_vm


@pytest.fixture(scope="module")
def cbt_hco_configured(
    admin_client,
    hco_namespace,
    hyperconverged_resource_scope_module,
):
    """
    Enable incremental backup and CBT VM label selectors in HyperConverged CR.

    Yields while both settings remain configured.
    """
    with ResourceEditorValidateHCOReconcile(
        patches={
            hyperconverged_resource_scope_module: {
                "spec": {
                    "featureGates": {"incrementalBackup": True},
                    "changedBlockTrackingLabelSelectors": {
                        "virtualMachineLabelSelector": {"matchLabels": CBT_ENABLED_LABEL},
                    },
                },
            },
        },
        list_resource_reconcile=[KubeVirt],
        wait_for_reconcile_post_update=True,
        admin_client=admin_client,
        hco_namespace=hco_namespace.name,
    ):
        yield


@pytest.fixture()
def vm_with_cbt_label(
    request,
    unprivileged_client,
    namespace,
    cbt_hco_configured,
    storage_class_name_scope_module,
    rhel9_data_source_scope_session,
):
    """
    VM with CBT enabled, started, and test data written.

    Returns:
        VirtualMachine: Running VM with CBT enabled and test data written
    """
    with VirtualMachineForTests(
        name=f"{request.param['name']}-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        vm_instance_type=VirtualMachineClusterInstancetype(client=unprivileged_client, name=U1_SMALL),
        vm_preference=VirtualMachineClusterPreference(client=unprivileged_client, name=RHEL9_PREFERENCE),
        data_volume_template=data_volume_template_with_source_ref_dict(
            data_source=rhel9_data_source_scope_session,
            storage_class=storage_class_name_scope_module,
        ),
        os_flavor=OS_FLAVOR_RHEL,
        label=CBT_ENABLED_LABEL,
    ) as vm:
        running_vm(vm=vm)
        write_file_via_ssh(vm=vm, filename=CBT_BOOT_DISK_TEST_DATA_FILE, content=CBT_TEST_DATA)
        yield vm


@pytest.fixture()
def backup_tracker_for_vm(
    unprivileged_client,
    namespace,
    vm_with_cbt_label,
):
    """
    VirtualMachineBackupTracker for the VM.

    Returns:
        VirtualMachineBackupTracker: Backup tracker for the VM
    """
    with VirtualMachineBackupTracker(
        name=f"{vm_with_cbt_label.name}-tracker",
        namespace=namespace.name,
        client=unprivileged_client,
        source={
            "apiGroup": VirtualMachine.api_group,
            "kind": VirtualMachine.kind,
            "name": vm_with_cbt_label.name,
        },
    ) as tracker:
        yield tracker


@pytest.fixture()
def backup_tracker_source(backup_tracker_for_vm):
    """
    VirtualMachineBackup.spec.source reference for the VM backup tracker.

    Returns:
        dict: Backup tracker source reference
    """
    return {
        "apiGroup": VirtualMachineBackupTracker.api_group,
        "kind": VirtualMachineBackupTracker.kind,
        "name": backup_tracker_for_vm.name,
    }


@pytest.fixture()
def vm_boot_disk_size(vm_with_cbt_label):
    """
    Boot disk size request from the under-test VM data volume template.

    Returns:
        str: Boot disk storage request (e.g. 30Gi)
    """
    return vm_with_cbt_label.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]


@pytest.fixture(scope="module")
def vm_boot_pvc_spec(admin_client, storage_class_name_scope_module):
    """
    Default volume mode and access mode for the test storage class.

    Reads the first claimPropertySet from the CDI StorageProfile, which reflects
    what the cluster applies when a PVC is created without explicit volumeMode or
    accessModes (i.e. our data volume templates).

    Returns:
        dict: PVC spec with 'volume_mode' (str) and 'access_mode' (str)
    """
    profile = StorageProfile(
        name=storage_class_name_scope_module,
        client=admin_client,
    )
    return {
        "volume_mode": profile.first_claim_property_set_volume_mode(),
        "access_mode": profile.first_claim_property_set_access_modes()[0],
    }


# Push mode fixtures


@pytest.fixture()
def backup_pvc(
    unprivileged_client,
    namespace,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    PVC for storing backup output (push mode).

    Returns:
        PersistentVolumeClaim: PVC for backup storage
    """
    with PersistentVolumeClaim(
        name=f"cbt-backup-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=vm_boot_disk_size),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def completed_full_backup_push_mode(
    unprivileged_client,
    namespace,
    backup_pvc,
    backup_tracker_source,
    storage_class_name_scope_module,
):
    """
    Full backup in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed backup
    """
    with VirtualMachineBackup(
        name=f"full-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        mode=VirtualMachineBackup.Mode.PUSH,
        pvc_name=backup_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_from_full_backup_push_mode(
    unprivileged_client,
    namespace,
    completed_full_backup_push_mode,
    vm_with_cbt_label,
    vm_boot_disk_size,
    vm_boot_pvc_spec,
    backup_pvc,
    storage_class_name_scope_module,
):
    """
    VM restored from full backup and started with the original VM name.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_push_backup(
        vm=vm_with_cbt_label,
        backup=completed_full_backup_push_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        backup_pvc_name=backup_pvc.name,
        **vm_boot_pvc_spec,
    )


@pytest.fixture()
def completed_incremental_backup_push_mode(
    unprivileged_client,
    namespace,
    backup_pvc,
    vm_with_cbt_label,
    completed_full_backup_push_mode,
    backup_tracker_source,
    storage_class_name_scope_module,
):
    """
    Incremental backup in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed incremental backup
    """
    write_file_via_ssh(
        vm=vm_with_cbt_label,
        filename=CBT_INCREMENTAL_TEST_DATA_FILE,
        content=CBT_INCREMENTAL_TEST_DATA,
    )
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"incr-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=backup_pvc.name,
        force_full_backup=False,
        source=backup_tracker_source,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_from_incremental_backup_push_mode(
    unprivileged_client,
    namespace,
    completed_incremental_backup_push_mode,
    vm_with_cbt_label,
    vm_boot_disk_size,
    vm_boot_pvc_spec,
    backup_pvc,
    storage_class_name_scope_module,
):
    """
    VM restored from incremental backup (push mode) and started with the original VM name.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_push_backup(
        vm=vm_with_cbt_label,
        backup=completed_incremental_backup_push_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        backup_pvc_name=backup_pvc.name,
        **vm_boot_pvc_spec,
    )


# Pull mode fixtures


@pytest.fixture()
def pull_backup_staging_pvc(
    unprivileged_client,
    namespace,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Controller-side staging PVC for pull-mode backup export.

    The backup controller mounts this PVC to stage the exported snapshot during
    the backup export window. It is ephemeral: deleted together with the
    VirtualMachineBackup CR once the client has pulled the data.

    Returns:
        PersistentVolumeClaim: Staging PVC for the pull-mode export
    """
    with PersistentVolumeClaim(
        name=f"cbt-staging-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=vm_boot_disk_size),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def pull_mode_token_secret(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    User-provided export token secret for pull-mode backup authentication.

    Pull-mode backups require a user-generated token in tokenSecretRef; the export
    endpoints authorize external clients using this secret value.

    Returns:
        Secret: Pull-mode token secret
    """
    with Secret(
        name=f"cbt-pull-token-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        string_data={"token": secrets.token_urlsafe(nbytes=16)},
    ) as secret:
        yield secret


@pytest.fixture()
def pull_client_backup_pvc(
    unprivileged_client,
    namespace,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    PVC simulating off-site client storage for pull-mode backup data.

    Sized for a full raw snapshot plus an incremental snapshot. Pull-mode
    incremental collection seeds the new checkpoint by copying the previous
    raw file before downloading changed blocks.

    Returns:
        PersistentVolumeClaim: Client-side backup storage PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-pull-client-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=vm_boot_disk_size, backup_copies=2),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def collected_full_backup_pull_mode(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc,
    pull_mode_token_secret,
    pull_client_backup_pvc,
    vm_boot_disk_size,
    backup_tracker_source,
    storage_class_name_scope_module,
):
    """
    Full pull-mode backup collected to client storage with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"full-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret.name,
        export_token=base64.b64decode(pull_mode_token_secret.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc.name,
        client_backup_pvc_name=pull_client_backup_pvc.name,
        backup_tracker_source=backup_tracker_source,
        force_full_backup=True,
        boot_disk_size=vm_boot_disk_size,
    )
    yield pull_client_backup_pvc.name


@pytest.fixture()
def restored_vm_from_full_backup_pull_mode(
    unprivileged_client,
    namespace,
    collected_full_backup_pull_mode,
    vm_with_cbt_label,
    vm_boot_disk_size,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    VM restored from collected pull-mode client storage and started with the original VM name.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_pull_client_backup(
        vm=vm_with_cbt_label,
        client_backup_pvc_name=collected_full_backup_pull_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        **vm_boot_pvc_spec,
    )


@pytest.fixture()
def collected_incremental_backup_pull_mode(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc,
    pull_mode_token_secret,
    pull_client_backup_pvc,
    vm_with_cbt_label,
    vm_boot_disk_size,
    collected_full_backup_pull_mode,
    backup_tracker_source,
    storage_class_name_scope_module,
):
    """
    Incremental pull-mode backup collected to client storage with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline full and incremental pull backup data
    """
    write_file_via_ssh(
        vm=vm_with_cbt_label,
        filename=CBT_INCREMENTAL_TEST_DATA_FILE,
        content=CBT_INCREMENTAL_TEST_DATA,
    )
    create_and_collect_pull_mode_backup(
        name=f"incr-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret.name,
        export_token=base64.b64decode(pull_mode_token_secret.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc.name,
        client_backup_pvc_name=pull_client_backup_pvc.name,
        backup_tracker_source=backup_tracker_source,
        force_full_backup=False,
        boot_disk_size=vm_boot_disk_size,
    )
    yield collected_full_backup_pull_mode


@pytest.fixture()
def restored_vm_from_incremental_backup_pull_mode(
    unprivileged_client,
    namespace,
    collected_incremental_backup_pull_mode,
    vm_with_cbt_label,
    vm_boot_disk_size,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    VM restored from collected incremental pull-mode client storage and started with the original VM name.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_pull_client_backup(
        vm=vm_with_cbt_label,
        client_backup_pvc_name=collected_incremental_backup_pull_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        **vm_boot_pvc_spec,
    )


# Multiple incremental backup fixtures (push mode)


@pytest.fixture()
def multi_incremental_phase_1_data_written(
    vm_with_cbt_label,
    completed_full_backup_push_mode,
):
    """
    Phase 1 multi-incremental test data written after full backup (push mode).

    Returns:
        VirtualMachineForTests: VM with phase 1 test data written
    """
    write_file_via_ssh(
        vm=vm_with_cbt_label,
        filename=CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_1,
        content=CBT_MULTI_INCREMENTAL_DATA_PHASE_1,
    )
    yield vm_with_cbt_label


@pytest.fixture()
def completed_first_incremental_backup_push_mode(
    unprivileged_client,
    namespace,
    backup_pvc,
    backup_tracker_source,
    multi_incremental_phase_1_data_written,
    storage_class_name_scope_module,
):
    """
    First incremental backup in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed first incremental backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"first-incr-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=backup_pvc.name,
        force_full_backup=False,
        source=backup_tracker_source,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def multi_incremental_phase_2_data_written(
    vm_with_cbt_label,
    completed_first_incremental_backup_push_mode,
):
    """
    Phase 2 multi-incremental test data written after first incremental backup (push mode).

    Returns:
        VirtualMachineForTests: VM with phase 2 test data written
    """
    write_file_via_ssh(
        vm=vm_with_cbt_label,
        filename=CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_2,
        content=CBT_MULTI_INCREMENTAL_DATA_PHASE_2,
    )
    yield vm_with_cbt_label


@pytest.fixture()
def completed_second_incremental_backup_push_mode(
    unprivileged_client,
    namespace,
    backup_pvc,
    backup_tracker_source,
    multi_incremental_phase_2_data_written,
    storage_class_name_scope_module,
):
    """
    Second incremental backup in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed second incremental backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"second-incr-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=backup_pvc.name,
        force_full_backup=False,
        source=backup_tracker_source,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_from_second_incremental_backup_push_mode(
    unprivileged_client,
    namespace,
    completed_second_incremental_backup_push_mode,
    vm_with_cbt_label,
    vm_boot_disk_size,
    vm_boot_pvc_spec,
    backup_pvc,
    storage_class_name_scope_module,
):
    """
    VM restored from second incremental backup (push mode) and started with the original VM name.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_push_backup(
        vm=vm_with_cbt_label,
        backup=completed_second_incremental_backup_push_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        backup_pvc_name=backup_pvc.name,
        **vm_boot_pvc_spec,
    )


# Multiple incremental backup fixtures (pull mode)


@pytest.fixture()
def pull_client_backup_pvc_multi_incremental(
    unprivileged_client,
    namespace,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Client PVC sized for a full pull backup plus two incremental checkpoints.

    Returns:
        PersistentVolumeClaim: Client-side backup storage PVC for multi-incremental pull tests
    """
    with PersistentVolumeClaim(
        name=f"cbt-pull-client-multi-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=vm_boot_disk_size, backup_copies=3),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def collected_full_backup_pull_mode_multi_incremental(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc,
    pull_mode_token_secret,
    pull_client_backup_pvc_multi_incremental,
    vm_boot_disk_size,
    backup_tracker_source,
    storage_class_name_scope_module,
):
    """
    Full pull-mode backup collected to multi-incremental client storage with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"full-pull-multi-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret.name,
        export_token=base64.b64decode(pull_mode_token_secret.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc.name,
        client_backup_pvc_name=pull_client_backup_pvc_multi_incremental.name,
        backup_tracker_source=backup_tracker_source,
        force_full_backup=True,
        boot_disk_size=vm_boot_disk_size,
    )
    yield pull_client_backup_pvc_multi_incremental.name


@pytest.fixture()
def multi_incremental_phase_1_data_written_pull_mode(
    vm_with_cbt_label,
    collected_full_backup_pull_mode_multi_incremental,
):
    """
    Phase 1 multi-incremental test data written after full backup (pull mode).

    Returns:
        VirtualMachineForTests: VM with phase 1 test data written
    """
    write_file_via_ssh(
        vm=vm_with_cbt_label,
        filename=CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_1,
        content=CBT_MULTI_INCREMENTAL_DATA_PHASE_1,
    )
    yield vm_with_cbt_label


@pytest.fixture()
def collected_first_incremental_backup_pull_mode(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc,
    pull_mode_token_secret,
    pull_client_backup_pvc_multi_incremental,
    vm_boot_disk_size,
    multi_incremental_phase_1_data_written_pull_mode,
    collected_full_backup_pull_mode_multi_incremental,
    backup_tracker_source,
    storage_class_name_scope_module,
):
    """
    First incremental pull-mode backup collected to client storage with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline full and first incremental pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"first-incr-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret.name,
        export_token=base64.b64decode(pull_mode_token_secret.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc.name,
        client_backup_pvc_name=pull_client_backup_pvc_multi_incremental.name,
        backup_tracker_source=backup_tracker_source,
        force_full_backup=False,
        boot_disk_size=vm_boot_disk_size,
    )
    yield collected_full_backup_pull_mode_multi_incremental


@pytest.fixture()
def multi_incremental_phase_2_data_written_pull_mode(
    vm_with_cbt_label,
    collected_first_incremental_backup_pull_mode,
):
    """
    Phase 2 multi-incremental test data written after first incremental backup (pull mode).

    Returns:
        VirtualMachineForTests: VM with phase 2 test data written
    """
    write_file_via_ssh(
        vm=vm_with_cbt_label,
        filename=CBT_MULTI_INCREMENTAL_TEST_DATA_FILE_PHASE_2,
        content=CBT_MULTI_INCREMENTAL_DATA_PHASE_2,
    )
    yield vm_with_cbt_label


@pytest.fixture()
def collected_second_incremental_backup_pull_mode(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc,
    pull_mode_token_secret,
    pull_client_backup_pvc_multi_incremental,
    vm_boot_disk_size,
    multi_incremental_phase_2_data_written_pull_mode,
    collected_first_incremental_backup_pull_mode,
    backup_tracker_source,
    storage_class_name_scope_module,
):
    """
    Second incremental pull-mode backup collected to client storage with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline full and both incremental pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"second-incr-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret.name,
        export_token=base64.b64decode(pull_mode_token_secret.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc.name,
        client_backup_pvc_name=pull_client_backup_pvc_multi_incremental.name,
        backup_tracker_source=backup_tracker_source,
        force_full_backup=False,
        boot_disk_size=vm_boot_disk_size,
    )
    yield collected_first_incremental_backup_pull_mode


@pytest.fixture()
def restored_vm_from_second_incremental_backup_pull_mode(
    unprivileged_client,
    namespace,
    collected_second_incremental_backup_pull_mode,
    vm_with_cbt_label,
    vm_boot_disk_size,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    VM restored from second incremental pull-mode client storage and started with the original VM name.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_pull_client_backup(
        vm=vm_with_cbt_label,
        client_backup_pvc_name=collected_second_incremental_backup_pull_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        **vm_boot_pvc_spec,
    )


# Negative backup fixtures


@pytest.fixture()
def undersized_backup_pvc(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    Undersized PVC for negative push-mode backup testing.

    Returns:
        PersistentVolumeClaim: 1Gi backup PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-undersized-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=UNDERSIZED_BACKUP_PVC_SIZE,
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def failed_full_backup_push_mode(
    unprivileged_client,
    namespace,
    backup_tracker_source,
    undersized_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Failed full backup in push mode due to undersized PVC.

    Returns:
        VirtualMachineBackup: Failed backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"failed-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=undersized_backup_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source,
    ) as backup:
        wait_for_backup_to_fail(backup=backup, timeout=TIMEOUT_10MIN)
        yield backup


@pytest.fixture()
def undersized_pull_backup_staging_pvc(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    Undersized staging PVC for negative pull-mode backup testing.

    Returns:
        PersistentVolumeClaim: 1Gi staging PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-undersized-staging-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=UNDERSIZED_BACKUP_PVC_SIZE,
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def failed_full_backup_pull_mode(
    unprivileged_client,
    namespace,
    backup_tracker_source,
    undersized_pull_backup_staging_pvc,
    pull_mode_token_secret,
    storage_class_name_scope_module,
):
    """
    Failed full backup in pull mode due to undersized staging PVC.

    Returns:
        VirtualMachineBackup: Failed backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PULL,
        name=f"failed-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_ref=pull_mode_token_secret.name,
        pvc_name=undersized_pull_backup_staging_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source,
    ) as backup:
        wait_for_backup_to_fail(backup=backup, timeout=TIMEOUT_10MIN)
        yield backup


# Concurrent backup fixtures


@pytest.fixture()
def five_cbt_vms_with_test_data(
    unprivileged_client,
    namespace,
    cbt_hco_configured,
    storage_class_name_scope_module,
    rhel9_data_source_scope_session,
):
    """
    Five running VMs with CBT enabled and test data written.

    Returns:
        list[VirtualMachineForTests]: Running VMs with test data
    """
    resource_id = cbt_resource_id(name=storage_class_name_scope_module)
    vms: list[VirtualMachineForTests] = []
    with ExitStack() as stack:
        for vm_index in range(CONCURRENT_CBT_VM_COUNT):
            vm = stack.enter_context(
                VirtualMachineForTests(
                    name=f"cbt-concurrent-{vm_index}-{resource_id}",
                    namespace=namespace.name,
                    client=unprivileged_client,
                    vm_instance_type=VirtualMachineClusterInstancetype(client=unprivileged_client, name=U1_SMALL),
                    vm_preference=VirtualMachineClusterPreference(client=unprivileged_client, name=RHEL9_PREFERENCE),
                    data_volume_template=data_volume_template_with_source_ref_dict(
                        data_source=rhel9_data_source_scope_session,
                        storage_class=storage_class_name_scope_module,
                    ),
                    os_flavor=OS_FLAVOR_RHEL,
                    label=CBT_ENABLED_LABEL,
                )
            )
            running_vm(vm=vm)
            write_file_via_ssh(vm=vm, filename=CBT_BOOT_DISK_TEST_DATA_FILE, content=CBT_TEST_DATA)
            vms.append(vm)
        yield vms


@pytest.fixture()
def five_backup_trackers(
    unprivileged_client,
    namespace,
    five_cbt_vms_with_test_data,
):
    """
    Backup trackers for five concurrent CBT VMs.

    Returns:
        list[VirtualMachineBackupTracker]: Backup trackers
    """
    trackers: list[VirtualMachineBackupTracker] = []
    with ExitStack() as stack:
        for vm in five_cbt_vms_with_test_data:
            tracker = stack.enter_context(
                VirtualMachineBackupTracker(
                    name=f"{vm.name}-tracker",
                    namespace=namespace.name,
                    client=unprivileged_client,
                    source={
                        "apiGroup": VirtualMachine.api_group,
                        "kind": VirtualMachine.kind,
                        "name": vm.name,
                    },
                )
            )
            trackers.append(tracker)
        yield trackers


@pytest.fixture()
def five_backup_pvcs(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
    five_cbt_vms_with_test_data,
):
    """
    Backup PVCs for five concurrent CBT VMs.

    Returns:
        list[PersistentVolumeClaim]: Backup PVCs
    """
    pvcs: list[PersistentVolumeClaim] = []
    with ExitStack() as stack:
        for vm in five_cbt_vms_with_test_data:
            boot_disk_size = vm.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]
            pvc = stack.enter_context(
                PersistentVolumeClaim(
                    name=f"{vm.name}-backup",
                    namespace=namespace.name,
                    client=unprivileged_client,
                    accessmodes=PersistentVolumeClaim.AccessMode.RWO,
                    size=cbt_pvc_size_with_headroom(source_disk_size=boot_disk_size),
                    storage_class=storage_class_name_scope_module,
                    volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
                )
            )
            pvcs.append(pvc)
        yield pvcs


@pytest.fixture()
def five_completed_full_backups_push_mode(
    unprivileged_client,
    namespace,
    five_backup_trackers,
    five_backup_pvcs,
):
    """
    Full backups in push mode for five concurrent VMs, all completed.

    Returns:
        list[VirtualMachineBackup]: Completed backups
    """
    backups: list[VirtualMachineBackup] = []
    with ExitStack() as stack:
        for tracker, pvc in zip(five_backup_trackers, five_backup_pvcs, strict=True):
            backup = stack.enter_context(
                VirtualMachineBackup(
                    mode=VirtualMachineBackup.Mode.PUSH,
                    name=f"{tracker.name}-full-push",
                    namespace=namespace.name,
                    client=unprivileged_client,
                    pvc_name=pvc.name,
                    force_full_backup=True,
                    source={
                        "apiGroup": VirtualMachineBackupTracker.api_group,
                        "kind": VirtualMachineBackupTracker.kind,
                        "name": tracker.name,
                    },
                )
            )
            backup.wait_for_condition(
                condition="Complete",
                status=VirtualMachineBackup.Condition.Status.TRUE,
                timeout=TIMEOUT_10MIN,
                sleep_time=TIMEOUT_5SEC,
            )
            backups.append(backup)
        yield backups


@pytest.fixture()
def five_restored_vms_push_mode(
    unprivileged_client,
    namespace,
    five_completed_full_backups_push_mode,
    five_cbt_vms_with_test_data,
    five_backup_pvcs,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    Five VMs restored from full backups (push mode) and started.

    Returns:
        list[VirtualMachineForTests]: Running restored VMs
    """
    restored_vms: list[VirtualMachineForTests] = []
    restore_generators = []
    try:
        for vm, backup, backup_pvc_for_vm in zip(
            five_cbt_vms_with_test_data,
            five_completed_full_backups_push_mode,
            five_backup_pvcs,
            strict=True,
        ):
            boot_disk_size = vm.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]
            restore_generator = restore_and_start_vm_from_push_backup(
                vm=vm,
                backup=backup,
                namespace=namespace.name,
                client=unprivileged_client,
                storage_class=storage_class_name_scope_module,
                size=boot_disk_size,
                backup_pvc_name=backup_pvc_for_vm.name,
                **vm_boot_pvc_spec,
            )
            restored_vms.append(next(restore_generator))
            restore_generators.append(restore_generator)
        yield restored_vms
    finally:
        for restore_generator in restore_generators:
            restore_generator.close()


@pytest.fixture()
def five_pull_backup_staging_pvcs(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
    five_cbt_vms_with_test_data,
):
    """
    Staging PVCs for five concurrent pull-mode backups.

    Returns:
        list[PersistentVolumeClaim]: Staging PVCs
    """
    pvcs: list[PersistentVolumeClaim] = []
    with ExitStack() as stack:
        for vm in five_cbt_vms_with_test_data:
            boot_disk_size = vm.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]
            pvc = stack.enter_context(
                PersistentVolumeClaim(
                    name=f"{vm.name}-staging",
                    namespace=namespace.name,
                    client=unprivileged_client,
                    accessmodes=PersistentVolumeClaim.AccessMode.RWO,
                    size=cbt_pvc_size_with_headroom(source_disk_size=boot_disk_size),
                    storage_class=storage_class_name_scope_module,
                    volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
                )
            )
            pvcs.append(pvc)
        yield pvcs


@pytest.fixture()
def five_pull_client_backup_pvcs(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
    five_cbt_vms_with_test_data,
):
    """
    Client PVCs for five concurrent pull-mode backups.

    Returns:
        list[PersistentVolumeClaim]: Client backup PVCs
    """
    pvcs: list[PersistentVolumeClaim] = []
    with ExitStack() as stack:
        for vm in five_cbt_vms_with_test_data:
            boot_disk_size = vm.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]
            pvc = stack.enter_context(
                PersistentVolumeClaim(
                    name=f"{vm.name}-pull-client",
                    namespace=namespace.name,
                    client=unprivileged_client,
                    accessmodes=PersistentVolumeClaim.AccessMode.RWO,
                    size=cbt_pvc_size_with_headroom(source_disk_size=boot_disk_size, backup_copies=2),
                    storage_class=storage_class_name_scope_module,
                    volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
                )
            )
            pvcs.append(pvc)
        yield pvcs


@pytest.fixture()
def five_collected_full_backups_pull_mode(
    unprivileged_client,
    namespace,
    five_backup_trackers,
    five_pull_backup_staging_pvcs,
    five_pull_client_backup_pvcs,
    pull_mode_token_secret,
    five_cbt_vms_with_test_data,
    storage_class_name_scope_module,
):
    """
    Full pull-mode backups collected for five concurrent VMs with backup CRs deleted.

    Returns:
        list[str]: Client PVC names containing offline pull backup data
    """
    export_token = base64.b64decode(pull_mode_token_secret.instance.data["token"]).decode("utf-8")
    client_backup_pvc_names: list[str] = []
    for tracker, staging_pvc, client_pvc, vm in zip(
        five_backup_trackers,
        five_pull_backup_staging_pvcs,
        five_pull_client_backup_pvcs,
        five_cbt_vms_with_test_data,
        strict=True,
    ):
        boot_disk_size = vm.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]
        create_and_collect_pull_mode_backup(
            name=f"{tracker.name}-full-pull",
            namespace=namespace.name,
            client=unprivileged_client,
            token_secret_name=pull_mode_token_secret.name,
            export_token=export_token,
            staging_pvc_name=staging_pvc.name,
            client_backup_pvc_name=client_pvc.name,
            backup_tracker_source={
                "apiGroup": VirtualMachineBackupTracker.api_group,
                "kind": VirtualMachineBackupTracker.kind,
                "name": tracker.name,
            },
            force_full_backup=True,
            boot_disk_size=boot_disk_size,
        )
        client_backup_pvc_names.append(client_pvc.name)
    yield client_backup_pvc_names


@pytest.fixture()
def five_restored_vms_pull_mode(
    unprivileged_client,
    namespace,
    five_collected_full_backups_pull_mode,
    five_cbt_vms_with_test_data,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    Five VMs restored from collected pull-mode backups and started.

    Returns:
        list[VirtualMachineForTests]: Running restored VMs
    """
    restored_vms: list[VirtualMachineForTests] = []
    restore_generators = []
    try:
        for vm, client_backup_pvc_name in zip(
            five_cbt_vms_with_test_data,
            five_collected_full_backups_pull_mode,
            strict=True,
        ):
            boot_disk_size = vm.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]
            restore_generator = restore_and_start_vm_from_pull_client_backup(
                vm=vm,
                client_backup_pvc_name=client_backup_pvc_name,
                namespace=namespace.name,
                client=unprivileged_client,
                storage_class=storage_class_name_scope_module,
                size=boot_disk_size,
                **vm_boot_pvc_spec,
            )
            restored_vms.append(next(restore_generator))
            restore_generators.append(restore_generator)
        yield restored_vms
    finally:
        for restore_generator in restore_generators:
            restore_generator.close()


# Live migration backup fixtures


@pytest.fixture()
def vm_with_cbt_on_rwx_storage(
    unprivileged_client,
    namespace,
    cbt_hco_configured,
    storage_class_name_scope_module,
    rhel9_data_source_scope_session,
):
    """
    VM with CBT enabled on RWX storage, started, and test data written.

    Returns:
        VirtualMachineForTests: Running VM with CBT enabled on RWX storage
    """
    resource_id = cbt_resource_id(name=storage_class_name_scope_module)
    with VirtualMachineForTests(
        name=f"cbt-rwx-migration-{resource_id}",
        namespace=namespace.name,
        client=unprivileged_client,
        vm_instance_type=VirtualMachineClusterInstancetype(client=unprivileged_client, name=U1_SMALL),
        vm_preference=VirtualMachineClusterPreference(client=unprivileged_client, name=RHEL9_PREFERENCE),
        data_volume_template=data_volume_template_with_source_ref_dict(
            data_source=rhel9_data_source_scope_session,
            storage_class=storage_class_name_scope_module,
        ),
        os_flavor=OS_FLAVOR_RHEL,
        label=CBT_ENABLED_LABEL,
    ) as vm:
        running_vm(vm=vm)
        write_file_via_ssh(vm=vm, filename=CBT_BOOT_DISK_TEST_DATA_FILE, content=CBT_TEST_DATA)
        yield vm


@pytest.fixture()
def rwx_vm_boot_disk_size(vm_with_cbt_on_rwx_storage):
    """
    Boot disk size request from the RWX migration VM data volume template.

    Returns:
        str: Boot disk storage request (e.g. 30Gi)
    """
    return vm_with_cbt_on_rwx_storage.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]


@pytest.fixture()
def backup_tracker_for_rwx_vm(
    unprivileged_client,
    namespace,
    vm_with_cbt_on_rwx_storage,
):
    """
    VirtualMachineBackupTracker for the RWX migration VM.

    Returns:
        VirtualMachineBackupTracker: Backup tracker for the RWX VM
    """
    with VirtualMachineBackupTracker(
        name=f"{vm_with_cbt_on_rwx_storage.name}-tracker",
        namespace=namespace.name,
        client=unprivileged_client,
        source={
            "apiGroup": VirtualMachine.api_group,
            "kind": VirtualMachine.kind,
            "name": vm_with_cbt_on_rwx_storage.name,
        },
    ) as tracker:
        yield tracker


@pytest.fixture()
def backup_tracker_source_rwx_vm(backup_tracker_for_rwx_vm):
    """
    VirtualMachineBackup.spec.source reference for the RWX VM backup tracker.

    Returns:
        dict: Backup tracker source reference
    """
    return {
        "apiGroup": VirtualMachineBackupTracker.api_group,
        "kind": VirtualMachineBackupTracker.kind,
        "name": backup_tracker_for_rwx_vm.name,
    }


@pytest.fixture()
def backup_pvc_rwx_vm(
    unprivileged_client,
    namespace,
    rwx_vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Backup PVC for the RWX migration VM (push mode).

    Returns:
        PersistentVolumeClaim: Backup PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-rwx-backup-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=rwx_vm_boot_disk_size),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def completed_full_backup_before_migration_push(
    unprivileged_client,
    namespace,
    backup_pvc_rwx_vm,
    backup_tracker_source_rwx_vm,
    storage_class_name_scope_module,
):
    """
    Full backup of RWX VM before migration in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed full backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"full-pre-mig-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=backup_pvc_rwx_vm.name,
        force_full_backup=True,
        source=backup_tracker_source_rwx_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def migrated_vm_with_post_migration_data(
    admin_client,
    vm_with_cbt_on_rwx_storage,
    completed_full_backup_before_migration_push,
):
    """
    RWX VM live-migrated with post-migration test data written (push backup chain).

    Returns:
        VirtualMachineForTests: Migrated VM with post-migration test data
    """
    migrate_vm_and_verify(vm=vm_with_cbt_on_rwx_storage, client=admin_client, check_ssh_connectivity=True)
    write_file_via_ssh(
        vm=vm_with_cbt_on_rwx_storage,
        filename=CBT_POST_MIGRATION_TEST_DATA_FILE,
        content=CBT_POST_MIGRATION_TEST_DATA,
    )
    yield vm_with_cbt_on_rwx_storage


@pytest.fixture()
def completed_incremental_backup_after_migration_push(
    unprivileged_client,
    namespace,
    backup_pvc_rwx_vm,
    backup_tracker_source_rwx_vm,
    migrated_vm_with_post_migration_data,
    storage_class_name_scope_module,
):
    """
    Incremental backup after live migration in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed incremental backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"incr-post-mig-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=backup_pvc_rwx_vm.name,
        force_full_backup=False,
        source=backup_tracker_source_rwx_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_after_migration_incremental_push(
    unprivileged_client,
    namespace,
    completed_incremental_backup_after_migration_push,
    vm_with_cbt_on_rwx_storage,
    rwx_vm_boot_disk_size,
    vm_boot_pvc_spec,
    backup_pvc_rwx_vm,
    storage_class_name_scope_module,
):
    """
    RWX VM restored from post-migration incremental backup (push mode) and started.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_push_backup(
        vm=vm_with_cbt_on_rwx_storage,
        backup=completed_incremental_backup_after_migration_push,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=rwx_vm_boot_disk_size,
        backup_pvc_name=backup_pvc_rwx_vm.name,
        **vm_boot_pvc_spec,
    )


@pytest.fixture()
def pull_backup_staging_pvc_rwx_vm(
    unprivileged_client,
    namespace,
    rwx_vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Staging PVC for pull-mode backup of the RWX migration VM.

    Returns:
        PersistentVolumeClaim: Staging PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-rwx-staging-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=rwx_vm_boot_disk_size),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def pull_client_backup_pvc_rwx_vm(
    unprivileged_client,
    namespace,
    rwx_vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Client PVC for pull-mode backup of the RWX migration VM.

    Returns:
        PersistentVolumeClaim: Client backup PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-rwx-pull-client-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=rwx_vm_boot_disk_size, backup_copies=2),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def pull_mode_token_secret_rwx_vm(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    Export token secret for pull-mode backup of the RWX migration VM.

    Returns:
        Secret: Pull-mode token secret
    """
    with Secret(
        name=f"cbt-rwx-pull-token-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        string_data={"token": secrets.token_urlsafe(nbytes=16)},
    ) as secret:
        yield secret


@pytest.fixture()
def collected_full_backup_before_migration_pull(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc_rwx_vm,
    pull_mode_token_secret_rwx_vm,
    pull_client_backup_pvc_rwx_vm,
    rwx_vm_boot_disk_size,
    backup_tracker_source_rwx_vm,
    storage_class_name_scope_module,
):
    """
    Full pull-mode backup collected before migration with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"full-pre-mig-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret_rwx_vm.name,
        export_token=base64.b64decode(pull_mode_token_secret_rwx_vm.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc_rwx_vm.name,
        client_backup_pvc_name=pull_client_backup_pvc_rwx_vm.name,
        backup_tracker_source=backup_tracker_source_rwx_vm,
        force_full_backup=True,
        boot_disk_size=rwx_vm_boot_disk_size,
    )
    yield pull_client_backup_pvc_rwx_vm.name


@pytest.fixture()
def migrated_vm_with_post_migration_data_pull_mode(
    admin_client,
    vm_with_cbt_on_rwx_storage,
    collected_full_backup_before_migration_pull,
):
    """
    RWX VM live-migrated with post-migration test data written (pull backup chain).

    Returns:
        VirtualMachineForTests: Migrated VM with post-migration test data
    """
    migrate_vm_and_verify(vm=vm_with_cbt_on_rwx_storage, client=admin_client, check_ssh_connectivity=True)
    write_file_via_ssh(
        vm=vm_with_cbt_on_rwx_storage,
        filename=CBT_POST_MIGRATION_TEST_DATA_FILE,
        content=CBT_POST_MIGRATION_TEST_DATA,
    )
    yield vm_with_cbt_on_rwx_storage


@pytest.fixture()
def collected_incremental_backup_after_migration_pull(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc_rwx_vm,
    pull_mode_token_secret_rwx_vm,
    pull_client_backup_pvc_rwx_vm,
    rwx_vm_boot_disk_size,
    migrated_vm_with_post_migration_data_pull_mode,
    collected_full_backup_before_migration_pull,
    backup_tracker_source_rwx_vm,
    storage_class_name_scope_module,
):
    """
    Incremental pull-mode backup collected after migration with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline full and incremental pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"incr-post-mig-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret_rwx_vm.name,
        export_token=base64.b64decode(pull_mode_token_secret_rwx_vm.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc_rwx_vm.name,
        client_backup_pvc_name=pull_client_backup_pvc_rwx_vm.name,
        backup_tracker_source=backup_tracker_source_rwx_vm,
        force_full_backup=False,
        boot_disk_size=rwx_vm_boot_disk_size,
    )
    yield collected_full_backup_before_migration_pull


@pytest.fixture()
def restored_vm_after_migration_incremental_pull(
    unprivileged_client,
    namespace,
    collected_incremental_backup_after_migration_pull,
    vm_with_cbt_on_rwx_storage,
    rwx_vm_boot_disk_size,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    RWX VM restored from post-migration incremental pull-mode client storage and started.

    Returns:
        VirtualMachineForTests: Running restored VM
    """
    yield from restore_and_start_vm_from_pull_client_backup(
        vm=vm_with_cbt_on_rwx_storage,
        client_backup_pvc_name=collected_incremental_backup_after_migration_pull,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=rwx_vm_boot_disk_size,
        **vm_boot_pvc_spec,
    )


# Multiple disk backup fixtures


@pytest.fixture()
def data_disk_dv_for_cbt_vm(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    Blank data disk DataVolume for CBT multi-disk testing.

    Returns:
        DataVolume: Blank 5Gi DataVolume
    """
    resource_id = cbt_resource_id(name=storage_class_name_scope_module)
    with create_dv(
        source="blank",
        dv_name=f"cbt-data-disk-{resource_id}",
        client=unprivileged_client,
        namespace=namespace.name,
        size=DATA_DISK_SIZE,
        storage_class=storage_class_name_scope_module,
    ) as data_volume:
        yield data_volume


@pytest.fixture()
def vm_with_boot_and_data_disk(
    vm_with_cbt_label,
    data_disk_dv_for_cbt_vm,
):
    """
    VM with boot and data disks, data disk formatted, mounted, and test data written.

    Returns:
        VirtualMachineForTests: Running VM with test data on both disks
    """
    vm_with_cbt_label.stop(wait=True)
    add_dv_to_vm(vm=vm_with_cbt_label, dv_name=data_disk_dv_for_cbt_vm.name)
    running_vm(vm=vm_with_cbt_label)
    run_ssh_commands(
        host=vm_with_cbt_label.ssh_exec,
        commands=[
            shlex.split(f"sudo mkdir -p {CBT_DATA_DISK_MOUNT_PATH}"),
            shlex.split(f"sudo mkfs.ext4 {CBT_DATA_DISK_DEVICE}"),
            shlex.split(f"sudo mount {CBT_DATA_DISK_DEVICE} {CBT_DATA_DISK_MOUNT_PATH}"),
        ],
        wait_timeout=TIMEOUT_2MIN,
        sleep=TIMEOUT_5SEC,
    )
    chown_mount_path_for_vm_user(vm=vm_with_cbt_label, mount_path=CBT_DATA_DISK_MOUNT_PATH)
    write_file_via_ssh(
        vm=vm_with_cbt_label,
        filename=CBT_DATA_DISK_TEST_DATA_FILE,
        content=CBT_DATA_DISK_TEST_DATA,
    )
    yield vm_with_cbt_label


@pytest.fixture()
def backup_tracker_for_multi_disk_vm(
    unprivileged_client,
    namespace,
    vm_with_boot_and_data_disk,
):
    """
    VirtualMachineBackupTracker for the multi-disk VM.

    Returns:
        VirtualMachineBackupTracker: Backup tracker for the multi-disk VM
    """
    with VirtualMachineBackupTracker(
        name=f"{vm_with_boot_and_data_disk.name}-tracker",
        namespace=namespace.name,
        client=unprivileged_client,
        source={
            "apiGroup": VirtualMachine.api_group,
            "kind": VirtualMachine.kind,
            "name": vm_with_boot_and_data_disk.name,
        },
    ) as tracker:
        yield tracker


@pytest.fixture()
def backup_tracker_source_multi_disk_vm(backup_tracker_for_multi_disk_vm):
    """
    VirtualMachineBackup.spec.source reference for the multi-disk VM backup tracker.

    Returns:
        dict: Backup tracker source reference
    """
    return {
        "apiGroup": VirtualMachineBackupTracker.api_group,
        "kind": VirtualMachineBackupTracker.kind,
        "name": backup_tracker_for_multi_disk_vm.name,
    }


@pytest.fixture()
def multi_disk_backup_pvc(
    unprivileged_client,
    namespace,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Backup PVC sized for a multi-disk VM full backup (push mode).

    Returns:
        PersistentVolumeClaim: Backup PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-multi-backup-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=vm_boot_disk_size, backup_copies=2),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def completed_full_backup_multi_disk_push_mode(
    unprivileged_client,
    namespace,
    backup_tracker_source_multi_disk_vm,
    multi_disk_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Full backup of multi-disk VM in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed full backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"full-multi-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=multi_disk_backup_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source_multi_disk_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_from_multi_disk_backup_push_mode(
    admin_client,
    unprivileged_client,
    namespace,
    completed_full_backup_multi_disk_push_mode,
    vm_with_boot_and_data_disk,
    data_disk_dv_for_cbt_vm,
    vm_boot_disk_size,
    multi_disk_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Multi-disk VM restored from full backup (push mode) and started.

    Returns:
        VirtualMachineForTests: Running restored VM with boot and data disks
    """
    source_volume_names = [DV_DISK, data_disk_dv_for_cbt_vm.name]
    yield from restore_and_start_multi_volume_vm_from_backup(
        vm=vm_with_boot_and_data_disk,
        backup=completed_full_backup_multi_disk_push_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        admin_client=admin_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        backup_pvc_name=multi_disk_backup_pvc.name,
        data_disk_size=DATA_DISK_SIZE,
        source_volume_names=source_volume_names,
        mount_secondary_disk=mount_cbt_data_disk_on_vm,
    )


@pytest.fixture()
def completed_full_backup_multi_disk_pull_mode(
    unprivileged_client,
    namespace,
    backup_tracker_source_multi_disk_vm,
    pull_backup_staging_pvc,
    pull_mode_token_secret,
    storage_class_name_scope_module,
):
    """
    Full backup of multi-disk VM in pull mode with export endpoints ready.

    Returns:
        VirtualMachineBackup: Completed full backup with export endpoints ready
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PULL,
        name=f"full-multi-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_ref=pull_mode_token_secret.name,
        pvc_name=pull_backup_staging_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source_multi_disk_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Progressing",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            reason="ExportReady",
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_from_multi_disk_backup_pull_mode(
    admin_client,
    unprivileged_client,
    namespace,
    completed_full_backup_multi_disk_pull_mode,
    vm_with_boot_and_data_disk,
    data_disk_dv_for_cbt_vm,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Multi-disk VM restored from full backup (pull mode) and started.

    Returns:
        VirtualMachineForTests: Running restored VM with boot and data disks
    """
    source_volume_names = [DV_DISK, data_disk_dv_for_cbt_vm.name]
    restored_vm_name = vm_with_boot_and_data_disk.name
    if restored_vm_name is None:
        raise RuntimeError("Cannot restore: source VM has no name")
    vm_instance_type_name = vm_with_boot_and_data_disk.instance.spec["instancetype"]["name"]
    vm_preference_name = vm_with_boot_and_data_disk.instance.spec["preference"]["name"]
    os_flavor = vm_with_boot_and_data_disk.os_flavor

    restored_vm = restore_vm_from_backup(
        backup=completed_full_backup_multi_disk_pull_mode,
        restored_vm_name=restored_vm_name,
        namespace=namespace.name,
        client=unprivileged_client,
        admin_client=admin_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        data_disk_size=DATA_DISK_SIZE,
        source_volume_names=source_volume_names,
        os_flavor=os_flavor,
        vm_preference_name=vm_preference_name,
        vm_instance_type_name=vm_instance_type_name,
    )
    vm_with_boot_and_data_disk.delete(wait=True)
    vm_with_boot_and_data_disk.teardown = False

    running_vm(vm=restored_vm)
    mount_cbt_data_disk_on_vm(vm=restored_vm)
    try:
        yield restored_vm
    finally:
        restored_vm.delete(wait=True)


# Hotplug backup fixtures


@pytest.fixture(scope="module")
def declarative_hotplug_volumes_feature_gate_enabled(
    hyperconverged_resource_scope_module,
    admin_client,
    hco_namespace,
):
    """
    Enable declarativeHotplugVolumes feature gate in HyperConverged CR.

    Yields while the feature gate remains enabled.
    """
    with ResourceEditorValidateHCOReconcile(
        patches={
            hyperconverged_resource_scope_module: {"spec": {"featureGates": {"declarativeHotplugVolumes": True}}},
        },
        list_resource_reconcile=[KubeVirt],
        wait_for_reconcile_post_update=True,
        admin_client=admin_client,
        hco_namespace=hco_namespace.name,
    ):
        yield


@pytest.fixture()
def blank_hotplug_disk_dv(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    Blank DataVolume for hotplug disk testing.

    Returns:
        DataVolume: Blank DataVolume for hotplug
    """
    resource_id = cbt_resource_id(name=storage_class_name_scope_module)
    with create_dv(
        source="blank",
        dv_name=f"cbt-hotplug-{resource_id}",
        client=unprivileged_client,
        namespace=namespace.name,
        size=DATA_DISK_SIZE,
        storage_class=storage_class_name_scope_module,
    ) as data_volume:
        yield data_volume


@pytest.fixture()
def vm_with_hotplugged_disk_and_data(
    namespace,
    declarative_hotplug_volumes_feature_gate_enabled,
    vm_with_cbt_label,
    blank_hotplug_disk_dv,
):
    """
    VM with hotplugged disk mounted and test data written.

    Returns:
        VirtualMachineForTests: Running VM with hotplugged disk test data
    """
    with virtctl_volume(
        action="add",
        namespace=namespace.name,
        vm_name=vm_with_cbt_label.name,
        volume_name=blank_hotplug_disk_dv.name,
        persist=True,
    ) as hotplug_result:
        status, out, err = hotplug_result
        assert status, f"Failed to add volume to VM, out: {out}, err: {err}."
        wait_for_vm_volume_ready(
            vm=vm_with_cbt_label,
            volume_name=blank_hotplug_disk_dv.name,
        )
        run_ssh_commands(
            host=vm_with_cbt_label.ssh_exec,
            commands=[
                shlex.split(f"sudo mkdir -p {CBT_HOTPLUG_DISK_MOUNT_PATH}"),
                shlex.split(f"sudo mkfs.ext4 {CBT_HOTPLUG_DISK_DEVICE}"),
                shlex.split(f"sudo mount {CBT_HOTPLUG_DISK_DEVICE} {CBT_HOTPLUG_DISK_MOUNT_PATH}"),
            ],
            wait_timeout=TIMEOUT_2MIN,
            sleep=TIMEOUT_5SEC,
        )
        chown_mount_path_for_vm_user(vm=vm_with_cbt_label, mount_path=CBT_HOTPLUG_DISK_MOUNT_PATH)
        write_file_via_ssh(
            vm=vm_with_cbt_label,
            filename=CBT_HOTPLUG_DISK_TEST_DATA_FILE,
            content=CBT_HOTPLUG_DISK_TEST_DATA,
        )
        yield vm_with_cbt_label


@pytest.fixture()
def backup_tracker_for_hotplug_vm(
    unprivileged_client,
    namespace,
    vm_with_hotplugged_disk_and_data,
):
    """
    VirtualMachineBackupTracker for the hotplug VM.

    Returns:
        VirtualMachineBackupTracker: Backup tracker for the hotplug VM
    """
    with VirtualMachineBackupTracker(
        name=f"{vm_with_hotplugged_disk_and_data.name}-tracker",
        namespace=namespace.name,
        client=unprivileged_client,
        source={
            "apiGroup": VirtualMachine.api_group,
            "kind": VirtualMachine.kind,
            "name": vm_with_hotplugged_disk_and_data.name,
        },
    ) as tracker:
        yield tracker


@pytest.fixture()
def backup_tracker_source_hotplug_vm(backup_tracker_for_hotplug_vm):
    """
    VirtualMachineBackup.spec.source reference for the hotplug VM backup tracker.

    Returns:
        dict: Backup tracker source reference
    """
    return {
        "apiGroup": VirtualMachineBackupTracker.api_group,
        "kind": VirtualMachineBackupTracker.kind,
        "name": backup_tracker_for_hotplug_vm.name,
    }


@pytest.fixture()
def hotplug_backup_pvc(
    unprivileged_client,
    namespace,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Backup PVC sized for a hotplug VM full backup (push mode).

    Returns:
        PersistentVolumeClaim: Backup PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-hotplug-backup-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=vm_boot_disk_size, backup_copies=2),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def completed_full_backup_hotplug_push(
    unprivileged_client,
    namespace,
    backup_tracker_source_hotplug_vm,
    hotplug_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Full backup of hotplug VM in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed full backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"full-hotplug-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=hotplug_backup_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source_hotplug_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_hotplug_push(
    admin_client,
    unprivileged_client,
    namespace,
    completed_full_backup_hotplug_push,
    vm_with_hotplugged_disk_and_data,
    blank_hotplug_disk_dv,
    vm_boot_disk_size,
    hotplug_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Hotplug VM restored from full backup (push mode) and started.

    Returns:
        VirtualMachineForTests: Running restored VM with boot and hotplug disks
    """
    source_volume_names = get_vm_disk_volume_names(vm=vm_with_hotplugged_disk_and_data)
    restored_vm_name = vm_with_hotplugged_disk_and_data.name
    if restored_vm_name is None:
        raise RuntimeError("Cannot restore: source VM has no name")
    vm_with_hotplugged_disk_and_data.delete(wait=True)
    vm_with_hotplugged_disk_and_data.teardown = False
    blank_hotplug_disk_dv.delete(wait=True)

    restored_vm = restore_vm_from_backup(
        backup=completed_full_backup_hotplug_push,
        restored_vm_name=restored_vm_name,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        admin_client=admin_client,
        backup_pvc_name=hotplug_backup_pvc.name,
        data_disk_size=DATA_DISK_SIZE,
        source_volume_names=source_volume_names,
    )

    running_vm(vm=restored_vm)
    mount_cbt_hotplug_disk_on_vm(vm=restored_vm)
    try:
        yield restored_vm
    finally:
        restored_vm.delete(wait=True)


@pytest.fixture()
def completed_full_backup_hotplug_pull(
    unprivileged_client,
    namespace,
    backup_tracker_source_hotplug_vm,
    pull_backup_staging_pvc,
    pull_mode_token_secret,
    storage_class_name_scope_module,
):
    """
    Full backup of hotplug VM in pull mode with export endpoints ready.

    Returns:
        VirtualMachineBackup: Completed full backup with export endpoints ready
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PULL,
        name=f"full-hotplug-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_ref=pull_mode_token_secret.name,
        pvc_name=pull_backup_staging_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source_hotplug_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Progressing",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            reason="ExportReady",
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_hotplug_pull(
    admin_client,
    unprivileged_client,
    namespace,
    completed_full_backup_hotplug_pull,
    vm_with_hotplugged_disk_and_data,
    blank_hotplug_disk_dv,
    vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Hotplug VM restored from full backup (pull mode) and started.

    Returns:
        VirtualMachineForTests: Running restored VM with boot and hotplug disks
    """
    source_volume_names = get_vm_disk_volume_names(vm=vm_with_hotplugged_disk_and_data)
    restored_vm_name = vm_with_hotplugged_disk_and_data.name
    if restored_vm_name is None:
        raise RuntimeError("Cannot restore: source VM has no name")
    vm_instance_type_name = vm_with_hotplugged_disk_and_data.instance.spec["instancetype"]["name"]
    vm_preference_name = vm_with_hotplugged_disk_and_data.instance.spec["preference"]["name"]
    os_flavor = vm_with_hotplugged_disk_and_data.os_flavor

    restored_vm = restore_vm_from_backup(
        backup=completed_full_backup_hotplug_pull,
        restored_vm_name=restored_vm_name,
        namespace=namespace.name,
        client=unprivileged_client,
        admin_client=admin_client,
        storage_class=storage_class_name_scope_module,
        size=vm_boot_disk_size,
        data_disk_size=DATA_DISK_SIZE,
        source_volume_names=source_volume_names,
        os_flavor=os_flavor,
        vm_preference_name=vm_preference_name,
        vm_instance_type_name=vm_instance_type_name,
    )

    vm_with_hotplugged_disk_and_data.delete(wait=True)
    vm_with_hotplugged_disk_and_data.teardown = False
    blank_hotplug_disk_dv.delete(wait=True)

    running_vm(vm=restored_vm)
    mount_cbt_hotplug_disk_on_vm(vm=restored_vm)
    try:
        yield restored_vm
    finally:
        restored_vm.delete(wait=True)


# Windows VM backup fixtures


@pytest.fixture()
def windows_dv_for_cbt(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    Windows 2022 DataVolume for CBT testing.

    Returns:
        dict: DataVolume template dictionary
    """
    resource_id = cbt_resource_id(name=storage_class_name_scope_module)
    with create_windows2022_dv_from_registry(
        dv_name=f"cbt-windows-{resource_id}",
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
    ) as dv_dict:
        yield dv_dict


@pytest.fixture()
def windows_vm_with_cbt(
    unprivileged_client,
    namespace,
    windows_dv_for_cbt,
    modern_cpu_for_migration,
    storage_class_name_scope_module,
):
    """
    Windows VM with CBT enabled, started, and test data written.

    Returns:
        VirtualMachineForTests: Running Windows VM with CBT enabled
    """
    resource_id = cbt_resource_id(name=storage_class_name_scope_module)
    with VirtualMachineForTests(
        name=f"cbt-windows-{resource_id}",
        namespace=namespace.name,
        client=unprivileged_client,
        os_flavor=OS_FLAVOR_WIN_CONTAINER_DISK,
        vm_instance_type=VirtualMachineClusterInstancetype(client=unprivileged_client, name=U1_LARGE),
        vm_preference=VirtualMachineClusterPreference(client=unprivileged_client, name=WINDOWS_2K22_PREFERENCE),
        data_volume_template=windows_dv_for_cbt,
        cpu_model=modern_cpu_for_migration,
        label=CBT_ENABLED_LABEL,
    ) as vm:
        running_vm(vm=vm, ssh_timeout=TIMEOUT_30MIN)
        wait_for_windows_vm(vm=vm, version="2022")
        run_ssh_commands(
            host=vm.ssh_exec,
            commands=[
                shlex.split(
                    f'powershell -command "New-Item -ItemType Directory -Force -Path {CBT_WINDOWS_TEST_USER_DIR}"'
                ),
            ],
            wait_timeout=TIMEOUT_2MIN,
            sleep=TIMEOUT_5SEC,
        )
        write_file_windows_vm(
            vm=vm,
            file_path=CBT_WINDOWS_TEST_DATA_FILE,
            content=CBT_WINDOWS_TEST_DATA,
        )
        yield vm


@pytest.fixture()
def backup_tracker_for_windows_vm(
    unprivileged_client,
    namespace,
    windows_vm_with_cbt,
):
    """
    VirtualMachineBackupTracker for the Windows VM.

    Returns:
        VirtualMachineBackupTracker: Backup tracker for the Windows VM
    """
    with VirtualMachineBackupTracker(
        name=f"{windows_vm_with_cbt.name}-tracker",
        namespace=namespace.name,
        client=unprivileged_client,
        source={
            "apiGroup": VirtualMachine.api_group,
            "kind": VirtualMachine.kind,
            "name": windows_vm_with_cbt.name,
        },
    ) as tracker:
        yield tracker


@pytest.fixture()
def backup_tracker_source_windows_vm(backup_tracker_for_windows_vm):
    """
    VirtualMachineBackup.spec.source reference for the Windows VM backup tracker.

    Returns:
        dict: Backup tracker source reference
    """
    return {
        "apiGroup": VirtualMachineBackupTracker.api_group,
        "kind": VirtualMachineBackupTracker.kind,
        "name": backup_tracker_for_windows_vm.name,
    }


@pytest.fixture()
def windows_vm_boot_disk_size(windows_vm_with_cbt):
    """
    Boot disk size request from the Windows VM data volume template.

    Returns:
        str: Boot disk storage request
    """
    return windows_vm_with_cbt.data_volume_template["spec"]["storage"]["resources"]["requests"]["storage"]


@pytest.fixture()
def windows_backup_pvc(
    unprivileged_client,
    namespace,
    windows_vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Backup PVC for the Windows VM (push mode).

    Returns:
        PersistentVolumeClaim: Backup PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-windows-backup-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=windows_vm_boot_disk_size),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def completed_full_backup_windows_push_mode(
    unprivileged_client,
    namespace,
    backup_tracker_source_windows_vm,
    windows_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Full backup of Windows VM in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed full backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"full-windows-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=windows_backup_pvc.name,
        force_full_backup=True,
        source=backup_tracker_source_windows_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_from_full_backup_windows_push_mode(
    unprivileged_client,
    namespace,
    completed_full_backup_windows_push_mode,
    windows_vm_with_cbt,
    windows_vm_boot_disk_size,
    vm_boot_pvc_spec,
    windows_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Windows VM restored from full backup (push mode) and started with the original VM name.

    Returns:
        VirtualMachineForTests: Running restored Windows VM
    """
    restore_generator = restore_and_start_vm_from_push_backup(
        vm=windows_vm_with_cbt,
        backup=completed_full_backup_windows_push_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=windows_vm_boot_disk_size,
        backup_pvc_name=windows_backup_pvc.name,
        ssh_timeout=TIMEOUT_30MIN,
        **vm_boot_pvc_spec,
    )
    restored_vm = next(restore_generator)
    wait_for_windows_vm(vm=restored_vm, version="2022")
    try:
        yield restored_vm
    finally:
        restore_generator.close()


@pytest.fixture()
def pull_backup_staging_pvc_windows_vm(
    unprivileged_client,
    namespace,
    windows_vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Staging PVC for pull-mode backup of the Windows VM.

    Returns:
        PersistentVolumeClaim: Staging PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-windows-staging-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=windows_vm_boot_disk_size),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def pull_client_backup_pvc_windows_vm(
    unprivileged_client,
    namespace,
    windows_vm_boot_disk_size,
    storage_class_name_scope_module,
):
    """
    Client PVC for pull-mode backup of the Windows VM.

    Returns:
        PersistentVolumeClaim: Client backup PVC
    """
    with PersistentVolumeClaim(
        name=f"cbt-windows-pull-client-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=cbt_pvc_size_with_headroom(source_disk_size=windows_vm_boot_disk_size, backup_copies=2),
        storage_class=storage_class_name_scope_module,
        volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
    ) as pvc:
        yield pvc


@pytest.fixture()
def pull_mode_token_secret_windows_vm(
    unprivileged_client,
    namespace,
    storage_class_name_scope_module,
):
    """
    Export token secret for pull-mode backup of the Windows VM.

    Returns:
        Secret: Pull-mode token secret
    """
    with Secret(
        name=f"cbt-windows-pull-token-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        string_data={"token": secrets.token_urlsafe(nbytes=16)},
    ) as secret:
        yield secret


@pytest.fixture()
def collected_full_backup_windows_pull_mode(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc_windows_vm,
    pull_mode_token_secret_windows_vm,
    pull_client_backup_pvc_windows_vm,
    windows_vm_boot_disk_size,
    backup_tracker_source_windows_vm,
    storage_class_name_scope_module,
):
    """
    Full pull-mode backup collected for the Windows VM with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"full-windows-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret_windows_vm.name,
        export_token=base64.b64decode(pull_mode_token_secret_windows_vm.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc_windows_vm.name,
        client_backup_pvc_name=pull_client_backup_pvc_windows_vm.name,
        backup_tracker_source=backup_tracker_source_windows_vm,
        force_full_backup=True,
        boot_disk_size=windows_vm_boot_disk_size,
    )
    yield pull_client_backup_pvc_windows_vm.name


@pytest.fixture()
def restored_vm_from_full_backup_windows_pull_mode(
    unprivileged_client,
    namespace,
    collected_full_backup_windows_pull_mode,
    windows_vm_with_cbt,
    windows_vm_boot_disk_size,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    Windows VM restored from collected pull-mode client storage and started.

    Returns:
        VirtualMachineForTests: Running restored Windows VM
    """
    restore_generator = restore_and_start_vm_from_pull_client_backup(
        vm=windows_vm_with_cbt,
        client_backup_pvc_name=collected_full_backup_windows_pull_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=windows_vm_boot_disk_size,
        ssh_timeout=TIMEOUT_30MIN,
        **vm_boot_pvc_spec,
    )
    restored_vm = next(restore_generator)
    wait_for_windows_vm(vm=restored_vm, version="2022")
    try:
        yield restored_vm
    finally:
        restore_generator.close()


@pytest.fixture()
def windows_incremental_test_data_written(
    windows_vm_with_cbt,
    completed_full_backup_windows_push_mode,
):
    """
    Incremental test data written to Windows VM after full backup (push chain).

    Returns:
        VirtualMachineForTests: Windows VM with incremental test data written
    """
    write_file_windows_vm(
        vm=windows_vm_with_cbt,
        file_path=CBT_WINDOWS_INCREMENTAL_TEST_DATA_FILE,
        content=CBT_WINDOWS_INCREMENTAL_TEST_DATA,
    )
    yield windows_vm_with_cbt


@pytest.fixture()
def completed_incremental_backup_windows_push_mode(
    unprivileged_client,
    namespace,
    backup_tracker_source_windows_vm,
    windows_backup_pvc,
    windows_incremental_test_data_written,
    storage_class_name_scope_module,
):
    """
    Incremental backup of Windows VM in push mode, completed.

    Returns:
        VirtualMachineBackup: Completed incremental backup
    """
    with VirtualMachineBackup(
        mode=VirtualMachineBackup.Mode.PUSH,
        name=f"incr-windows-push-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        pvc_name=windows_backup_pvc.name,
        force_full_backup=False,
        source=backup_tracker_source_windows_vm,
    ) as backup:
        backup.wait_for_condition(
            condition="Complete",
            status=VirtualMachineBackup.Condition.Status.TRUE,
            timeout=TIMEOUT_10MIN,
            sleep_time=TIMEOUT_5SEC,
        )
        yield backup


@pytest.fixture()
def restored_vm_from_incremental_backup_windows_push_mode(
    unprivileged_client,
    namespace,
    completed_incremental_backup_windows_push_mode,
    windows_vm_with_cbt,
    windows_vm_boot_disk_size,
    vm_boot_pvc_spec,
    windows_backup_pvc,
    storage_class_name_scope_module,
):
    """
    Windows VM restored from incremental backup (push mode) and started.

    Returns:
        VirtualMachineForTests: Running restored Windows VM
    """
    restore_generator = restore_and_start_vm_from_push_backup(
        vm=windows_vm_with_cbt,
        backup=completed_incremental_backup_windows_push_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=windows_vm_boot_disk_size,
        backup_pvc_name=windows_backup_pvc.name,
        ssh_timeout=TIMEOUT_30MIN,
        **vm_boot_pvc_spec,
    )
    restored_vm = next(restore_generator)
    wait_for_windows_vm(vm=restored_vm, version="2022")
    try:
        yield restored_vm
    finally:
        restore_generator.close()


@pytest.fixture()
def windows_incremental_test_data_written_pull_mode(
    windows_vm_with_cbt,
    collected_full_backup_windows_pull_mode,
):
    """
    Incremental test data written to Windows VM after full backup (pull chain).

    Returns:
        VirtualMachineForTests: Windows VM with incremental test data written
    """
    write_file_windows_vm(
        vm=windows_vm_with_cbt,
        file_path=CBT_WINDOWS_INCREMENTAL_TEST_DATA_FILE,
        content=CBT_WINDOWS_INCREMENTAL_TEST_DATA,
    )
    yield windows_vm_with_cbt


@pytest.fixture()
def collected_incremental_backup_windows_pull_mode(
    unprivileged_client,
    namespace,
    pull_backup_staging_pvc_windows_vm,
    pull_mode_token_secret_windows_vm,
    pull_client_backup_pvc_windows_vm,
    windows_vm_boot_disk_size,
    collected_full_backup_windows_pull_mode,
    backup_tracker_source_windows_vm,
    storage_class_name_scope_module,
):
    """
    Incremental pull-mode backup collected for the Windows VM with the backup CR deleted.

    Returns:
        str: Name of the client PVC containing offline full and incremental pull backup data
    """
    create_and_collect_pull_mode_backup(
        name=f"incr-windows-pull-{cbt_resource_id(name=storage_class_name_scope_module)}",
        namespace=namespace.name,
        client=unprivileged_client,
        token_secret_name=pull_mode_token_secret_windows_vm.name,
        export_token=base64.b64decode(pull_mode_token_secret_windows_vm.instance.data["token"]).decode("utf-8"),
        staging_pvc_name=pull_backup_staging_pvc_windows_vm.name,
        client_backup_pvc_name=pull_client_backup_pvc_windows_vm.name,
        backup_tracker_source=backup_tracker_source_windows_vm,
        force_full_backup=False,
        boot_disk_size=windows_vm_boot_disk_size,
    )
    yield collected_full_backup_windows_pull_mode


@pytest.fixture()
def restored_vm_from_incremental_backup_windows_pull_mode(
    unprivileged_client,
    namespace,
    collected_incremental_backup_windows_pull_mode,
    windows_vm_with_cbt,
    windows_vm_boot_disk_size,
    vm_boot_pvc_spec,
    storage_class_name_scope_module,
):
    """
    Windows VM restored from incremental pull-mode client storage and started.

    Returns:
        VirtualMachineForTests: Running restored Windows VM
    """
    restore_generator = restore_and_start_vm_from_pull_client_backup(
        vm=windows_vm_with_cbt,
        client_backup_pvc_name=collected_incremental_backup_windows_pull_mode,
        namespace=namespace.name,
        client=unprivileged_client,
        storage_class=storage_class_name_scope_module,
        size=windows_vm_boot_disk_size,
        ssh_timeout=TIMEOUT_30MIN,
        **vm_boot_pvc_spec,
    )
    restored_vm = next(restore_generator)
    wait_for_windows_vm(vm=restored_vm, version="2022")
    try:
        yield restored_vm
    finally:
        restore_generator.close()
