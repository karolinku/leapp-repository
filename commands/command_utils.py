import hashlib
import json
import os
import re
import resource

from leapp.actors import config as actor_config
from leapp.exceptions import CommandError
from leapp.utils import audit, path

HANA_BASE_PATH = '/hana/shared'
HANA_SAPCONTROL_PATH_X86_64 = 'exe/linuxx86_64/hdb/sapcontrol'
HANA_SAPCONTROL_PATH_PPC64LE = 'exe/linuxppc64le/hdb/sapcontrol'

LEAPP_UPGRADE_FLAVOUR_DEFAULT = 'default'
LEAPP_UPGRADE_FLAVOUR_SAP_HANA = 'saphana'
LEAPP_UPGRADE_PATHS = 'upgrade_paths.json'

VERSION_REGEX = re.compile(r"^([1-9]\d*)\.(\d+)$")


def check_version(version):
    """
    Versioning schema: MAJOR.MINOR
    In case version contains an invalid version string, an CommandError will be raised.

    :raises: CommandError
    :return: release tuple
    """
    if not re.match(VERSION_REGEX, version):
        raise CommandError(
            "Unexpected format of target version: {}. "
            "The required format is 'X.Y' (major and minor version).".format(version)
        )
    return version.split('.')


def get_major_version(version):
    """
    Return the major version from the given version string.

    Versioning schema: MAJOR.MINOR.PATCH

    :param str version: The version string according to the versioning schema described.
    :rtype: str
    :returns: The major version from the given version string.
    """
    return str(check_version(version)[0])


def detect_sap_hana():
    """
    Detect SAP HANA based on existence of /hana/shared/*/exe/linuxx86_64/hdb/sapcontrol
    """
    if os.path.exists(HANA_BASE_PATH):
        for entry in os.listdir(HANA_BASE_PATH):
            # Does /hana/shared/{entry}/exe/linuxx86_64/hdb/sapcontrol exist?
            sap_on_intel = os.path.exists(os.path.join(HANA_BASE_PATH, entry, HANA_SAPCONTROL_PATH_X86_64))
            sap_on_power = os.path.exists(os.path.join(HANA_BASE_PATH, entry, HANA_SAPCONTROL_PATH_PPC64LE))
            if sap_on_intel or sap_on_power:
                return True
    return False


def get_upgrade_flavour():
    """
    Returns the flavour of the upgrade for this system.
    """
    if detect_sap_hana():
        return LEAPP_UPGRADE_FLAVOUR_SAP_HANA
    return LEAPP_UPGRADE_FLAVOUR_DEFAULT


def _retrieve_os_release_contents(_os_release_path='/etc/os-release'):
    """
    Retrieve the contents of /etc/os-release

    :rtype: dict[str, str]
    """
    with open(_os_release_path) as os_release_handle:
        lines = os_release_handle.readlines()
        return dict(line.strip().split('=', 1) for line in lines if '=' in line)


def get_os_release_version_id(filepath):
    """
    Retrieve data about System OS release from provided file.

    :return: `str` version_id
    """
    return _retrieve_os_release_contents(_os_release_path=filepath).get('VERSION_ID', '').strip('"')


def get_upgrade_paths_config():
    # NOTE(ivasilev) Importing here not to have circular dependencies
    from leapp.cli.commands.upgrade import util  # noqa: C415; pylint: disable=import-outside-toplevel

    repository = util.load_repositories_from('repo_path', '/etc/leapp/repo.d/', manager=None)
    with open(path.get_common_file_path(repository, LEAPP_UPGRADE_PATHS)) as f:
        upgrade_paths_map = json.loads(f.read())
    return upgrade_paths_map


def get_target_versions_from_config(src_version_id, distro, flavor):
    """
    Retrieve all possible target versions from upgrade_paths_map.
    If no match is found returns empty list.
    """
    upgrade_paths_map = get_upgrade_paths_config()
    return upgrade_paths_map.get(distro, {}).get(flavor, {}).get(src_version_id, [])


def get_supported_target_versions(flavour=get_upgrade_flavour()):
    """
    Return a list of supported target versions for the given `flavour` of upgrade.
    The default value for `flavour` is `default`.
    """

    os_release_contents = _retrieve_os_release_contents()
    current_version_id = os_release_contents.get('VERSION_ID', '').strip('"')
    distro_id = os_release_contents.get('ID', '').strip('"')

    target_versions = get_target_versions_from_config(current_version_id, distro_id, flavour)
    if not target_versions:
        # If we cannot find a particular major.minor version in the map,
        # we fallback to pick a target version just based on a major version.
        # This can happen for example when testing not yet released versions
        major_version = get_major_version(current_version_id)
        target_versions = get_target_versions_from_config(major_version, distro_id, flavour)

    return target_versions


def get_target_version(flavour):
    target_versions = get_supported_target_versions(flavour)
    return target_versions[-1] if target_versions else None


def vet_upgrade_path(args):
    """
    Make sure the user requested upgrade_path is a supported one.
    If LEAPP_DEVEL_TARGET_RELEASE is set then it's value is not vetted against upgrade_paths_map but used as is.

    :return: `tuple` (target_release, flavor)
    """
    flavor = get_upgrade_flavour()
    env_version_override = os.getenv('LEAPP_DEVEL_TARGET_RELEASE')
    if env_version_override:
        check_version(env_version_override)
        return (env_version_override, flavor)
    target_release = args.target or get_target_version(flavor)
    return (target_release, flavor)


def set_resource_limits():
    """
    Set resource limits for the maximum number of open file descriptors and the maximum writable file size.

    :raises: `CommandError` if the resource limits cannot be set
    """

    def set_resource_limit(resource_type, soft, hard):
        rtype_string = (
            'open file descriptors' if resource_type == resource.RLIMIT_NOFILE
            else 'writable file size' if resource_type == resource.RLIMIT_FSIZE
            else 'unknown resource'
        )
        try:
            resource.setrlimit(resource_type, (soft, hard))
        except ValueError as err:
            raise CommandError(
                'Failure occurred while attempting to set soft limit higher than the hard limit. '
                'Resource type: {}, error: {}'.format(rtype_string, err)
            )
        except OSError as err:
            raise CommandError(
                'Failed to set resource limit. Resource type: {}, error: {}'.format(rtype_string, err)
            )

    soft_nofile, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    soft_fsize, _ = resource.getrlimit(resource.RLIMIT_FSIZE)
    nofile_limit = 1024*16
    fsize_limit = resource.RLIM_INFINITY

    if soft_nofile < nofile_limit:
        set_resource_limit(resource.RLIMIT_NOFILE, nofile_limit, nofile_limit)

    if soft_fsize != fsize_limit:
        set_resource_limit(resource.RLIMIT_FSIZE, fsize_limit, fsize_limit)


def load_actor_configs_and_store_it_in_db(context, repositories, framework_cfg):
    """
    Load actor configuration so that actor's can access it and store it into leapp db.

    :param context: Current execution context
    :param repositories: Discovered repositories
    :param framework_cfg: Leapp's configuration
    """
    # Read the Actor Config and validate it against the schemas saved in the
    # configuration.

    actor_config_schemas = tuple(actor.config_schemas for actor in repositories.actors)
    actor_config_schemas = actor_config.normalize_schemas(actor_config_schemas)
    actor_config_path = framework_cfg.get('actor_config', 'path')

    # Note: actor_config.load() stores the loaded actor config into a global
    # variable which can then be accessed by functions in that file.  Is this
    # the right way to store that information?
    actor_cfg = actor_config.load(actor_config_path, actor_config_schemas)

    # Dump the collected configuration, checksum it and store it inside the DB
    config_text = json.dumps(actor_cfg)
    config_text_hash = hashlib.sha256(config_text.encode('utf-8')).hexdigest()
    config_data = audit.ActorConfigData(config=config_text, hash_id=config_text_hash)
    db_config = audit.ActorConfig(config=config_data, context=context)
    db_config.store()
