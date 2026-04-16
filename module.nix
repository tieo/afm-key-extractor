self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.airtag-tracker;
  package = self.packages.${pkgs.system}.server;
  provisionPkg = self.packages.${pkgs.system}.provision-vm;
in {
  options.services.airtag-tracker = {
    enable = lib.mkEnableOption "AirTag location tracker";

    port = lib.mkOption {
      type = lib.types.port;
      default = 8042;
      description = "Port for the web UI and API.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/airtag-tracker";
      description = "Directory for keys, database, and account session.";
    };

    pollInterval = lib.mkOption {
      type = lib.types.int;
      default = 900;
      description = "Seconds between location polls (default: 15 min).";
    };

    vm = {
      enable = lib.mkEnableOption "macOS VM for AirTag key extraction";

      vmDir = lib.mkOption {
        type = lib.types.path;
        default = "/var/lib/airtag-tracker/osx-kvm";
        description = "Path to OSX-KVM directory with macOS disk image.";
      };

      vncPort = lib.mkOption {
        type = lib.types.port;
        default = 5901;
        description = "VNC port for macOS VM display.";
      };

      websocketPort = lib.mkOption {
        type = lib.types.port;
        default = 6901;
        description = "WebSocket port for noVNC proxy.";
      };

    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      users.users.airtag-tracker = {
        isSystemUser = true;
        group = "airtag-tracker";
        home = cfg.dataDir;
      };
      users.groups.airtag-tracker = {};

      systemd.services.airtag-tracker = {
        description = "AirTag location tracker";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];

        environment = {
          AIRTAG_DATA_DIR = cfg.dataDir;
          AIRTAG_PORT = toString cfg.port;
          AIRTAG_POLL_INTERVAL = toString cfg.pollInterval;
          AIRTAG_VM_ENABLED = lib.boolToString cfg.vm.enable;
          AIRTAG_VM_DIR = cfg.vm.vmDir;
          AIRTAG_VNC_WS_PORT = toString cfg.vm.websocketPort;
        };

        path = [ pkgs.qemu ];

        serviceConfig = {
          ExecStart = "${package}/bin/airtag-tracker";
          User = "airtag-tracker";
          Group = "airtag-tracker";
          StateDirectory = "airtag-tracker";
          Restart = "on-failure";
          RestartSec = 10;
          # QEMU runs as a daemonized child — default KillMode=control-group
          # would kill it alongside the tracker on restart, rebooting the VM
          # on every deploy. "process" kills only the main PID; the tracker
          # reattaches to the existing QEMU on startup.
          KillMode = "process";
          # QEMU runs as a child process — deprioritize so it doesn't starve
          # other services (especially dnsmasq) when doing heavy VM work.
          Nice = 10;
          IOSchedulingClass = "best-effort";
          IOSchedulingPriority = 7;
        };
      };
    }

    (lib.mkIf cfg.vm.enable {
      boot.extraModprobeConfig = ''
        options kvm ignore_msrs=1
        options kvm report_ignored_msrs=0
      '';

      environment.systemPackages = [ pkgs.qemu ];

      security.sudo.extraRules = [{
        users = [ "airtag-tracker" ];
        commands = [
          { command = "/run/current-system/sw/bin/systemctl start airtag-novnc"; options = [ "NOPASSWD" ]; }
          { command = "/run/current-system/sw/bin/systemctl stop airtag-novnc"; options = [ "NOPASSWD" ]; }
          { command = "/run/current-system/sw/bin/systemctl start airtag-provision-vm"; options = [ "NOPASSWD" ]; }
          { command = "/run/current-system/sw/bin/systemctl restart airtag-provision-vm"; options = [ "NOPASSWD" ]; }
          { command = "/run/current-system/sw/bin/systemctl restart --no-block airtag-provision-vm"; options = [ "NOPASSWD" ]; }
        ];
      }];

      # One-time VM provisioning — downloads macOS + OSX-KVM, creates disk.
      systemd.services.airtag-provision-vm = {
        description = "Provision macOS VM for AirTag key extraction";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];
        unitConfig.ConditionPathExists = "!${cfg.vm.vmDir}/mac_hdd_ng.img";
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          User = "airtag-tracker";
          Group = "airtag-tracker";
          ExecStart = "${provisionPkg}/bin/airtag-provision-vm";
          Environment = [ "AIRTAG_VM_DIR=${cfg.vm.vmDir}" ];
        };
      };

      # Inject a stable, plausible Apple device identity into the OpenCore
      # config.plist baked into the cloned qcow2. The upstream OSX-KVM
      # ships placeholder values (SystemUUID=zeros, SystemSerialNumber=
      # W00000000001) which Apple's CloudKit/FMIP reject as fraudulent —
      # blocking iCloud Keychain sync and Find-My Location Services, the
      # two services searchpartyd needs to publish AirTag beacon keys.
      # Needs root to modprobe nbd + mount the EFI partition; runs once
      # after provisioning and idempotently skips if marker is present.
      systemd.services.airtag-patch-identity = {
        description = "Inject stable Apple identity into OpenCore config";
        after = [ "airtag-provision-vm.service" ];
        wants = [ "airtag-provision-vm.service" ];
        before = [ "airtag-tracker.service" ];
        wantedBy = [ "multi-user.target" ];
        unitConfig.ConditionPathExists = [
          "${cfg.vm.vmDir}/OpenCore/OpenCore.qcow2"
          "!${cfg.vm.vmDir}/.identity-patched"
        ];
        path = [ pkgs.qemu pkgs.python3 pkgs.util-linux pkgs.kmod pkgs.coreutils ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script = ''
          set -euo pipefail
          VM_DIR=${cfg.vm.vmDir}
          QCOW=$VM_DIR/OpenCore/OpenCore.qcow2
          MNT=$(mktemp -d)
          modprobe nbd max_part=8
          # Always release any stale nbd attachment before claiming.
          qemu-nbd -d /dev/nbd0 >/dev/null 2>&1 || true
          cleanup() {
            umount "$MNT" 2>/dev/null || true
            qemu-nbd -d /dev/nbd0 >/dev/null 2>&1 || true
            rmdir "$MNT" 2>/dev/null || true
          }
          trap cleanup EXIT
          qemu-nbd -c /dev/nbd0 "$QCOW"
          # nbd partition scan can lag briefly after attach.
          for _ in $(seq 1 10); do
            [ -b /dev/nbd0p1 ] && break
            sleep 0.3
          done
          mount /dev/nbd0p1 "$MNT"
          python3 ${./server/airtag_tracker/vm_identity.py} \
            "$VM_DIR/vm-identity.json" "$MNT/EFI/OC/config.plist"
          umount "$MNT"
          qemu-nbd -d /dev/nbd0
          chown airtag-tracker:airtag-tracker "$QCOW" "$VM_DIR/vm-identity.json"
          touch "$VM_DIR/.identity-patched"
          chown airtag-tracker:airtag-tracker "$VM_DIR/.identity-patched"
        '';
      };

      # noVNC websocket proxy — bridges browser to VM's VNC display.
      # Started/stopped on demand by the tracker server.
      systemd.services.airtag-novnc = {
        description = "noVNC WebSocket proxy for macOS VM";
        serviceConfig = {
          Type = "simple";
          ExecStart = "${pkgs.python3Packages.websockify}/bin/websockify --web ${pkgs.novnc}/share/webapps/novnc 127.0.0.1:${toString cfg.vm.websocketPort} localhost:${toString cfg.vm.vncPort}";
          Restart = "on-failure";
        };
      };

    })
  ]);
}
