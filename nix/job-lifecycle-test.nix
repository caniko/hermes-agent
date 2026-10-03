{pkgs, package, source, revision}: let
  testPackage = package.override {extraDependencyGroups = ["dev"];};
  python = "${testPackage.hermesVenv}/bin/python3";
in
  pkgs.testers.runNixOSTest {
    name = "hermes-target-job-lifecycle";
    nodes.worker = {pkgs, ...}: {
      virtualisation = {
        memorySize = 3072;
        cores = 2;
        diskSize = 16384;
      };
      environment.systemPackages = with pkgs; [bash coreutils findutils git openssh systemd util-linux];
      # The disposable transport starts its own loopback sshd. The NixOS module
      # supplies OpenSSH's privilege-separation accounts and runtime directory.
      services.openssh.enable = true;
      services.openssh.openFirewall = false;
      environment.etc."hermes-qualification-source.json".text = builtins.toJSON {
        inherit revision;
        environment = toString testPackage.hermesVenv;
        runtimeEnvironment = toString package.hermesVenv;
      };
      systemd.tmpfiles.rules = ["d /var/lib/hermes-qualification 0700 root root -"];
      # Root is confined to this disposable VM. The suite exercises both a real
      # user manager and the cross-UID system provider; neither can be mocked.
      services.logind.settings.Login.UserStopDelaySec = "infinity";
      system.stateVersion = "26.05";
    };
    testScript = ''
      worker.start()
      worker.wait_for_unit("multi-user.target")
      worker.succeed("loginctl enable-linger root; systemctl start user@0.service")
      worker.wait_for_unit("user@0.service")
      worker.succeed("test -S /run/user/0/bus")
      worker.succeed("getent passwd sshd")
      # Only test fixtures are copied. Production imports must resolve from the
      # built candidate venv, never from an editable source checkout.
      worker.succeed("cp -R ${source}/tests /var/lib/hermes-qualification/; chmod -R u+w /var/lib/hermes-qualification/tests")
      # Mirror bounded qualification output to the VM console as it happens.
      # A driver timeout otherwise retains only pytest dots, losing the failing
      # case names and the assertion that preceded blocked teardown.
      status, output = worker.execute("cd /var/lib/hermes-qualification && set -o pipefail && XDG_RUNTIME_DIR=/run/user/0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus ${python} -m pytest -vv --tb=short -o addopts= -p tests._fixtures.qualification_diagnostics -o faulthandler_timeout=30 --junitxml=/var/lib/hermes-qualification/lifecycle.xml "
          "tests/tools/test_target_job_supervision.py "
          "tests/tools/test_supervision_control_stdin.py "
          "tests/tools/test_supervision_stop_fence.py "
          "tests/tools/test_supervised_process_completion.py "
          "tests/tools/test_filesystem_claims.py "
          "tests/tools/test_filesystem_authority.py "
          "tests/tools/test_filesystem_authority_system.py "
          "tests/gateway/test_api_server_execution_context.py "
          "tests/gateway/test_api_server_run_admission.py "
          "tests/gateway/test_api_server_job_recovery.py "
          "tests/gateway/test_api_server_filesystem_ownership.py "
          "2>&1 | tee /var/lib/hermes-qualification/pytest.txt /dev/console", timeout=900)
      worker.copy_from_machine("/var/lib/hermes-qualification/pytest.txt")
      if worker.execute("test -s /var/lib/hermes-qualification/lifecycle.xml")[0] == 0:
          worker.copy_from_machine("/var/lib/hermes-qualification/lifecycle.xml")
      assert status == 0, output
      worker.succeed("${python} ${source}/scripts/qualify-job-lifecycle.py /var/lib/hermes-qualification/lifecycle.xml /etc/hermes-qualification-source.json /var/lib/hermes-qualification/receipt.json")
      worker.copy_from_machine("/var/lib/hermes-qualification/receipt.json")
    '';
  }
