import logging
import paramiko
import pytest
from _pytest.outcomes import Failed
import time

from tests.common.helpers.tacacs.tacacs_helper import stop_tacacs_server, start_tacacs_server, \
    per_command_authorization_skip_versions, remove_all_tacacs_server, get_ld_path, \
    check_tacacs  # noqa: F401
from tests.tacacs.utils import change_and_wait_aaa_config_update, ensure_tacacs_server_running_after_ut, \
    ssh_connect_remote_retry, ssh_run_command, TIMEOUT_LIMIT, \
    cleanup_tacacs_log, count_authorization_request       # noqa: F401
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import skip_release, wait_until, paramiko_ssh
from .utils import check_server_received
from tests.common.utilities import backup_config, restore_config, \
        reload_minigraph_with_golden_config
from tests.common.helpers.dut_utils import is_container_running
from .utils import duthost_shell_with_unreachable_retry

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any', 't1-multi-asic'),
    pytest.mark.device_type('vs')
]

logger = logging.getLogger(__name__)


def check_ssh_connect_remote_failed(remote_ip, remote_username, remote_password):
    login_failed = False
    try:
        paramiko_ssh(remote_ip, remote_username, remote_password)
    except paramiko.ssh_exception.AuthenticationException as e:
        login_failed = True
        logger.info("Paramiko SSH connect failed with authentication: " + repr(e))

    pytest_assert(login_failed)


def check_ssh_output_any_of(res_stream, exp_vals, timeout=10):
    while timeout > 0:
        res_lines = res_stream.readlines()
        for line in res_lines:
            for exp_val in exp_vals:
                if exp_val in line:
                    return
        time.sleep(1)
        timeout -= 1

    pytest_assert(False)


@pytest.fixture
def remote_user_client(duthosts, enum_rand_one_per_hwsku_hostname, tacacs_creds):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    dutip = duthost.mgmt_ip
    with ssh_connect_remote_retry(
        dutip,
        tacacs_creds['tacacs_authorization_user'],
        tacacs_creds['tacacs_authorization_user_passwd'],
        duthost
    ) as ssh_client:
        yield ssh_client


@pytest.fixture
def remote_rw_user_client(duthosts, enum_rand_one_per_hwsku_hostname, tacacs_creds):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    dutip = duthost.mgmt_ip
    with ssh_connect_remote_retry(
        dutip,
        tacacs_creds['tacacs_rw_user'],
        tacacs_creds['tacacs_rw_user_passwd'],
        duthost
    ) as ssh_client:
        yield ssh_client


@pytest.fixture
def local_user_client():
    with paramiko.SSHClient() as ssh_client:
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        yield ssh_client


@pytest.fixture(scope="module", autouse=True)
def check_image_version(duthost):
    """Skips this test if the SONiC image installed on DUT is older than 202112
    Args:
        duthost: Hostname of DUT.
    Returns:
        None.
    """
    skip_release(duthost, per_command_authorization_skip_versions)


@pytest.fixture
def setup_authorization_tacacs(duthosts, enum_rand_one_per_hwsku_hostname):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    change_and_wait_aaa_config_update(duthost, "sudo config aaa authorization tacacs+")
    yield
    duthost.shell("sudo config aaa authorization local")    # Default authorization method is local


@pytest.fixture
def setup_authorization_tacacs_local(duthosts, enum_rand_one_per_hwsku_hostname):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    change_and_wait_aaa_config_update(duthost, "sudo config aaa authorization \"tacacs+ local\"")
    yield
    duthost.shell("sudo config aaa authorization local")    # Default authorization method is local


def verify_show_aaa(remote_user_client):
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "show aaa")
    if exit_code != 0:
        return False

    try:
        check_ssh_output_any_of(stdout, ['AAA authentication'])
        return True
    except Failed:
        return False


def check_authorization_tacacs_only(
                                    duthosts,
                                    enum_rand_one_per_hwsku_hostname,
                                    tacacs_creds,
                                    remote_user_client):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    """
        Verify TACACS+ user run command in server side whitelist:
            If command have local permission, user can run command.
    """
    # The "config tacacs add" commands will trigger hostcfgd to regenerate tacacs config.
    # If we immediately run "show aaa" command, the client may still be using the first invalid tacacs server.
    # The second valid tacacs may not take effect yet. Wait some time for the valid tacacs server to take effect.
    succeeded = wait_until(10, 1, 0, verify_show_aaa, remote_user_client)
    pytest_assert(succeeded)

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "config aaa", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stderr, ['Root privileges are required for this operation'])

    # Verify TACACS+ user can't run command not in server side whitelist.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "cat /etc/passwd", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stdout, ['/usr/bin/cat authorize failed by TACACS+ with given arguments, not executing'])

    # Verify Local user can't login.
    dutip = duthost.mgmt_ip
    check_ssh_connect_remote_failed(
        dutip, tacacs_creds['local_user'],
        tacacs_creds['local_user_passwd']
    )


def test_authorization_tacacs_only(
                                duthosts,
                                enum_rand_one_per_hwsku_hostname,
                                setup_authorization_tacacs,
                                tacacs_creds,
                                check_tacacs,  # noqa: F811
                                remote_user_client,
                                remote_rw_user_client):

    check_authorization_tacacs_only(
                                    duthosts,
                                    enum_rand_one_per_hwsku_hostname,
                                    tacacs_creds,
                                    remote_user_client)

    # check commands used by scripts
    commands = [
        "show interfaces counters -a -p 3",
        "touch testfile",
        "chmod +w testfile",
        "echo \"test\" > testfile",
        "ls -l testfile | egrep -v -i '^total'",
        "/bin/sed -i '$d' testfile",
        "find -type f -name testfile -print | xargs /bin/rm -f",
        "touch testfile",
        "rm -f testfi*",
        "mkdir -p test",
        "portstat -c",
        "show interfaces portchannel",
        "show platform summary",
        "show interfaces status",
        "show version",
        "show lldp table",
        "show reboot-cause",
        "configlet --help",
        "sonic-db-cli  CONFIG_DB HGET \"FEATURE|macsec\" state"
    ]

    frontend_commands = [
        "show ip bgp neighbor",
        "show ipv6 bgp neighbor",
        "show ip bgp summary",
        "show ipv6 bgp summary",
        "show muxcable firmware",
    ]

    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    if duthost.sonichost.is_frontend_node():
        commands.extend(frontend_commands)
    telemetry_is_running = is_container_running(duthost, 'telemetry')
    gnmi_is_running = is_container_running(duthost, 'gnmi')
    if not telemetry_is_running and gnmi_is_running:
        commands.append("show feature status gnmi")
    else:
        commands.append("show feature status telemetry")

    for subcommand in commands:
        exit_code, stdout, stderr = ssh_run_command(remote_user_client, subcommand, expect_exit_code=0, verify=True)

    rw_commands = [
        "sudo config interface",
        "sudo route_check.py | head -n 100",
        "sudo dmesg -D",
        "sudo sonic-cfggen --print-data",
        "sudo config list-checkpoints",
        "redis-cli -n 4 keys \\*"
    ]

    for subcommand in rw_commands:
        exit_code, stdout, stderr = ssh_run_command(remote_rw_user_client, subcommand, expect_exit_code=0, verify=True)


def test_authorization_tacacs_only_some_server_down(
        duthosts, enum_rand_one_per_hwsku_hostname,
        setup_authorization_tacacs,
        tacacs_creds,
        ptfhost,
        check_tacacs,  # noqa: F811
        remote_user_client):
    """
        Setup multiple tacacs server for this UT.
        Tacacs server 127.0.0.1 not accessible.
    """
    invalid_tacacs_server_ip = "127.0.0.1"
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    tacacs_server_ip = ptfhost.mgmt_ip
    duthost.shell("sudo config tacacs timeout 1")

    # cleanup all tacacs server, if UT break, tacacs server may still left in dut and will break next UT.
    remove_all_tacacs_server(duthost)

    duthost.shell("sudo config tacacs add %s --port 59" % invalid_tacacs_server_ip)
    duthost.shell("sudo config tacacs add %s --port 59" % tacacs_server_ip)

    """
        Verify TACACS+ user run command in server side whitelist:
            If command have local permission, user can run command.
            If command not have local permission, user can't run command.
        Verify TACACS+ user can't run command not in server side whitelist.
        Verify Local user can't login.
    """
    check_authorization_tacacs_only(
                                duthosts,
                                enum_rand_one_per_hwsku_hostname,
                                tacacs_creds,
                                remote_user_client)

    # Cleanup
    duthost.shell("sudo config tacacs delete %s" % invalid_tacacs_server_ip)
    duthost.shell("sudo config tacacs timeout 5")


def test_authorization_tacacs_only_then_server_down_after_login(
        setup_authorization_tacacs, ptfhost, check_tacacs,  # noqa: F811
        remote_user_client, ensure_tacacs_server_running_after_ut):  # noqa: F811

    # Verify when server are accessible, TACACS+ user can run command in server side whitelist.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "show aaa", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['AAA authentication'])

    # Shutdown tacacs server
    stop_tacacs_server(ptfhost)

    # Verify when server are not accessible, TACACS+ user can't run any command.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "show aaa", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(
        stdout,
        ['/usr/local/bin/show not authorized by TACACS+ with given arguments, not executing']
    )

    #  Cleanup UT.
    start_tacacs_server(ptfhost)


def test_authorization_tacacs_and_local(
        duthosts, enum_rand_one_per_hwsku_hostname,
        setup_authorization_tacacs_local, tacacs_creds, check_tacacs, remote_user_client):  # noqa: F811
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    """
        Verify TACACS+ user run command in server side whitelist:
            If command have local permission, user can run command.
    """
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "show aaa", expect_exit_code=0, verify=True)

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "config aaa", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stderr, ['Root privileges are required for this operation'])

    # Verify TACACS+ user can't run command not in server side whitelist but have local permission.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "cat /etc/passwd", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stdout, ['/usr/bin/cat authorize failed by TACACS+ with given arguments, not executing'])

    # Verify Local user can't login.
    dutip = duthost.mgmt_ip
    check_ssh_connect_remote_failed(
        dutip, tacacs_creds['local_user'],
        tacacs_creds['local_user_passwd']
    )


def test_authorization_tacacs_and_local_then_server_down_after_login(
        duthosts, enum_rand_one_per_hwsku_hostname,
        setup_authorization_tacacs_local, tacacs_creds, ptfhost,
        check_tacacs, remote_user_client, local_user_client, ensure_tacacs_server_running_after_ut):  # noqa: F811
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    # Shutdown tacacs server
    stop_tacacs_server(ptfhost)

    # Verify TACACS+ user can run command not in server side whitelist but have permission in local.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "cat /etc/passwd", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['root:x:0:0:root:/root:/bin/bash'])

    # Verify TACACS+ user can't run command in server side whitelist also not have permission in local.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "config tacacs", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(
        stdout,
        ['/usr/local/bin/config not authorized by TACACS+ with given arguments, not executing']
    )
    check_ssh_output_any_of(stderr, ['Root privileges are required for this operation'])

    # Verify Local user can login when tacacs closed, and run command with local permission.
    dutip = duthost.mgmt_ip
    local_user_client.connect(
        dutip, username=tacacs_creds['local_user'],
        password=tacacs_creds['local_user_passwd'],
        allow_agent=False, look_for_keys=False, auth_timeout=TIMEOUT_LIMIT
    )

    exit_code, stdout, stderr = ssh_run_command(local_user_client, "show aaa", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['AAA authentication'])

    # Start tacacs server
    start_tacacs_server(ptfhost)

    # Verify after Local user login, then server becomes accessible,
    # Local user still can run command with local permission.
    exit_code, stdout, stderr = ssh_run_command(local_user_client, "show aaa", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['AAA authentication'])


def test_authorization_local(
        duthosts, enum_rand_one_per_hwsku_hostname,
        tacacs_creds, ptfhost, check_tacacs,  # noqa: F811
        remote_user_client, local_user_client, ensure_tacacs_server_running_after_ut):  # noqa: F811
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    """
        TACACS server up:
            Verify TACACS+ user can run command if have permission in local.
    """
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "show aaa", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['AAA authentication'])

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "config aaa", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stderr, ['Root privileges are required for this operation'])

    # Shutdown tacacs server.
    stop_tacacs_server(ptfhost)

    """
        TACACS server down:
            Verify Local user can login, and run command with local permission.
    """
    dutip = duthost.mgmt_ip
    local_user_client.connect(
        dutip, username=tacacs_creds['local_user'],
        password=tacacs_creds['local_user_passwd'],
        allow_agent=False, look_for_keys=False, auth_timeout=TIMEOUT_LIMIT
    )

    exit_code, stdout, stderr = ssh_run_command(local_user_client, "show aaa", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['AAA authentication'])

    # Cleanup
    start_tacacs_server(ptfhost)


def test_bypass_authorization(
        duthosts, enum_rand_one_per_hwsku_hostname,
        setup_authorization_tacacs, check_tacacs, remote_user_client):  # noqa: F811
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    """
        Verify user can't run script with sh/python with following command.
            python ./testscript.py

        NOTE: TACACS UT using tac_plus as server side, there is a bug that tac_plus can't handle an authorization
              message contains more than 10 attributes.
              Because every command parameter will convert to a TACACS attribute, please don't using more than 5
              command parameters in test case.
    """
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, 'echo "" >> ./testscript.py',
                                                expect_exit_code=0, verify=True)
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "python ./testscript.py",
                                                expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stdout, ['authorize failed by TACACS+ with given arguments, not executing'])

    # Verify user can't run 'find' command with '-exec' parameter.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "find . -exec",
                                                expect_exit_code=1, verify=True)
    exp_outputs = ['not authorized by TACACS+ with given arguments, not executing',
                   'authorize failed by TACACS+ with given arguments, not executing']
    check_ssh_output_any_of(stdout, exp_outputs)

    # Verify user can run 'find' command without '-exec' parameter.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "find . /bin/sh",
                                                expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['/bin/sh'])

    # Verify user can't run command with loader:
    #     /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 sh
    ld_path = get_ld_path(duthost)
    if not ld_path:
        exit_code, stdout, stderr = ssh_run_command(remote_user_client, ld_path + " sh",
                                                    expect_exit_code=1, verify=True)
        check_ssh_output_any_of(stdout, ['authorize failed by TACACS+ with given arguments, not executing'])

    # Verify user can't run command with prefix/quoting:
    #     \sh
    #     "sh"
    #     echo $(sh -c ls)
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "\\sh",
                                                expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stdout, ['authorize failed by TACACS+ with given arguments, not executing'])

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, '"sh"',
                                                expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stdout, ['authorize failed by TACACS+ with given arguments, not executing'])

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "echo $(sh -c ls)",
                                                expect_exit_code=0, verify=True)
    # echo command will run success and return 0, but sh command will be blocked.
    check_ssh_output_any_of(stdout, ['authorize failed by TACACS+ with given arguments, not executing'])


def test_backward_compatibility_disable_authorization(
        duthosts, enum_rand_one_per_hwsku_hostname,
        tacacs_creds, ptfhost, check_tacacs,  # noqa: F811
        remote_user_client, local_user_client, ensure_tacacs_server_running_after_ut):  # noqa: F811
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    # Verify domain account can run command if have permission in local.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "show aaa", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['AAA authentication'])

    # Shutdown tacacs server
    stop_tacacs_server(ptfhost)

    # Verify domain account can't login to device successfully.
    dutip = duthost.mgmt_ip
    check_ssh_connect_remote_failed(
        dutip, tacacs_creds['tacacs_authorization_user'],
        tacacs_creds['tacacs_authorization_user_passwd']
    )

    # Verify local admin account can run command if have permission in local.
    dutip = duthost.mgmt_ip
    local_user_client.connect(
        dutip, username=tacacs_creds['local_user'],
        password=tacacs_creds['local_user_passwd'],
        allow_agent=False, look_for_keys=False, auth_timeout=TIMEOUT_LIMIT
    )

    exit_code, stdout, stderr = ssh_run_command(local_user_client, "show aaa", expect_exit_code=0, verify=True)
    check_ssh_output_any_of(stdout, ['AAA authentication'])

    # Verify local admin account can't run command if not have permission in local.
    exit_code, stdout, stderr = ssh_run_command(local_user_client, "config aaa", expect_exit_code=1, verify=True)
    check_ssh_output_any_of(stderr, ['Root privileges are required for this operation'])
    # cleanup
    start_tacacs_server(ptfhost)


def create_test_files(remote_client):
    exit_code, stdout, stderr = ssh_run_command(remote_client, "touch testfile.1", expect_exit_code=0, verify=True)

    exit_code, stdout, stderr = ssh_run_command(remote_client, "touch testfile.2", expect_exit_code=0, verify=True)

    exit_code, stdout, stderr = ssh_run_command(remote_client, "touch testfile.3", expect_exit_code=0, verify=True)


def test_tacacs_authorization_wildcard(
                                    ptfhost,
                                    duthosts,
                                    enum_rand_one_per_hwsku_hostname,
                                    setup_authorization_tacacs,
                                    tacacs_creds,
                                    check_tacacs,  # noqa: F811
                                    remote_user_client,
                                    remote_rw_user_client):
    # Create files for command with wildcards
    create_test_files(remote_user_client)

    # Verify command with wildcard been send to TACACS server side correctly.
    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "ls *",
                                                expect_exit_code=0, verify=True)
    check_server_received(ptfhost, "cmd=/usr/bin/ls")
    check_server_received(ptfhost, "cmd-arg=*")

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "ls testfile.?",
                                                expect_exit_code=0, verify=True)
    check_server_received(ptfhost, "cmd=/usr/bin/ls")
    check_server_received(ptfhost, "cmd-arg=testfile.?")

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "ls testfile*",
                                                expect_exit_code=0, verify=True)
    check_server_received(ptfhost, "cmd=/usr/bin/ls")
    check_server_received(ptfhost, "cmd-arg=testfile*")

    exit_code, stdout, stderr = ssh_run_command(remote_user_client, "ls test*.?",
                                                expect_exit_code=0, verify=True)
    check_server_received(ptfhost, "cmd=/usr/bin/ls")
    check_server_received(ptfhost, "cmd-arg=test*.?")

    # Create files for command with wildcards
    create_test_files(remote_rw_user_client)

    # Verify sudo command with * been send to TACACS server side correctly.
    exit_code, stdout, stderr = ssh_run_command(remote_rw_user_client, "sudo ls test*.?",
                                                expect_exit_code=0, verify=True)
    check_server_received(ptfhost, "cmd=/usr/bin/sudo")
    check_server_received(ptfhost, "cmd-arg=ls")
    check_server_received(ptfhost, "cmd-arg=test*.?")

    # Not check exit code, if no match found zgrep will exit with 1
    exit_code, stdout, stderr = ssh_run_command(remote_rw_user_client, "sudo zgrep pfcwd /var/log/syslog*")
    check_server_received(ptfhost, "cmd=/usr/bin/sudo")
    check_server_received(ptfhost, "cmd-arg=zgrep")
    check_server_received(ptfhost, "cmd-arg=pfcwd")
    check_server_received(ptfhost, "cmd-arg=/var/log/syslog*")


def test_stop_request_next_server_after_reject(
                                            duthosts,
                                            enum_rand_one_per_hwsku_hostname,
                                            setup_authorization_tacacs,
                                            tacacs_creds,
                                            ptfhost,
                                            check_tacacs):  # noqa: F811
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    # not ignore on version >= 202305
    skip_versions = ["201811", "201911", "202012", "202106", "202111", "202205", "202211"]
    skip_release(duthost, skip_versions)

    # Use ptfhost ipv6 address as second ip address
    ptfhost_vars = ptfhost.host.options['inventory_manager'].get_host(ptfhost.hostname).vars
    if 'ansible_hostv6' not in ptfhost_vars:
        pytest.skip("Skip UT. ptf ansible_hostv6 not configured.")
    tacacs_server_ipv6 = ptfhost_vars['ansible_hostv6']

    # Setup second tacacs server
    duthost_shell_with_unreachable_retry(duthost, "sudo config tacacs add {} --port 59".format(tacacs_server_ipv6))
    duthost.shell("sudo config tacacs timeout 1")

    # Clean tacacs log
    res = ptfhost.command(r'truncate -s 0  /var/log/tac_plus.log')

    # Login with invalied user, the first tacacs server will reject user login
    dutip = duthost.mgmt_ip
    check_ssh_connect_remote_failed(
        dutip,
        "invalid_user",
        "invalid_password"
    )

    # Server side should only have 1 login request log:
    #       After first tacacs server reject user login, tacacs will not try to connect to second server.
    res = ptfhost.command(r"sed -n 's/\(exec authorization request for invalid_user\)/\1/p'  /var/log/tac_plus.log")
    logger.warning(res["stdout_lines"])
    pytest_assert(len(res["stdout_lines"]) == 1)

    # Remove second server IP
    duthost.shell("sudo config tacacs delete %s" % tacacs_server_ipv6)
    duthost.shell("sudo config tacacs timeout 5")


def test_fallback_to_local_authorization_with_config_reload(
                                    ptfhost,
                                    duthosts,
                                    enum_rand_one_per_hwsku_hostname,
                                    setup_authorization_tacacs,
                                    tacacs_creds,
                                    check_tacacs,  # noqa: F811
                                    remote_user_client,
                                    remote_rw_user_client):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    """
        During load minigraph BGP service will shutdown and restart.
        Verify still can run config save command with "tacacs+,local".
    """
    # Skip multi-asic because override_config format are different.
    if duthost.is_multi_asic:
        pytest.skip("Skip test_fallback_to_local_authorization_with_config_reload for multi-asic device")

    #  Backup config before load minigraph
    CONFIG_DB = "/etc/sonic/config_db.json"
    CONFIG_DB_BACKUP = "/etc/sonic/config_db.json_before_override"
    backup_config(duthost, CONFIG_DB, CONFIG_DB_BACKUP)

    # Reload minigraph with override per-command authorization to "tacacs+,local"
    tacacs_server_ip = ptfhost.mgmt_ip
    tacacs_passkey = tacacs_creds[duthost.hostname]['tacacs_passkey']
    override_config = {
        "AAA": {
            "authentication": {"login": "tacacs+"},
            "accounting": {"login": "tacacs+,local"},
            "authorization": {"login": "tacacs+,local"}
        },
        "TACPLUS": {
            "global": {"auth_type": "login", "passkey": tacacs_passkey}
        },
        "TACPLUS_SERVER": {
            tacacs_server_ip: {"priority": "60", "tcp_port": "59", "timeout": "2"}
        }
    }
    try:
        reload_minigraph_with_golden_config(duthost, override_config)

        # Shutdown tacacs server to simulate network unreachable because BGP shutdown
        stop_tacacs_server(ptfhost)

        # Test "sudo config save -y" can success after reload minigraph
        exit_code, stdout, stderr = ssh_run_command(remote_rw_user_client, "sudo config save -y",
                                                    expect_exit_code=0, verify=True)

        #  Cleanup UT.
        start_tacacs_server(ptfhost)
    finally:
        #  Restore config after test finish
        restore_config(duthost, CONFIG_DB, CONFIG_DB_BACKUP)


def test_tacacs_authorization_commands_during_login(
                                                ptfhost,
                                                duthosts,
                                                enum_rand_one_per_hwsku_hostname,
                                                setup_authorization_tacacs,
                                                tacacs_creds,
                                                check_tacacs,  # noqa: F811
                                                remote_rw_user_client):
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    duthost.shell("sudo config aaa authentication debug disable")
    duthost.shell("sudo service auditd stop")
    duthost.shell("sudo service auditd start")
    change_and_wait_aaa_config_update(duthost, "sudo config aaa accounting local")

    # Clean tacacs log
    cleanup_tacacs_log(ptfhost, remote_rw_user_client)

    # Create a new SSH session
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    dutip = duthost.mgmt_ip
    test_user = tacacs_creds['tacacs_authorization_user']
    with ssh_connect_remote_retry(
        dutip,
        test_user,
        tacacs_creds['tacacs_authorization_user_passwd'],
        duthost
    ) as ssh_client:
        # run some command to make sure login finish
        ssh_client.exec_command("/usr/bin/run-parts")
        ssh_client.exec_command("grep")
        # get authorization command count during user login
        count = count_authorization_request(ptfhost)
        if count > 10:
            """
                Get local accounting log for debug
                Remove all ansible command log with /D command,
                which will match following format:
                    "ansible.legacy.command Invoked"
                Remove all usermod command with /D command,
                which will match following format:
                    "usermod"
                Remove all command exit log with /D command,
                which will match following format:
                    "exit=.*"
                Find logs run by test user from syslog:
                    Find logs match following format:
                        "INFO audisp-tacplus: Accounting: user: ,.*, command: .*command,"
                    Print matched logs with /P command.
            """
            log_pattern = "/ansible.legacy.command Invoked/D;\
                            /usermod/D;\
                            /exit=.*/D;\
                            /INFO audisp-tacplus.+Accounting: user: {0},.*, command: .*,/P" \
                        .format(test_user)

            res = duthost.shell("sed -nE '{0}' /var/log/syslog".format(log_pattern))["stdout"]
            logger.warning("Found {} commands during login, local accounting log: {}".format(count, res))
            pytest_assert(False, "Device execute {} commands during login,\
                           please check and remove unecessary login commands: {}".format(count, res))


def test_send_remote_address(
                            ptfhost,
                            duthosts,
                            enum_rand_one_per_hwsku_hostname,
                            tacacs_creds,
                            check_tacacs,  # noqa: F811
                            remote_rw_user_client):
    """
        Verify TACACS+ send remote address to server.
    """

    # Set accounting to local because per-command accounting TACACS request also send remote address
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    change_and_wait_aaa_config_update(duthost, 'sudo config aaa accounting local')

    # Clean tacacs log
    ptfhost.command(r'truncate -s 0  /var/log/tac_plus.log')

    # Send a authorization packet to TACACS server
    ssh_run_command(remote_rw_user_client, "show version")

    exit_code, stdout_stream, stderr_stream = ssh_run_command(remote_rw_user_client, "echo $SSH_CONNECTION")
    pytest_assert(exit_code == 0)

    # Remote address is first part of SSH_CONNECTION: '10.250.0.1 47462 10.250.0.101 22'
    stdout = stdout_stream.readlines()
    remote_address = stdout[0].split(" ")[0]
    check_server_received(ptfhost, remote_address)
