import ast
import shlex
from contextlib import contextmanager

import pytest
from kubernetes.client.rest import ApiException
from ocp_resources.datavolume import DataVolume
from ocp_resources.template import Template
from ocp_resources.virtual_machine_snapshot import VirtualMachineSnapshot
from pyhelper_utils.shell import run_ssh_commands

from tests.storage.snapshots.constants import ERROR_MSG_USER_CANNOT_CREATE_VM_SNAPSHOTS
from utilities.constants import TIMEOUT_10MIN, Images
from utilities.infra import (
    cleanup_artifactory_secret_and_config_map,
    get_artifactory_config_map,
    get_artifactory_secret,
    get_http_image_url,
)
from utilities.virt import VirtualMachineForTestsFromTemplate, get_windows_os_dict, running_vm


def expected_output_after_restore(snapshot_number):
    """
    Returns a string representing the list of files that should exist in the VM (sorted)
    after a restore snapshot was performed

    Args:
        snapshot_number (int): The snapshot number that was restored

    Returns:
        string: the list of files that should exist on the VM after restore operation was performed
    """
    files = []
    for idx in range(snapshot_number - 1):
        files.append(f"before-snap-{idx + 1}.txt")
        files.append(f"after-snap-{idx + 1}.txt")
    files.append(f"before-snap-{snapshot_number}.txt ")
    files.sort()
    return " ".join(files)


def fail_to_create_snapshot_no_permissions(snapshot_name, namespace, vm_name, client):
    with pytest.raises(
        ApiException,
        match=ERROR_MSG_USER_CANNOT_CREATE_VM_SNAPSHOTS,
    ):
        with VirtualMachineSnapshot(
            name=snapshot_name,
            namespace=namespace,
            vm_name=vm_name,
            client=client,
        ):
            return


def assert_directory_existence(expected_result, windows_vm, directory_path):
    cmd = shlex.split(f'powershell -command "Test-Path -Path {directory_path}"')
    out = run_ssh_commands(host=windows_vm.ssh_exec, commands=cmd)[0].strip()
    assert expected_result == ast.literal_eval(out), f"Directory exist: {out}, expected result: {expected_result}"


def start_windows_vm_after_restore(vm_restore, windows_vm):
    vm_restore.wait_restore_done(timeout=TIMEOUT_10MIN)
    running_vm(vm=windows_vm)


@contextmanager
def create_windows11_vm(dv_name, namespace, client, vm_name, cpu_model, storage_class):
    artifactory_secret = get_artifactory_secret(namespace=namespace)
    artifactory_config_map = get_artifactory_config_map(namespace=namespace)
    dv = DataVolume(
        name=dv_name,
        namespace=namespace,
        storage_class=storage_class,
        source="http",
        url=get_http_image_url(image_directory=Images.Windows.UEFI_WIN_DIR, image_name=Images.Windows.WIN11_IMG),
        size=Images.Windows.DEFAULT_DV_SIZE,
        client=client,
        api_name="storage",
        secret=artifactory_secret,
        cert_configmap=artifactory_config_map.name,
    )
    dv.to_dict()
    with VirtualMachineForTestsFromTemplate(
        name=vm_name,
        namespace=namespace,
        client=client,
        labels=Template.generate_template_labels(**get_windows_os_dict(windows_version="win-11")["template_labels"]),
        cpu_model=cpu_model,
        data_volume_template={"metadata": dv.res["metadata"], "spec": dv.res["spec"]},
    ) as vm:
        running_vm(vm=vm)
        yield vm
    cleanup_artifactory_secret_and_config_map(
        artifactory_secret=artifactory_secret, artifactory_config_map=artifactory_config_map
    )
