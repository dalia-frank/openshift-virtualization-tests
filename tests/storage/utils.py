import logging
import shlex
from contextlib import contextmanager

import requests
from ocp_resources.cdi import CDI
from ocp_resources.cluster_role import ClusterRole
from ocp_resources.config_map import ConfigMap
from ocp_resources.daemonset import DaemonSet
from ocp_resources.datavolume import DataVolume
from ocp_resources.hostpath_provisioner import HostPathProvisioner
from ocp_resources.pod import Pod
from ocp_resources.resource import Resource
from ocp_resources.role_binding import RoleBinding
from ocp_resources.route import Route
from ocp_resources.service import Service
from ocp_resources.storage_class import StorageClass
from ocp_resources.storage_profile import StorageProfile
from ocp_resources.upload_token_request import UploadTokenRequest
from ocp_resources.virtual_machine import VirtualMachine
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from utilities.constants import (
    CDI_UPLOADPROXY,
    OS_FLAVOR_CIRROS,
    TIMEOUT_2MIN,
    TIMEOUT_30MIN,
    Images,
)
from utilities.hco import ResourceEditorValidateHCOReconcile
from utilities.infra import (
    get_artifactory_config_map,
    get_artifactory_secret,
    get_http_image_url,
    get_pod_by_name_prefix,
)
from utilities.ssp import validate_os_info_vmi_vs_windows_os
from utilities.storage import (
    PodWithPVC,
    create_dv,
    create_vm_from_dv,
    get_containers_for_pods_with_pvc,
    is_snapshot_supported_by_sc,
    sc_volume_binding_mode_is_wffc,
)
from utilities.virt import (
    VirtualMachineForTests,
    running_vm,
    vm_instance_from_template,
    wait_for_windows_vm,
)

LOGGER = logging.getLogger(__name__)


@contextmanager
def import_image_to_dv(
    dv_name,
    images_https_server_name,
    storage_ns_name,
    https_server_certificate,
):
    url = get_file_url_https_server(images_https_server=images_https_server_name, file_name=Images.Cirros.QCOW2_IMG)
    with ConfigMap(
        name="https-cert-configmap",
        namespace=storage_ns_name,
        data={"tlsregistry.crt": https_server_certificate},
    ) as configmap:
        with create_dv(
            source="http",
            dv_name=dv_name,
            namespace=configmap.namespace,
            url=url,
            cert_configmap=configmap.name,
            storage_class=py_config["default_storage_class"],
        ) as dv:
            yield dv


@contextmanager
def upload_image_to_dv(dv_name, storage_ns_name, storage_class, client, consume_wffc=True):
    with create_dv(
        source="upload",
        dv_name=dv_name,
        namespace=storage_ns_name,
        size="3Gi",
        storage_class=storage_class,
        client=client,
        consume_wffc=consume_wffc,
    ) as dv:
        dv.wait_for_status(status=DataVolume.Status.UPLOAD_READY, timeout=TIMEOUT_2MIN)
        yield dv


@contextmanager
def upload_token_request(storage_ns_name, pvc_name, data):
    with UploadTokenRequest(name="upload-image", namespace=storage_ns_name, pvc_name=pvc_name) as utr:
        token = utr.create().status.token
        LOGGER.info("Ensure upload was successful")
        sampler = TimeoutSampler(
            wait_timeout=TIMEOUT_2MIN,
            sleep=5,
            func=upload_image,
            token=token,
            data=data,
        )
        for sample in sampler:
            if sample == 200:
                break


def create_windows_vm_validate_guest_agent_info(
    dv,
    namespace,
    unprivileged_client,
    vm_params,
):
    with vm_instance_from_template(
        request=vm_params,
        existing_data_volume=dv,
        namespace=namespace,
        unprivileged_client=unprivileged_client,
    ) as vm_dv:
        wait_for_windows_vm(vm=vm_dv, version=vm_params["os_version"], timeout=TIMEOUT_30MIN)
        validate_os_info_vmi_vs_windows_os(vm=vm_dv)


def upload_image(token, data, asynchronous=False):
    headers = {"Authorization": f"Bearer {token}"}
    uploadproxy = Route(name=CDI_UPLOADPROXY, namespace=py_config["hco_namespace"])
    uploadproxy_url = f"https://{uploadproxy.host}/v1alpha1/upload"
    if asynchronous:
        uploadproxy_url = f"{uploadproxy_url}-async"
    LOGGER.info(msg=f"Upload {data} to {uploadproxy_url}")
    try:
        with open(data, "rb") as fd:
            fd_data = fd.read()
    except (OSError, IOError):
        fd_data = data

    return requests.post(url=uploadproxy_url, data=fd_data, headers=headers, verify=False).status_code


class HttpService(Service):
    def to_dict(self):
        super().to_dict()
        self.res.update({
            "spec": {
                "selector": {"name": "internal-http"},
                "ports": [
                    {"name": "rate-limit", "port": 82},
                    {"name": "http-auth", "port": 81},
                    {"name": "http-no-auth", "port": 80},
                    {"name": "https", "port": 443},
                ],
            }
        })


def get_file_url_https_server(images_https_server, file_name):
    return f"{images_https_server}{Images.Cirros.DIR}/{file_name}"


@contextmanager
def create_cluster_role(name, api_groups, verbs, permissions_to_resources):
    """
    Create cluster role
    """
    with ClusterRole(
        name=name,
        rules=[
            {
                "apiGroups": api_groups,
                "resources": permissions_to_resources,
                "verbs": verbs,
            },
        ],
    ) as cluster_role:
        yield cluster_role


@contextmanager
def create_role_binding(
    name,
    namespace,
    subjects_kind,
    subjects_name,
    role_ref_kind,
    role_ref_name,
    subjects_namespace=None,
    subjects_api_group=None,
):
    """
    Create role binding
    """
    with RoleBinding(
        name=name,
        namespace=namespace,
        subjects_kind=subjects_kind,
        subjects_name=subjects_name,
        subjects_api_group=subjects_api_group,
        subjects_namespace=subjects_namespace,
        role_ref_kind=role_ref_kind,
        role_ref_name=role_ref_name,
    ) as role_binding:
        yield role_binding


@contextmanager
def set_permissions(
    role_name,
    verbs,
    permissions_to_resources,
    binding_name,
    namespace,
    subjects_name,
    subjects_kind="User",
    subjects_api_group=None,
    subjects_namespace=None,
):
    with create_cluster_role(
        name=role_name,
        api_groups=["cdi.kubevirt.io"],
        permissions_to_resources=permissions_to_resources,
        verbs=verbs,
    ) as cluster_role:
        with create_role_binding(
            name=binding_name,
            namespace=namespace,
            subjects_kind=subjects_kind,
            subjects_name=subjects_name,
            subjects_api_group=subjects_api_group,
            subjects_namespace=subjects_namespace,
            role_ref_kind=cluster_role.kind,
            role_ref_name=cluster_role.name,
        ) as role_binding:
            yield [cluster_role, role_binding]


def create_vm_and_verify_image_permission(dv):
    with create_vm_from_dv(dv=dv) as vm:
        running_vm(vm=vm, check_ssh_connectivity=False, wait_for_interfaces=False)
        v_pod = vm.vmi.virt_launcher_pod
        LOGGER.debug("Check image exist, permission and ownership")
        output = v_pod.execute(command=["ls", "-l", "/var/run/kubevirt-private/vmi-disks/dv-disk"])
        assert "disk.img" in output
        assert "-rw-rw----." in output
        assert "qemu qemu" in output


def get_importer_pod(
    dyn_client,
    namespace,
):
    try:
        for pod in TimeoutSampler(
            wait_timeout=30,
            sleep=1,
            func=get_pod_by_name_prefix,
            dyn_client=dyn_client,
            pod_prefix="importer",
            namespace=namespace,
        ):
            if pod:
                return pod
    except TimeoutExpiredError:
        LOGGER.error("Importer pod not found")
        raise


def wait_for_importer_container_message(importer_pod, msg):
    LOGGER.info(f"Wait for {importer_pod.name} container to show message: {msg}")
    try:
        sampled_msg = TimeoutSampler(
            wait_timeout=120,
            sleep=5,
            func=lambda: importer_container_status_reason(importer_pod) == Pod.Status.CRASH_LOOPBACK_OFF
            and msg
            in importer_pod.instance.status.containerStatuses[0]
            .get("lastState", {})
            .get("terminated", {})
            .get("message", ""),
        )
        for sample in sampled_msg:
            if sample:
                return
    except TimeoutExpiredError:
        LOGGER.error(f"{importer_pod.name} did not get message: {msg}")
        raise


def importer_container_status_reason(pod):
    """
    Get status for why importer pod container is waiting or terminated
    (for container status running there is no 'reason' key)
    """
    container_state = pod.instance.status.containerStatuses[0].state
    if container_state.waiting:
        return container_state.waiting.reason
    if container_state.terminated:
        return container_state.terminated.reason


def verify_snapshot_used_namespace_transfer(cdv, unprivileged_client):
    cdv.wait_for_dv_success()
    storage_class = cdv.storage_class
    # Namespace transfer is not possible with WFFC
    if is_snapshot_supported_by_sc(
        sc_name=storage_class, client=unprivileged_client
    ) and not sc_volume_binding_mode_is_wffc(sc=storage_class):
        assert_pvc_snapshot_clone_annotation(pvc=cdv.pvc, storage_class=storage_class)


def assert_pvc_snapshot_clone_annotation(pvc, storage_class):
    clone_type_annotation_str = f"{Resource.ApiGroup.CDI_KUBEVIRT_IO}/cloneType"
    clone_type_annotation = pvc.instance["metadata"].get("annotations").get(clone_type_annotation_str)
    # For snapshot capable storage, 'csi-clone' may be set in the StorageProfile
    expected_clone_type_annotation = StorageProfile(name=storage_class).instance.status.cloneStrategy
    assert clone_type_annotation == expected_clone_type_annotation, (
        f"{clone_type_annotation_str}: {clone_type_annotation}, expected: '{expected_clone_type_annotation}'"
    )


def hpp_cr_suffix(is_hpp_cr_legacy):
    return "" if is_hpp_cr_legacy else "-csi"


def is_hpp_cr_legacy(hostpath_provisioner):
    # Only New HPP CR has storage storagePools field.
    # If there are no explicit storagePools in the CR - it's a Legacy CR.
    return not hostpath_provisioner.instance.spec.storagePools


def get_hpp_daemonset(hco_namespace, hpp_cr_suffix):
    daemonset = DaemonSet(
        name=f"{HostPathProvisioner.Name.HOSTPATH_PROVISIONER}{hpp_cr_suffix}",
        namespace=hco_namespace.name,
    )
    assert daemonset.exists, "hpp_daemonset does not exist"
    return daemonset


@contextmanager
def update_scratch_space_sc(cdi_config, new_sc, hco):
    def _wait_for_sc_update():
        samples = TimeoutSampler(
            wait_timeout=30,
            sleep=1,
            func=lambda: cdi_config.scratch_space_storage_class_from_status == new_sc,
        )
        for sample in samples:
            if sample:
                return

    with ResourceEditorValidateHCOReconcile(
        patches={hco: {"spec": {"scratchSpaceStorageClass": new_sc}}},
        list_resource_reconcile=[CDI],
    ) as edited_cdi_config:
        _wait_for_sc_update()

        yield edited_cdi_config


def create_cirros_dv(
    namespace,
    name,
    storage_class,
    access_modes=None,
    volume_mode=None,
    client=None,
    dv_size=Images.Cirros.DEFAULT_DV_SIZE,
):
    with create_dv(
        dv_name=f"dv-{name}",
        namespace=namespace,
        url=get_http_image_url(image_directory=Images.Cirros.DIR, image_name=Images.Cirros.QCOW2_IMG),
        size=dv_size,
        storage_class=storage_class,
        access_modes=access_modes,
        volume_mode=volume_mode,
        client=client,
    ) as dv:
        dv.wait_for_dv_success()
        yield dv


def check_snapshot_indication(snapshot, is_online):
    snapshot_indications = snapshot.instance.status.indications
    online = "Online"
    if is_online:
        assert online in snapshot_indications, f"No Snapshot indication '{online}'"
    else:
        assert not snapshot_indications, (
            f"Snapshot should not have indications, current indications: {snapshot_indications}"
        )


@contextmanager
def create_cirros_vm(
    storage_class,
    namespace,
    client,
    dv_name,
    vm_name,
    node=None,
    wait_running=True,
    volume_mode=None,
    cpu_model=None,
    annotations=None,
):
    artifactory_secret = get_artifactory_secret(namespace=namespace)
    artifactory_config_map = get_artifactory_config_map(namespace=namespace)

    dv = DataVolume(
        name=dv_name,
        namespace=namespace,
        source="http",
        url=get_http_image_url(image_directory=Images.Cirros.DIR, image_name=Images.Cirros.QCOW2_IMG),
        storage_class=storage_class,
        size=Images.Cirros.DEFAULT_DV_SIZE,
        api_name="storage",
        volume_mode=volume_mode,
        secret=artifactory_secret,
        cert_configmap=artifactory_config_map.name,
    )
    dv.to_dict()
    dv_metadata = dv.res["metadata"]
    with VirtualMachineForTests(
        client=client,
        name=vm_name,
        namespace=dv_metadata["namespace"],
        os_flavor=OS_FLAVOR_CIRROS,
        memory_requests=Images.Cirros.DEFAULT_MEMORY_SIZE,
        data_volume_template={"metadata": dv_metadata, "spec": dv.res["spec"]},
        node_selector=node,
        run_strategy=VirtualMachine.RunStrategy.ALWAYS,
        cpu_model=cpu_model,
        annotations=annotations,
    ) as vm:
        if wait_running:
            running_vm(vm=vm, wait_for_interfaces=False)
        yield vm


@contextmanager
def create_pod_for_pvc(pvc, volume_mode):
    with PodWithPVC(
        namespace=pvc.namespace,
        name=f"{pvc.name}-pod",
        pvc_name=pvc.name,
        containers=get_containers_for_pods_with_pvc(volume_mode=volume_mode, pvc_name=pvc.name),
    ) as pod:
        pod.wait_for_status(status=pod.Status.RUNNING)
        yield pod


def assert_disk_img(pod):
    disk_img = "disk.img"
    ls_pvc_output = pod.execute(command=shlex.split(f"ls -1 /pvc/{disk_img}"))
    assert disk_img in ls_pvc_output, f"{disk_img} is not in: {ls_pvc_output}"


def get_file_url(url, file_name):
    return f"{url}{file_name}"


def assert_num_files_in_pod(pod, expected_num_of_files):
    num_of_file_in_pod = pod.execute(command=shlex.split("ls -1 /pvc")).count("\n")
    assert num_of_file_in_pod == expected_num_of_files, (
        f"Number of file in pod is {num_of_file_in_pod}, while the expected is {expected_num_of_files}"
    )


def assert_use_populator(pvc, storage_class, cluster_csi_drivers_names):
    expected_use_populator_value = (
        StorageClass(name=storage_class).instance.get("provisioner") in cluster_csi_drivers_names
    )
    assert pvc.use_populator == expected_use_populator_value


def wait_for_processes_exit_successfully(processes, timeout):
    try:
        for object_name in processes:
            process = processes[object_name]
            process.join(timeout)
            if process.exception:
                raise process.exception
            assert process.exitcode == 0, f"The object {object_name} wasn't created in the given time"
    except Exception as e:
        LOGGER.error(f"failed with the exception - {e}")
        raise


def clean_up_multiprocess(processes, object_list):
    # deleting objects and closing processes
    for obj in object_list:
        obj.clean_up()
    for object_name in processes:
        process = processes[object_name]
        try:
            if process.is_alive():
                process.kill()
        except Exception as e:
            print(f"Error killing process {process}, associated with {object_name}: {e}")
        finally:
            process.close()
